"""Tests for the read-only verifier's judgement.

The verifier's whole value is telling "Snowflake refused this on privileges"
apart from "Snowflake could not find the object", because only the first
proves anything. An earlier version could not: its privilege markers included
Snowflake's *object-not-found* text, and every DML probe targeted a table that
by design did not exist, so a role holding INSERT/UPDATE/DELETE passed. These
tests pin the distinction using Snowflake's actual error strings.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "scripts" / "verify_snowflake_readonly.py"

_spec = importlib.util.spec_from_file_location("verify_snowflake_readonly", MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["verify_snowflake_readonly"] = _module
_spec.loader.exec_module(_module)

classify_error = _module.classify_error
disallowed_grants = _module.disallowed_grants
probe_verdict = _module.probe_verdict


# --------------------------------------------------------------------------
# Error classification.
# --------------------------------------------------------------------------
# Verbatim Snowflake responses. The first group is what a missing object looks
# like; note that it contains the words "not authorized" without being a
# privilege error, which is the trap.
NOT_FOUND = [
    "002003 (42S02): SQL compilation error:\n"
    "Object 'ANALYTICS.PUBLIC.SAM_READONLY_VERIFY_PROBE' does not exist or not authorized.",
    "002003 (02000): SQL compilation error:\nSchema 'ANALYTICS.NOPE' does not exist or not authorized.",
    "002043 (02000): SQL compilation error:\nObject does not exist, or operation cannot be performed.",
]

PRIVILEGE = [
    "003001 (42501): SQL access control error:\n"
    "Insufficient privileges to operate on table 'ORDERS'",
    "003011 (42501): SQL access control error:\n"
    "Insufficient privileges to operate on schema 'PUBLIC'",
    "003001 (42501): SQL access control error:\n"
    "Insufficient privileges to operate on database 'ANALYTICS'",
]


@pytest.mark.parametrize("message", NOT_FOUND)
def test_missing_object_is_not_a_privilege_error(message):
    assert classify_error(message) == "not_found"


@pytest.mark.parametrize("message", PRIVILEGE)
def test_privilege_refusals_are_recognised(message):
    assert classify_error(message) == "privilege"


def test_unrecognised_errors_are_neither():
    assert classify_error("000605 (57014): Query reached its timeout") == "other"


def test_not_found_wins_over_the_substring_it_contains():
    # "does not exist or not authorized" ends in "not authorized". Matching
    # that substring first is exactly the bug this file exists to prevent.
    message = "Object 'T' does not exist or not authorized."
    assert "not authorized" in message
    assert classify_error(message) == "not_found"


# --------------------------------------------------------------------------
# Probe verdicts. Only an explicit privilege refusal may pass.
# --------------------------------------------------------------------------
def test_a_write_that_succeeds_fails_the_run():
    passed, line = probe_verdict("INSERT", None)
    assert not passed
    assert "the role can write" in line


def test_a_privilege_refusal_passes():
    passed, line = probe_verdict("INSERT", PRIVILEGE[0])
    assert passed
    assert "refused on privileges" in line


@pytest.mark.parametrize("message", NOT_FOUND)
def test_a_missing_object_fails_the_run(message):
    # The old script printed a warning here and still exited 0.
    passed, line = probe_verdict("INSERT", message)
    assert not passed
    assert "proves nothing" in line


def test_an_unrecognised_failure_fails_the_run():
    passed, line = probe_verdict("DELETE", "000605 (57014): Query reached its timeout")
    assert not passed
    assert "unrecognised" in line


def test_an_empty_error_message_does_not_crash():
    passed, line = probe_verdict("UPDATE", "   ")
    assert not passed
    assert "(no message)" in line


# --------------------------------------------------------------------------
# Grant inspection -- the part that covers what a DROP probe cannot safely.
# --------------------------------------------------------------------------
def grant(privilege, granted_on="TABLE", name="ANALYTICS.PUBLIC.ORDERS"):
    return {"privilege": privilege, "granted_on": granted_on, "name": name}


def test_read_only_grants_are_accepted():
    rows = [
        grant("USAGE", "DATABASE", "ANALYTICS"),
        grant("USAGE", "WAREHOUSE", "COMPUTE_WH"),
        grant("SELECT", "TABLE"),
        grant("SELECT", "VIEW", "ANALYTICS.PUBLIC.ORDERS_V"),
    ]
    assert disallowed_grants(rows) == []


@pytest.mark.parametrize(
    "privilege", ["INSERT", "UPDATE", "DELETE", "TRUNCATE", "OWNERSHIP", "MODIFY"]
)
def test_write_grants_are_rejected(privilege):
    problems = disallowed_grants([grant(privilege)])
    assert len(problems) == 1
    assert privilege in problems[0]


def test_an_inherited_role_is_rejected():
    # Granting SYSADMIN to the read-only role shows up as USAGE on a ROLE. The
    # privilege alone looks harmless; what it points at is not.
    problems = disallowed_grants([grant("USAGE", "ROLE", "SYSADMIN")])
    assert len(problems) == 1
    assert "inherits role SYSADMIN" in problems[0]


def test_future_grant_column_name_is_handled():
    # SHOW GRANTS calls the column granted_on; SHOW FUTURE GRANTS calls it
    # grant_on. Reading only one name would silently skip the other's rows.
    assert disallowed_grants([{"privilege": "INSERT", "grant_on": "TABLE", "name": "T"}]) != []


def test_access_denied_alone_is_not_treated_as_proof():
    # Snowflake uses "Access denied" for a session with no current database,
    # which says nothing about privileges. Matching it would turn a setup
    # mistake into a pass, so it falls through to "other" and fails the run.
    message = (
        "090105 (22000): Cannot perform CREATE TABLE. This session does not "
        "have a current database. Access denied."
    )
    assert classify_error(message) == "other"
    assert not probe_verdict("CREATE TABLE", message)[0]
