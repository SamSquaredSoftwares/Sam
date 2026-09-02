"""Tests for the read-only Snowflake role renderer.

The rendered SQL grants privileges, so the two things worth pinning are that
identifiers cannot smuggle extra DDL into it, and that the output actually
contains the grants (and only the grants) we intend.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "snowflake_readonly_role.py"

_spec = importlib.util.spec_from_file_location("snowflake_readonly_role", MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["snowflake_readonly_role"] = _module
_spec.loader.exec_module(_module)

InvalidIdentifier = _module.InvalidIdentifier
render = _module.render
split_statements = _module.split_statements
validate_identifier = _module.validate_identifier

TEMPLATE = (ROOT / "snowflake" / "readonly_role.sql").read_text(encoding="utf-8")


def rendered(**overrides):
    params = {
        "role": "SAM_READONLY",
        "user": "SAM_SERVICE",
        "warehouse": "COMPUTE_WH",
        "database": "ANALYTICS",
        "template": TEMPLATE,
    }
    params.update(overrides)
    return render(**params)


# --------------------------------------------------------------------------
# Identifier validation -- the injection guard.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "SAM_READONLY; DROP ROLE ACCOUNTADMIN",
        "FOO BAR",
        "foo-bar",
        'foo"bar',
        "foo'bar",
        "1_starts_with_digit",
        "",
        "foo\nbar",
        "foo/*c*/bar",
        "foo--comment",
    ],
)
def test_rejects_unsafe_identifiers(value):
    with pytest.raises(InvalidIdentifier):
        validate_identifier(value, "role")


@pytest.mark.parametrize("value", ["SAM_READONLY", "_private", "a$b", "Mixed_Case9"])
def test_accepts_valid_identifiers(value):
    assert validate_identifier(value, "role") == value.upper()


def test_identifiers_are_upper_cased():
    assert validate_identifier("sam_readonly", "role") == "SAM_READONLY"


def test_rejects_over_long_identifier():
    with pytest.raises(InvalidIdentifier):
        validate_identifier("A" * 256, "role")


def test_injection_attempt_is_rejected_before_rendering():
    with pytest.raises(InvalidIdentifier):
        rendered(role="X; GRANT ROLE ACCOUNTADMIN TO USER EVE")


# --------------------------------------------------------------------------
# Rendering.
# --------------------------------------------------------------------------
def test_no_placeholders_remain():
    assert "<" not in rendered().replace("<=", "")


def test_substitutes_all_four_values():
    sql = rendered()
    assert "CREATE ROLE IF NOT EXISTS SAM_READONLY" in sql
    assert "GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE SAM_READONLY;" in sql
    assert "GRANT USAGE ON DATABASE ANALYTICS TO ROLE SAM_READONLY;" in sql
    assert "GRANT ROLE SAM_READONLY TO USER SAM_SERVICE;" in sql


def test_lowercase_input_is_normalized():
    sql = rendered(role="sam_readonly", database="analytics")
    assert "GRANT USAGE ON DATABASE ANALYTICS TO ROLE SAM_READONLY;" in sql


def test_grants_current_and_future_objects():
    sql = rendered()
    for object_type in ("TABLES", "VIEWS"):
        assert f"GRANT SELECT ON ALL {object_type} IN DATABASE ANALYTICS" in sql
        assert f"GRANT SELECT ON FUTURE {object_type} IN DATABASE ANALYTICS" in sql


def test_grants_schema_usage_for_traversal():
    sql = rendered()
    assert "GRANT USAGE ON ALL SCHEMAS IN DATABASE ANALYTICS" in sql
    assert "GRANT USAGE ON FUTURE SCHEMAS IN DATABASE ANALYTICS" in sql


def test_sets_default_role_so_accidental_sessions_land_read_only():
    sql = rendered()
    assert "ALTER USER SAM_SERVICE SET DEFAULT_ROLE = SAM_READONLY;" in sql


# --------------------------------------------------------------------------
# The point of the whole exercise: no write privilege is ever granted.
# --------------------------------------------------------------------------
WRITE_PRIVILEGES = [
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "MODIFY",
    "CREATE TABLE",
    "CREATE SCHEMA",
    "OWNERSHIP",
    "ALL PRIVILEGES",
]


@pytest.mark.parametrize("privilege", WRITE_PRIVILEGES)
def test_grants_no_write_privilege(privilege):
    for statement in split_statements(rendered()):
        if statement.upper().startswith("GRANT"):
            assert f"GRANT {privilege} " not in statement.upper() + " "


def test_only_usage_and_select_are_granted_on_objects():
    granted = set()
    for statement in split_statements(rendered()):
        upper = " ".join(statement.upper().split())
        if upper.startswith("GRANT ") and " TO ROLE " in upper:
            granted.add(upper.split(" ON ")[0].replace("GRANT ", "").strip())
    # "GRANT ROLE SAM_READONLY TO USER ..." has no ON clause and is excluded.
    assert granted == {"USAGE", "SELECT"}


# --------------------------------------------------------------------------
# Statement splitting (used by --execute).
# --------------------------------------------------------------------------
def test_split_statements_drops_comments_and_blank_entries():
    statements = split_statements(rendered())
    assert statements
    assert not any(s.startswith("--") for s in statements)
    assert all(s.strip() for s in statements)


def test_split_statements_starts_by_creating_the_role():
    statements = split_statements(rendered())
    assert statements[0] == "USE ROLE SECURITYADMIN"
    assert "CREATE ROLE IF NOT EXISTS SAM_READONLY" in statements[1]
