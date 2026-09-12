"""Regression suite for query_snowflake's read-only SQL guard.

The guard is a usability guardrail, not a security boundary -- the durable
protection is a read-only Snowflake role. These tests pin the behavior we do
rely on: destructive statements and stacked statements are refused, and
ordinary read-only SQL (including comments, string literals containing
semicolons, and a trailing semicolon) is allowed through untouched.
"""

import pytest
from sema4ai.actions import ActionError

from snowflake_actions import validate_read_only_sql

# --------------------------------------------------------------------------
# Rejected: writes, DDL, and anything that is not a read-only statement.
# --------------------------------------------------------------------------
REJECTED_STATEMENTS = [
    "drop table foo",
    "DROP TABLE foo",
    "delete from t",
    "insert into t values (1)",
    "update t set a=1",
    "merge into t using s on t.id=s.id",
    "truncate table t",
    "alter table t add column c int",
    "create table t (a int)",
    "grant select on t to role r",
    "revoke select on t from role r",
    "call my_proc()",
    "use warehouse wh",
    "copy into t from @stage",
    "put file:///tmp/x @stage",
    "remove @stage/file",
    "set x = 1",
    "begin",
    "commit",
    "rollback",
    "execute immediate 'drop table t'",
]


@pytest.mark.parametrize("sql", REJECTED_STATEMENTS)
def test_rejects_non_read_only_statements(sql):
    with pytest.raises(ActionError) as excinfo:
        validate_read_only_sql(sql)
    assert "read-only" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
# Rejected: nothing to run.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("sql", ["", "   ", "\n\t ", ";", "-- just a comment", "/* only */"])
def test_rejects_empty_input(sql):
    with pytest.raises(ActionError) as excinfo:
        validate_read_only_sql(sql)
    assert "<empty>" in str(excinfo.value)


# --------------------------------------------------------------------------
# Rejected: stacked statements (the injection case that matters most).
# --------------------------------------------------------------------------
STACKED_STATEMENTS = [
    "select 1; drop table foo",
    "select 1; drop table foo;",
    "select 1; select 2; drop table t",
    "; drop table foo",
    "select 1 ; delete from t",
    "select 1;\ndrop table foo",
    "show tables; drop table foo",
]


@pytest.mark.parametrize("sql", STACKED_STATEMENTS)
def test_rejects_stacked_statements(sql):
    with pytest.raises(ActionError):
        validate_read_only_sql(sql)


def test_stacked_statement_names_the_reason():
    with pytest.raises(ActionError) as excinfo:
        validate_read_only_sql("select 1; drop table foo")
    assert "single SQL statement" in str(excinfo.value)


# --------------------------------------------------------------------------
# Rejected: statements whose first word is read-only but whose body is not.
# These are the bypasses the first-word-only guard used to allow.
# --------------------------------------------------------------------------
def test_rejects_with_clause_feeding_insert():
    with pytest.raises(ActionError) as excinfo:
        validate_read_only_sql(
            "with x as (select 1) insert into t select * from x"
        )
    assert "WITH clause" in str(excinfo.value)


@pytest.mark.parametrize(
    "sql",
    [
        "with x as (select 1) insert into t select * from x",
        "with x as (select 1) update t set a = 1",
        "with x as (select 1) delete from t",
        "with x as (select 1) merge into t using x on t.id = x.id",
    ],
)
def test_rejects_with_clause_feeding_dml(sql):
    with pytest.raises(ActionError):
        validate_read_only_sql(sql)


def test_rejects_explain_of_destructive_statement():
    with pytest.raises(ActionError) as excinfo:
        validate_read_only_sql("explain drop table t")
    assert "EXPLAIN" in str(excinfo.value)


# --------------------------------------------------------------------------
# Rejected: malformed input that could confuse the scanner.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql, fragment",
    [
        ("select 'unterminated", "Unterminated string literal"),
        ('select "unterminated', "Unterminated quoted identifier"),
        ("select 1 /* unterminated", "Unterminated block comment"),
        ("select $$unterminated", "Unterminated $$-quoted string"),
    ],
)
def test_rejects_unterminated_literals(sql, fragment):
    with pytest.raises(ActionError) as excinfo:
        validate_read_only_sql(sql)
    assert fragment in str(excinfo.value)


# --------------------------------------------------------------------------
# Accepted: ordinary read-only SQL.
# --------------------------------------------------------------------------
ACCEPTED_STATEMENTS = [
    "select 1",
    "SELECT 1",
    "SeLeCt current_timestamp()",
    "show tables",
    "describe table t",
    "desc table t",
    "explain select 1",
    "with x as (select 1) select * from x",
    "with recursive x as (select 1) select * from x",
    "select\n  a,\n  b\nfrom t",
    "select\t1",
]


@pytest.mark.parametrize("sql", ACCEPTED_STATEMENTS)
def test_accepts_read_only_statements(sql):
    assert validate_read_only_sql(sql)


# --------------------------------------------------------------------------
# Accepted: the false positives the original first-word guard rejected.
# --------------------------------------------------------------------------
def test_accepts_line_comment_before_statement():
    assert validate_read_only_sql("-- a note\nselect 1") == "-- a note\nselect 1"


def test_accepts_block_comment_before_statement():
    assert validate_read_only_sql("/* a note */ select 1")


def test_accepts_semicolon_inside_string_literal():
    assert validate_read_only_sql("select ';'") == "select ';'"


def test_accepts_semicolon_inside_string_in_predicate():
    sql = "select * from t where note = 'a;b'"
    assert validate_read_only_sql(sql) == sql


def test_accepts_escaped_quote_inside_string():
    sql = "select * from t where note = 'it''s here; really'"
    assert validate_read_only_sql(sql) == sql


def test_accepts_parenthesized_query():
    assert validate_read_only_sql("(select 1)")


def test_accepts_dollar_quoted_string():
    sql = "select $$a ; b$$"
    assert validate_read_only_sql(sql) == sql


def test_accepts_quoted_identifier_containing_semicolon():
    sql = 'select "odd;name" from t'
    assert validate_read_only_sql(sql) == sql


# --------------------------------------------------------------------------
# Normalization: a trailing semicolon is stripped, not treated as a second
# statement, and the returned statement is what gets executed.
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql, expected",
    [
        ("select 1;", "select 1"),
        ("select 1;   ", "select 1"),
        ("select 1 ;", "select 1"),
        ("  select 1  ", "select 1"),
        ("\n\nselect 1\n", "select 1"),
        ("select 1; -- done", "select 1"),
    ],
)
def test_normalizes_statement(sql, expected):
    assert validate_read_only_sql(sql) == expected


def test_select_function_named_insert_is_not_confused_for_dml():
    """`INSERT` is also a Snowflake string function -- do not reject it."""
    sql = "select insert('abc', 1, 1, 'z')"
    assert validate_read_only_sql(sql) == sql


def test_with_clause_selecting_function_named_insert():
    sql = "with x as (select 1) select insert('abc', 1, 1, 'z') from x"
    assert validate_read_only_sql(sql) == sql


def test_identifier_prefixed_with_keyword_is_not_confused():
    sql = "select * from insert_log"
    assert validate_read_only_sql(sql) == sql
