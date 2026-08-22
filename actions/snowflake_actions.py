"""Snowflake actions for the Sam action package.

Connection configuration is taken from Sema4.ai secrets (passed out of band by
the Action Server) with environment-variable fallbacks, so the same actions
work locally, in CI, and when the package is deployed:

    SNOWFLAKE_ACCOUNT     e.g. "myorg-myaccount"
    SNOWFLAKE_USER
    SNOWFLAKE_PASSWORD    (or use key-pair auth via SNOWFLAKE_PRIVATE_KEY_PATH)
    SNOWFLAKE_WAREHOUSE   optional
    SNOWFLAKE_DATABASE    optional
    SNOWFLAKE_SCHEMA      optional
    SNOWFLAKE_ROLE        optional

NOTE: `query_snowflake` guards against non-read-only SQL, but that guard is a
usability guardrail, not a security boundary. The durable protection is to
point SNOWFLAKE_ROLE at a Snowflake role that only holds USAGE/SELECT grants,
so the database itself refuses any write.
"""

import json
import os
import string
from typing import Any

from sema4ai.actions import ActionError, Secret, action

MAX_ROWS = 1000

# Statements the guard will let through, based on the first significant token.
_READ_ONLY_STARTERS = frozenset(
    {"select", "show", "describe", "desc", "with", "explain"}
)

# What EXPLAIN may be applied to.
_EXPLAINABLE = frozenset({"select", "with", "show", "describe", "desc"})

# Tokens that introduce a statement. Used to find what a WITH clause or an
# EXPLAIN actually runs, so `WITH x AS (...) INSERT ...` cannot slip through on
# the strength of its first word alone.
_STATEMENT_KEYWORDS = frozenset(
    {
        "select", "insert", "update", "delete", "merge", "create", "drop",
        "alter", "truncate", "grant", "revoke", "call", "copy", "put", "get",
        "remove", "use", "set", "unset", "begin", "commit", "rollback",
        "execute", "show", "describe", "desc", "explain", "with",
    }
)

_WORD_CHARS = frozenset(string.ascii_letters + string.digits + "_$")
_WHITESPACE = frozenset(" \t\r\n\f\v")


def _scan_sql(sql: str) -> tuple[list[tuple[str, int]], list[int], int]:
    """Tokenize SQL well enough to reason about it safely.

    Skips over string literals, quoted identifiers, and comments so that their
    contents are never mistaken for code -- a semicolon inside `'a;b'` is data,
    not a statement separator.

    Returns:
        tokens: (lowercased word, paren depth) for each bare word, in order.
        semicolons: indices of semicolons found outside strings/comments.
        last_significant: index of the last code character that is not a
            semicolon (-1 when there is none). Used to tell a harmless trailing
            semicolon from a second statement.
    """
    tokens: list[tuple[str, int]] = []
    semicolons: list[int] = []
    last_significant = -1
    depth = 0
    i = 0
    n = len(sql)

    while i < n:
        char = sql[i]

        if char in _WHITESPACE:
            i += 1
            continue

        if sql.startswith("--", i):
            newline = sql.find("\n", i)
            i = n if newline == -1 else newline + 1
            continue

        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end == -1:
                raise ActionError("Unterminated block comment in SQL statement.")
            i = end + 2
            continue

        if sql.startswith("$$", i):
            end = sql.find("$$", i + 2)
            if end == -1:
                raise ActionError("Unterminated $$-quoted string in SQL statement.")
            last_significant = end + 1
            i = end + 2
            continue

        if char == "'":
            i += 1
            while i < n:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            if i >= n:
                raise ActionError("Unterminated string literal in SQL statement.")
            last_significant = i
            i += 1
            continue

        if char == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    break
                i += 1
            if i >= n:
                raise ActionError("Unterminated quoted identifier in SQL statement.")
            last_significant = i
            i += 1
            continue

        if char == "(":
            depth += 1
            last_significant = i
            i += 1
            continue

        if char == ")":
            depth = max(0, depth - 1)
            last_significant = i
            i += 1
            continue

        if char == ";":
            semicolons.append(i)
            i += 1
            continue

        if char in _WORD_CHARS:
            start = i
            while i < n and sql[i] in _WORD_CHARS:
                i += 1
            tokens.append((sql[start:i].lower(), depth))
            last_significant = i - 1
            continue

        last_significant = i
        i += 1

    return tokens, semicolons, last_significant


def validate_read_only_sql(sql: str) -> str:
    """Return the statement to execute, or raise ActionError if it is not safe.

    Accepts a single read-only statement. Comments, string literals, and a
    trailing semicolon are handled; anything that would run a second statement
    or perform a write is rejected.
    """
    tokens, semicolons, last_significant = _scan_sql(sql)

    if semicolons and last_significant > semicolons[0]:
        raise ActionError("Only a single SQL statement is allowed.")

    statement = (sql[: semicolons[0]] if semicolons else sql).strip()

    if not tokens:
        raise ActionError(
            "Only read-only statements (SELECT/SHOW/DESCRIBE/WITH/EXPLAIN) are "
            "allowed, got: <empty>"
        )

    first_word = tokens[0][0]
    if first_word not in _READ_ONLY_STARTERS:
        raise ActionError(
            "Only read-only statements (SELECT/SHOW/DESCRIBE/WITH/EXPLAIN) are "
            f"allowed, got: {first_word}"
        )

    if first_word == "explain":
        explained = next(
            (word for word, _ in tokens[1:] if word in _STATEMENT_KEYWORDS), None
        )
        if explained is not None and explained not in _EXPLAINABLE:
            raise ActionError(
                "EXPLAIN is only allowed for read-only statements, got: "
                f"explain {explained}"
            )

    if first_word == "with":
        body = next(
            (
                word
                for word, depth in tokens[1:]
                if depth == 0 and word in _STATEMENT_KEYWORDS
            ),
            None,
        )
        if body is not None and body != "select":
            raise ActionError(
                f"A WITH clause may only feed a SELECT, got: {body}"
            )

    return statement


