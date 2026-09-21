# tests/test_validator_checks.py
"""Phase 4: ValidationResult.checks -- the structured per-check list behind
the `validator_verdict` SSE event. Additive to the existing pass/fail/reasons
contract tests/test_guardrails.py already covers; never asserts on reasons
text (that's test_guardrails.py's job)."""
from __future__ import annotations

from app.core import validator


def _check(result, name):
    return next(c for c in result.checks if c["name"] == name)


def test_valid_select_reports_all_checks_passed():
    result = validator.validate_sql("SELECT id FROM orders WHERE id = 1")
    assert result.passed
    assert _check(result, "sql_parses")["passed"] is True
    assert _check(result, "statement_type_allowed")["passed"] is True
    assert _check(result, "tables_exist")["passed"] is True
    assert _check(result, "columns_exist")["passed"] is True


def test_unknown_table_fails_only_the_tables_exist_check():
    result = validator.validate_sql("SELECT * FROM customers")
    assert not result.passed
    assert _check(result, "tables_exist")["passed"] is False


def test_unknown_column_fails_only_the_columns_exist_check():
    result = validator.validate_sql("SELECT loyalty_points FROM retailers")
    assert not result.passed
    assert _check(result, "tables_exist")["passed"] is True
    assert _check(result, "columns_exist")["passed"] is False


def test_missing_where_clause_fails_the_where_clause_check():
    result = validator.validate_sql("DELETE FROM orders")
    assert not result.passed
    assert _check(result, "where_clause_required")["passed"] is False


def test_select_has_no_where_clause_check_at_all():
    result = validator.validate_sql("SELECT * FROM orders")
    assert result.passed
    assert not any(c["name"] == "where_clause_required" for c in result.checks)


def test_unparseable_sql_fails_sql_parses_and_stops_there():
    result = validator.validate_sql("this is not sql at all (((")
    assert not result.passed
    assert _check(result, "sql_parses")["passed"] is False
    assert result.checks == [{"name": "sql_parses", "passed": False}]


def test_row_impact_within_ceiling_check_on_small_delete():
    result = validator.check_row_impact("DELETE FROM orders WHERE id = 1")
    assert result.passed
    assert _check(result, "row_impact_within_ceiling")["passed"] is True


def test_row_impact_check_fails_over_ceiling():
    result = validator.check_row_impact("DELETE FROM orders WHERE id = 1", ceiling=0)
    assert not result.passed
    assert _check(result, "row_impact_within_ceiling")["passed"] is False


def test_select_and_insert_are_exempt_from_row_impact_checks():
    assert validator.check_row_impact("SELECT * FROM orders").checks == []
