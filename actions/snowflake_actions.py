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

The connector reads its CA bundle from the environment, so a custom trust store
is configured with `SAM_CA_BUNDLE` / `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`
rather than a connect parameter - see `tls_trust`.
"""

import json
import os
from typing import Any

from sema4ai.actions import ActionError, Secret, action

from tls_trust import CaBundleError, apply_snowflake_ca_env

MAX_ROWS = 1000


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

    # The connector resolves its CA bundle from the environment at handshake
    # time, and silently ignores a path it cannot load. Export the configured
    # bundle under the names it reads, validating it first so a bad path is a
    # clear error here instead of an opaque handshake failure later.
    try:
        apply_snowflake_ca_env()
    except CaBundleError as e:
        raise ActionError(str(e))

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
        sql: The SQL statement to run. Only a single SELECT / SHOW / DESCRIBE /
            WITH statement is allowed.
        account: Snowflake account identifier. Falls back to SNOWFLAKE_ACCOUNT.
        user: Snowflake user name. Falls back to SNOWFLAKE_USER.
        password: Snowflake password. Falls back to SNOWFLAKE_PASSWORD, or
            key-pair auth via SNOWFLAKE_PRIVATE_KEY_PATH.
        max_rows: Maximum number of rows to return (capped at 1000).

    Returns:
        A JSON object with `columns`, `rows`, and `row_count`.
    """
    statement = sql.strip().rstrip(";")
    first_word = statement.split(None, 1)[0].lower() if statement else ""
    if first_word not in ("select", "show", "describe", "desc", "with", "explain"):
        raise ActionError(
            "Only read-only statements (SELECT/SHOW/DESCRIBE/WITH/EXPLAIN) are "
            f"allowed, got: {first_word or '<empty>'}"
        )
    if ";" in statement:
        raise ActionError("Only a single SQL statement is allowed.")

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
