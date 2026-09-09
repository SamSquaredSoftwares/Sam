#!/usr/bin/env python3
"""Prove the Snowflake role the Action Server uses cannot write.

    .venv/bin/python scripts/verify_snowflake_readonly.py

Connects as the configured role (SNOWFLAKE_ROLE) and proves three things:

  1. reads work    -- the role can query, so the Action Server is usable
  2. writes fail   -- mutating statements are refused by Snowflake itself
  3. grants are read-only -- the role holds nothing but read privileges, and
     inherits no other role that could hand it write access

Exits non-zero unless all three are proven, so it can gate a deploy.

Two things this script gets right that a naive version does not:

*   **A write probe must target an object that exists.** Snowflake answers a
    write against a missing table with "Object '...' does not exist or not
    authorized" -- which is *not* proof of anything, because it is the same
    answer a role with full write access gets. So the DML probes run against a
    real table discovered through INFORMATION_SCHEMA (override with
    --probe-table). They are written to touch zero rows and run inside a
    transaction that is rolled back, so they stay harmless even against a role
    that turns out to be able to write.

*   **"Not proven safe" is a failure, not a pass.** A probe that fails for a
    reason other than privileges proves nothing, so it fails the run rather
    than printing a warning nobody reads. The only passing outcome is an
    explicit privilege refusal.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dotenv_file  # noqa: E402  (needs the path above)

# Objects the CREATE probes try to make. These must NOT exist -- for CREATE,
# unlike DML, a missing object is the correct starting state.
PROBE_TABLE = "SAM_READONLY_VERIFY_PROBE"
PROBE_SCHEMA = "SAM_READONLY_VERIFY_SCHEMA"

# Snowflake says "does not exist or not authorized" when an object is missing
# *or* invisible. It contains the words "not authorized" but is not a privilege
# error, so it is matched first and never counted as proof.
NOT_FOUND_MARKERS = (
    "does not exist or not authorized",
    "does not exist, or operation cannot be performed",
    "does not exist",
)

# Snowflake's genuine privilege refusals. Kept deliberately short: an
# unmatched message is classified "other" and fails the run with its text
# shown, which is recoverable. A marker that over-matches turns a failure the
# script does not understand into a silent pass, which is not. "access denied"
# is absent for that reason -- Snowflake also uses it for a session with no
# current database, which says nothing about privileges.
PRIVILEGE_MARKERS = (
    "insufficient privileges",
    "access control error",
    "not authorized to perform",
)

# Privileges a read-only role may hold. Everything else -- OWNERSHIP, INSERT,
# UPDATE, DELETE, TRUNCATE, MODIFY, any CREATE -- makes it not read-only.
READ_ONLY_PRIVILEGES = frozenset({"USAGE", "SELECT", "REFERENCES", "MONITOR", "OPERATE"})


def classify_error(message: str) -> str:
    """Return 'not_found', 'privilege' or 'other' for a Snowflake error.

    Order matters: "does not exist or not authorized" contains "not
    authorized", so the not-found test has to run first or a missing object
    would masquerade as a privilege refusal.
    """
    lowered = message.lower()
    if any(marker in lowered for marker in NOT_FOUND_MARKERS):
        return "not_found"
    if any(marker in lowered for marker in PRIVILEGE_MARKERS):
        return "privilege"
    return "other"


def probe_verdict(label: str, error: str | None) -> tuple[bool, str]:
    """Judge one write probe. Returns (passed, line to print).

    `error` is None when the statement *succeeded*, which is the one outcome
    that means the role can write.
    """
    if error is None:
        return False, f"FAIL  {label} succeeded -- the role can write"

    kind = classify_error(error)
    first_line = error.splitlines()[0][:140] if error.strip() else "(no message)"
    if kind == "privilege":
        return True, f"ok    {label} refused on privileges"
    if kind == "not_found":
        return False, (
            f"FAIL  {label} hit a missing object, which proves nothing "
            f"(a role that can write gets this same error): {first_line}"
        )
    return False, f"FAIL  {label} failed for an unrecognised reason: {first_line}"


def disallowed_grants(rows: list[dict[str, str]]) -> list[str]:
    """Return descriptions of grants that a read-only role must not hold."""
    problems = []
    for row in rows:
        privilege = (row.get("privilege") or "").upper()
        granted_on = (row.get("granted_on") or row.get("grant_on") or "").upper()
        name = row.get("name") or "?"
        if granted_on == "ROLE":
            problems.append(
                f"inherits role {name} -- its privileges apply here too, "
                "so this role is only as read-only as that one"
            )
        elif privilege not in READ_ONLY_PRIVILEGES:
            problems.append(f"holds {privilege} on {granted_on} {name}")
    return problems


def _rows_as_dicts(cursor) -> list[dict[str, str]]:
    """SHOW output as dicts, since its column order is not contractual."""
    columns = [column[0].lower() for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _quote(identifier: str) -> str:
    """Quote one identifier part for interpolation into a probe statement."""
    return '"' + identifier.replace('"', '""') + '"'


def _qualify(catalog: str, schema: str, name: str) -> str:
    return ".".join(_quote(part) for part in (catalog, schema, name))


def find_probe_table(cursor, database: str) -> tuple[str, str, str, str] | None:
    """Pick a real base table the role can see, plus one of its columns.

    INFORMATION_SCHEMA only lists what the current role may access, so
    whatever comes back is readable -- which is exactly what the DML probes
    need in order to be about privileges rather than visibility.
    """
    cursor.execute(
        f"select table_catalog, table_schema, table_name from {_quote(database)}"
        ".information_schema.tables where table_type = 'BASE TABLE' and "
        "table_schema <> 'INFORMATION_SCHEMA' order by table_schema, table_name limit 1"
    )
    row = cursor.fetchone()
    if not row:
        return None
    catalog, schema, table = row

    cursor.execute(
        f"select column_name from {_quote(database)}.information_schema.columns "
        "where table_catalog = %s and table_schema = %s and table_name = %s "
        "order by ordinal_position limit 1",
        (catalog, schema, table),
    )
    column_row = cursor.fetchone()
    if not column_row:
        return None
    return catalog, schema, table, column_row[0]


def _probe(cursor, statement: str) -> str | None:
    """Run a probe. Returns the error text, or None if it succeeded."""
    try:
        cursor.execute(statement)
    except Exception as e:  # noqa: BLE001 -- any refusal is the interesting part
        return str(e)
    return None


def _connect_args(env) -> tuple[dict, str, list[str]]:
    """Build connect kwargs from the environment. Returns (args, role, errors)."""
    account = env.get("SNOWFLAKE_ACCOUNT", "")
    user = env.get("SNOWFLAKE_USER", "")
    role = env.get("SNOWFLAKE_ROLE", "")
    database = env.get("SNOWFLAKE_DATABASE", "")

    missing = [
        name
        for name, value in (
            ("SNOWFLAKE_ACCOUNT", account),
            ("SNOWFLAKE_USER", user),
            ("SNOWFLAKE_ROLE", role),
            ("SNOWFLAKE_DATABASE", database),
        )
        if not value
    ]
    if missing:
        return {}, role, [f"missing configuration: {', '.join(missing)}"]

    args = {
        "account": account,
        "user": user,
        "role": role,
        "database": database,
        "schema": env.get("SNOWFLAKE_SCHEMA", "PUBLIC"),
        "login_timeout": 30,
    }
    if env.get("SNOWFLAKE_WAREHOUSE"):
        args["warehouse"] = env["SNOWFLAKE_WAREHOUSE"]

    private_key_path = env.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")
    if private_key_path:
        args["private_key_file"] = private_key_path
        if env.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"):
            args["private_key_file_pwd"] = env["SNOWFLAKE_PRIVATE_KEY_PASSPHRASE"].encode()
    elif env.get("SNOWFLAKE_PASSWORD"):
        args["password"] = env["SNOWFLAKE_PASSWORD"]
    else:
        return {}, role, ["set SNOWFLAKE_PASSWORD or SNOWFLAKE_PRIVATE_KEY_PATH."]

    return args, role, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove the Snowflake role cannot write.")
    parser.add_argument(
        "--probe-table",
        default=os.environ.get("SAM_VERIFY_PROBE_TABLE", ""),
        metavar="DB.SCHEMA.TABLE",
        help="Existing table to aim the write probes at. Default: discovered "
        "from INFORMATION_SCHEMA. Nothing is written to it either way.",
    )
    args = parser.parse_args(argv)

    dotenv_file.load_repo_dotenv()

    try:
        import snowflake.connector
    except ImportError:
        print(
            "error: snowflake-connector-python is not installed. Run this with the "
            "project virtualenv: .venv/bin/python scripts/verify_snowflake_readonly.py",
            file=sys.stderr,
        )
        return 1

    connect_args, role, errors = _connect_args(os.environ)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        print("Set them in .env (see .env.example) or the environment.", file=sys.stderr)
        return 2

    database = connect_args["database"]
    print(f"Connecting as {connect_args['user']} with role {role}...")
    try:
        conn = snowflake.connector.connect(**connect_args)
    except Exception as e:  # noqa: BLE001
        print(f"error: could not connect: {e}", file=sys.stderr)
        return 1

    failures: list[str] = []
    try:
        cursor = conn.cursor()

        # ---- 1. reads must work -------------------------------------------
        cursor.execute("select current_role(), current_warehouse(), current_database()")
        actual_role, actual_wh, actual_db = cursor.fetchone()
        print(f"  session: role={actual_role} warehouse={actual_wh} database={actual_db}")
        if actual_role and actual_role.upper() != role.upper():
            failures.append(f"connected as role {actual_role}, expected {role}")

        try:
            cursor.execute("select 1")
            cursor.fetchone()
            print("  ok    SELECT works")
        except Exception as e:  # noqa: BLE001
            failures.append(f"SELECT failed, the role cannot read: {e}")
            print(f"  FAIL  SELECT failed: {e}")

        # ---- 2. the role's grants must be read-only ------------------------
        try:
            cursor.execute(f"show grants to role {_quote(role)}")
            problems = disallowed_grants(_rows_as_dicts(cursor))
        except Exception as e:  # noqa: BLE001
            failures.append(f"could not read the role's grants, so they are unproven: {e}")
            print(f"  FAIL  SHOW GRANTS failed: {e}")
        else:
            if problems:
                for problem in problems:
                    failures.append(f"role {role} {problem}")
                    print(f"  FAIL  role {role} {problem}")
            else:
                print("  ok    every grant on the role is read-only")

        # ---- 3. writes must be refused -------------------------------------
        # DML has to hit a table that exists, or the error says nothing about
        # privileges. Everything below touches zero rows regardless.
        located = None
        if args.probe_table:
            parts = [part.strip('"') for part in args.probe_table.split(".")]
            if len(parts) != 3:
                print(
                    f"error: --probe-table must be DB.SCHEMA.TABLE, got {args.probe_table!r}",
                    file=sys.stderr,
                )
                return 2
            try:
                cursor.execute(
                    f"select * from {_qualify(*parts)} where 1 = 0"
                )
                column = cursor.description[0][0]
                located = (*parts, column)
            except Exception as e:  # noqa: BLE001
                failures.append(f"cannot read --probe-table {args.probe_table}: {e}")
                print(f"  FAIL  cannot read --probe-table {args.probe_table}: {e}")
        else:
            try:
                located = find_probe_table(cursor, database)
            except Exception as e:  # noqa: BLE001
                failures.append(f"could not search for a probe table: {e}")
                print(f"  FAIL  could not search for a probe table: {e}")

        if located is None and not failures:
            failures.append(
                f"found no readable base table in {database}, so the write probes "
                "could not run -- pass --probe-table DB.SCHEMA.TABLE"
            )
            print(f"  FAIL  no readable base table in {database} to probe against")

        probes: list[tuple[str, str]] = []
        create_catalog, create_schema = database, connect_args["schema"]
        if located is not None:
            catalog, schema, table, column = located
            create_catalog, create_schema = catalog, schema
            target = _qualify(catalog, schema, table)
            print(f"  probing writes against {target} (zero rows, rolled back)")
            probes += [
                ("INSERT", f"insert into {target} select * from {target} where 1 = 0"),
                ("UPDATE", f"update {target} set {_quote(column)} = {_quote(column)} where 1 = 0"),
                ("DELETE", f"delete from {target} where 1 = 0"),
            ]
        probes += [
            (
                "CREATE TABLE",
                f"create table {_qualify(create_catalog, create_schema, PROBE_TABLE)} (id int)",
            ),
            (
                "CREATE SCHEMA",
                f"create schema {_quote(create_catalog)}.{_quote(PROBE_SCHEMA)}",
            ),
        ]

        _probe(cursor, "begin")
        try:
            for label, statement in probes:
                passed, line = probe_verdict(label, _probe(cursor, statement))
                print(f"  {line}")
                if not passed:
                    failures.append(line[len("FAIL  ") :])
        finally:
            # Nothing above changes a row, but a rollback costs nothing and
            # means a surprise here cannot leave anything behind.
            _probe(cursor, "rollback")
    finally:
        conn.close()

    print()
    if failures:
        print("VERIFICATION FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        print("\nThe role is NOT proven read-only. Do not point SNOWFLAKE_ROLE at it.")
        return 1

    print(f"Role {role} reads but cannot write. Safe to use as SNOWFLAKE_ROLE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