def _connect(account: str, user: str, password: str):
    """Open a Snowflake connection from explicit values + env fallbacks."""
    try:
        import snowflake.connector
    except ImportError:
        raise ActionError(
            "snowflake-connector-python is not installed in the action environment."
        )

    account = account or os.environ.get("SNOWFLAKE_ACCOUNT", "")
    user = user or os.environ.get("SNOWFLAKE_USER", "")
    password = password or os.environ.get("SNOWFLAKE_PASSWORD", "")

    missing = [
        name
        for name, value in (
            ("SNOWFLAKE_ACCOUNT", account),
            ("SNOWFLAKE_USER", user),
        )
        if not value
    ]
    if missing:
        raise ActionError(
            "Snowflake connection is not configured. Missing: "
            + ", ".join(missing)
            + ". Provide them as secrets or environment variables."
        )

    connect_args: dict[str, Any] = {
        "account": account,
        "user": user,
        "client_session_keep_alive": False,
        "login_timeout": 30,
        "network_timeout": 60,
    }

    private_key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH", "")
    if private_key_path:
        connect_args["private_key_file"] = private_key_path
        passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
        if passphrase:
            connect_args["private_key_file_pwd"] = passphrase.encode()
    elif password:
        connect_args["password"] = password
    else:
        raise ActionError(
            "No Snowflake credentials found. Set SNOWFLAKE_PASSWORD or "
            "SNOWFLAKE_PRIVATE_KEY_PATH (key-pair auth)."
        )

    for option, env_var in (
        ("warehouse", "SNOWFLAKE_WAREHOUSE"),
        ("database", "SNOWFLAKE_DATABASE"),
        ("schema", "SNOWFLAKE_SCHEMA"),
        ("role", "SNOWFLAKE_ROLE"),
    ):
        value = os.environ.get(env_var)
        if value:
            connect_args[option] = value

    try:
        return snowflake.connector.connect(**connect_args)
    except Exception as e:
        raise ActionError(f"Could not connect to Snowflake: {e}")


def _secret_value(secret: Secret) -> str:
    try:
        return secret.value
    except Exception:
        return ""


@action
def test_snowflake_connection(
    account: Secret, user: Secret, password: Secret
) -> str:
    """Verify the Snowflake connection and report session details.

    Args:
        account: Snowflake account identifier (e.g. "myorg-myaccount"). Falls
            back to the SNOWFLAKE_ACCOUNT environment variable when empty.
        user: Snowflake user name. Falls back to SNOWFLAKE_USER.
        password: Snowflake password. Falls back to SNOWFLAKE_PASSWORD, or
            key-pair auth via SNOWFLAKE_PRIVATE_KEY_PATH.

    Returns:
        A summary of the Snowflake version and current session context.
    """
    conn = _connect(_secret_value(account), _secret_value(user), _secret_value(password))
    try:
        cursor = conn.cursor()
        cursor.execute(
            "select current_version(), current_account(), current_user(), "
            "current_role(), current_warehouse(), current_database(), current_schema()"
        )
        row = cursor.fetchone()
        return (
            f"Connected. version={row[0]} account={row[1]} user={row[2]} "
            f"role={row[3]} warehouse={row[4]} database={row[5]} schema={row[6]}"
        )
    finally:
        conn.close()


@action
def query_snowflake(
    sql: str, account: Secret, user: Secret, password: Secret, max_rows: int = 100
) -> str:
    """Run a read-only SQL query against Snowflake and return rows as JSON.

    Args:
        sql: The SQL statement to run. Only a single read-only statement
            (SELECT / SHOW / DESCRIBE / WITH / EXPLAIN) is allowed.
        account: Snowflake account identifier. Falls back to SNOWFLAKE_ACCOUNT.
        user: Snowflake user name. Falls back to SNOWFLAKE_USER.
        password: Snowflake password. Falls back to SNOWFLAKE_PASSWORD, or
            key-pair auth via SNOWFLAKE_PRIVATE_KEY_PATH.
        max_rows: Maximum number of rows to return (capped at 1000).

    Returns:
        A JSON object with `columns`, `rows`, `row_count`, and `truncated`.
    """
    statement = validate_read_only_sql(sql)
    limit = max(1, min(int(max_rows), MAX_ROWS))

    conn = _connect(_secret_value(account), _secret_value(user), _secret_value(password))
    try:
        cursor = conn.cursor()
        cursor.execute(statement)
        columns = [col[0] for col in cursor.description or []]
        rows = cursor.fetchmany(limit)
        payload = {
            "columns": columns,
            "rows": [[_jsonable(value) for value in row] for row in rows],
            "row_count": len(rows),
            "truncated": len(rows) == limit,
        }
        return json.dumps(payload, ensure_ascii=False)
    except ActionError:
        raise
    except Exception as e:
        raise ActionError(f"Query failed: {e}")
    finally:
        conn.close()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
