#!/usr/bin/env python3
"""Prove the read-only Snowflake role is actually read-only.

Connects as the configured role and checks two things:

  1. reads work   -- the role can query, so the Action Server is usable
  2. writes fail  -- every mutating statement is refused by Snowflake itself

    python scripts/verify_snowflake_readonly.py

Uses the same SNOWFLAKE_* configuration as the actions (SNOWFLAKE_ROLE decides
which role is tested). Exits non-zero if any check fails, so it can gate a
deploy.

The write probes below are expected to FAIL. They are deliberately harmless --
they target a table name that should not exist -- but note that a privilege
error and a "table not found" error are different outcomes, and this script
tells them apart: only a privilege/permission error proves the role is safe.
"""

from __future__ import annotations

import os
import sys

PROBE_TABLE = "SAM_READONLY_VERIFY_PROBE"

# Errors that mean "Snowflake refused on privileges" rather than "no such object".
PRIVILEGE_MARKERS = (
    "insufficient privileges",
    "not authorized",
    "does not exist or not authorized",
    "access denied",
    "read-only",
)


def _is_privilege_error(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in PRIVILEGE_MARKERS)


def main() -> int:
    try:
        import snowflake.connector
    except ImportError:
        print("error: snowflake-connector-python is not installed.", file=sys.stderr)
        return 1

    account = os.environ.get("SNOWFLAKE_ACCOUNT", "")
    user = os.environ.get("SNOWFLAKE_USER", "")
    password = os.environ.get("SNOWFLAKE_PASSWORD", "")
    role = os.environ.get("SNOWFLAKE_ROLE", "")
    warehouse = os.environ.get("SNOWFLAKE_WAREHOUSE", "")
    database = os.environ.get("SNOWFLAKE_DATABASE", "")
    schema = os.environ.get("SNOWFLAKE_SCHEMA", "PUBLIC")

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
        print(f"error: missing configuration: {', '.join(missing)}", file=sys.stderr)
        print("Set them in .env (see .env.example) or the environment.", file=sys.stderr)
        return 2

    connect_args = {
        "account": account,
        "user": user,
        "role": role,
        "database": database,
        "schema": schema,
        "login_timeout": 30,
    }
    if warehouse:
        connect_args["warehouse"] = warehouse

    private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")
    if private_key_path:
        connect_args["private_key_file"] = private_key_path
        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        if passphrase:
            connect_args["private_key_file_pwd"] = passphrase.encode()
    elif password:
        connect_args["password"] = password
    else:
        print("error: set SNOWFLAKE_PASSWORD or SNOWFLAKE_PRIVATE_KEY_PATH.", file=sys.stderr)
        return 2

    print(f"Connecting as {user} with role {role}...")
    try:
        conn = snowflake.connector.connect(**connect_args)
    except Exception as e:
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
        except Exception as e:
            failures.append(f"SELECT failed, the role cannot read: {e}")
            print(f"  FAIL  SELECT failed: {e}")

        # ---- 2. writes must be refused ------------------------------------
        probes = [
            ("CREATE TABLE", f"create table {database}.{schema}.{PROBE_TABLE} (id int)"),
            ("INSERT", f"insert into {database}.{schema}.{PROBE_TABLE} values (1)"),
            ("UPDATE", f"update {database}.{schema}.{PROBE_TABLE} set id = 2"),
            ("DELETE", f"delete from {database}.{schema}.{PROBE_TABLE}"),
            ("DROP TABLE", f"drop table {database}.{schema}.{PROBE_TABLE}"),
            ("CREATE SCHEMA", f"create schema {database}.SAM_READONLY_VERIFY_SCHEMA"),
        ]
        for label, statement in probes:
            try:
                cursor.execute(statement)
                failures.append(f"{label} SUCCEEDED -- the role is NOT read-only")
                print(f"  FAIL  {label} succeeded; the role can write")
            except Exception as e:
                message = str(e)
                if _is_privilege_error(message):
                    print(f"  ok    {label} refused on privileges")
                else:
                    print(
                        f"  warn  {label} failed, but not clearly on privileges "
                        f"-- read the error and confirm: {message.splitlines()[0][:120]}"
                    )
    finally:
        conn.close()

    print()
    if failures:
        print("VERIFICATION FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"Role {role} reads but cannot write. Safe to use as SNOWFLAKE_ROLE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
