#!/usr/bin/env python3
"""Render (and optionally apply) the read-only Snowflake role DDL.

    # print the SQL for review, filling placeholders from the environment
    python scripts/snowflake_readonly_role.py

    # override any value
    python scripts/snowflake_readonly_role.py --role SAM_READONLY --database ANALYTICS

    # apply it (needs admin credentials with SECURITYADMIN + object ownership)
    python scripts/snowflake_readonly_role.py --execute

Printing is the default: this generates privilege changes, so you should read
them before they run.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "snowflake" / "readonly_role.sql"

# Snowflake unquoted identifier: letter or underscore, then letters, digits,
# underscore or dollar. Validated so a name can never smuggle extra DDL into
# the rendered script.
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
MAX_IDENTIFIER_LENGTH = 255


class InvalidIdentifier(ValueError):
    """Raised when a supplied name is not a safe bare Snowflake identifier."""


def validate_identifier(value: str, kind: str) -> str:
    """Return the identifier upper-cased, or raise if it is not safe to inline."""
    if not value:
        raise InvalidIdentifier(f"{kind} is required (got an empty value).")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise InvalidIdentifier(
            f"{kind} is longer than {MAX_IDENTIFIER_LENGTH} characters."
        )
    if not IDENTIFIER.match(value):
        raise InvalidIdentifier(
            f"{kind} must be a bare Snowflake identifier "
            f"(letters, digits, _ and $, not starting with a digit); got: {value!r}"
        )
    return value.upper()


def render(role: str, user: str, warehouse: str, database: str, template: str) -> str:
    """Substitute the four placeholders after validating each one."""
    replacements = {
        "<ROLE>": validate_identifier(role, "role"),
        "<USER>": validate_identifier(user, "user"),
        "<WAREHOUSE>": validate_identifier(warehouse, "warehouse"),
        "<DATABASE>": validate_identifier(database, "database"),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)

    leftover = re.findall(r"<[A-Z_]+>", rendered)
    if leftover:
        raise ValueError(f"Template still has unfilled placeholders: {sorted(set(leftover))}")
    return rendered


def split_statements(sql: str) -> list[str]:
    """Split the rendered script into individual statements.

    The template is ours and contains no string literals, so stripping `--`
    comments and splitting on `;` is sufficient here.
    """
    lines = [line for line in sql.splitlines() if not line.lstrip().startswith("--")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default=os.environ.get("SNOWFLAKE_READONLY_ROLE", "SAM_READONLY"))
    parser.add_argument("--user", default=os.environ.get("SNOWFLAKE_USER", ""))
    parser.add_argument("--warehouse", default=os.environ.get("SNOWFLAKE_WAREHOUSE", ""))
    parser.add_argument("--database", default=os.environ.get("SNOWFLAKE_DATABASE", ""))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the statements instead of printing them. Requires admin credentials.",
    )
    args = parser.parse_args(argv)

    try:
        sql = render(
            role=args.role,
            user=args.user,
            warehouse=args.warehouse,
            database=args.database,
            template=TEMPLATE.read_text(encoding="utf-8"),
        )
    except (InvalidIdentifier, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        print(
            "\nSupply the missing values as flags or environment variables:\n"
            "  --role/SNOWFLAKE_READONLY_ROLE  --user/SNOWFLAKE_USER\n"
            "  --warehouse/SNOWFLAKE_WAREHOUSE --database/SNOWFLAKE_DATABASE",
            file=sys.stderr,
        )
        return 2

    if not args.execute:
        print(sql)
        return 0

    try:
        import snowflake.connector
    except ImportError:
        print("error: snowflake-connector-python is not installed.", file=sys.stderr)
        return 1

    account = os.environ.get("SNOWFLAKE_ACCOUNT", "")
    admin_user = os.environ.get("SNOWFLAKE_ADMIN_USER") or os.environ.get("SNOWFLAKE_USER", "")
    admin_password = os.environ.get("SNOWFLAKE_ADMIN_PASSWORD") or os.environ.get(
        "SNOWFLAKE_PASSWORD", ""
    )
    if not (account and admin_user and admin_password):
        print(
            "error: --execute needs SNOWFLAKE_ACCOUNT plus admin credentials "
            "(SNOWFLAKE_ADMIN_USER/SNOWFLAKE_ADMIN_PASSWORD, falling back to "
            "SNOWFLAKE_USER/SNOWFLAKE_PASSWORD).",
            file=sys.stderr,
        )
        return 2

    statements = split_statements(sql)
    print(f"Applying {len(statements)} statements as {admin_user}...")
    conn = snowflake.connector.connect(
        account=account, user=admin_user, password=admin_password, login_timeout=30
    )
    failures = 0
    try:
        cursor = conn.cursor()
        for statement in statements:
            summary = " ".join(statement.split())[:90]
            try:
                cursor.execute(statement)
                print(f"  ok    {summary}")
            except Exception as e:
                failures += 1
                print(f"  FAIL  {summary}\n        {e}")
    finally:
        conn.close()

    if failures:
        print(
            f"\n{failures} statement(s) failed. Unsupported object types "
            "(materialized/dynamic/external) are safe to ignore; privilege "
            "errors are not -- check you are using a role that owns the objects."
        )
        return 1

    print("\nAll statements applied. Verify with:\n  python scripts/verify_snowflake_readonly.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
