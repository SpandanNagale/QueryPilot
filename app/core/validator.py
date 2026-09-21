"""Deterministic, code-enforced SQL guardrails.

Nothing here trusts the LLM. Every rule is either a plain sqlglot AST check
or a live check against the real database schema/row counts. This module —
not the prompts in app/agent/prompts.py — is the actual safety boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlglot import exp

from app.config import settings
from app.core.db import get_referencing_tables, read_only_engine
from app.core.dry_run import build_cascade_impact_queries, build_preview

DIALECT = "sqlite"

_ALLOWED_STATEMENT_TYPES: dict[type, str] = {
    exp.Select: "SELECT",
    exp.Insert: "INSERT",
    exp.Update: "UPDATE",
    exp.Delete: "DELETE",
}


@dataclass
class ValidationResult:
    passed: bool = True
    statement_type: str | None = None
    reasons: list[str] = field(default_factory=list)
    parsed: exp.Expression | None = field(default=None, repr=False)
    # Structured record of which individual checks ran and passed, for the
    # Phase 4 `validator_verdict` SSE event -- e.g. [{"name": "tables_exist",
    # "passed": true}, ...]. Additive to `reasons` (the human-readable
    # failure text), never a replacement for it.
    checks: list[dict] = field(default_factory=list)

    def fail(self, reason: str) -> None:
        self.passed = False
        self.reasons.append(reason)

    def add_check(self, name: str, passed: bool) -> None:
        self.checks.append({"name": name, "passed": passed})

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "statement_type": self.statement_type,
            "reasons": list(self.reasons),
            "checks": list(self.checks),
        }


def get_live_schema(engine: Engine = read_only_engine) -> dict[str, set[str]]:
    """table_name (lowercase) -> set of column names (lowercase), read
    straight from sqlite_master / PRAGMA table_info so this can never drift
    from the real, current schema."""
    schema: dict[str, set[str]] = {}
    with engine.connect() as conn:
        tables = [
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
        ]
        for table in tables:
            columns = conn.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
            schema[table.lower()] = {col[1].lower() for col in columns}
    return schema


def _parse_single_statement(sql: str, result: ValidationResult) -> exp.Expression | None:
    try:
        statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
    except Exception as exc:
        result.fail(f"SQL failed to parse: {exc}")
        result.add_check("sql_parses", False)
        return None
    result.add_check("sql_parses", True)

    if len(statements) == 0:
        result.fail("No SQL statement found.")
        result.add_check("statement_type_allowed", False)
        return None
    if len(statements) > 1:
        result.fail("Multiple statements in a single request are not allowed.")
        result.add_check("statement_type_allowed", False)
        return None

    stmt = statements[0]
    stmt_type = _ALLOWED_STATEMENT_TYPES.get(type(stmt))
    if stmt_type is None:
        result.fail(
            f"Statement type '{stmt.key.upper()}' is not permitted. "
            "Only SELECT, INSERT, UPDATE, DELETE are allowed."
        )
        result.add_check("statement_type_allowed", False)
        return None

    result.statement_type = stmt_type
    result.add_check("statement_type_allowed", True)
    return stmt


def _check_where_clause(stmt: exp.Expression, result: ValidationResult) -> None:
    if not isinstance(stmt, (exp.Delete, exp.Update)):
        return
    has_where = stmt.args.get("where") is not None
    if not has_where:
        result.fail(
            f"{result.statement_type} requires a WHERE clause — unconditional writes are blocked."
        )
    result.add_check("where_clause_required", has_where)


def _cte_names(stmt: exp.Expression) -> set[str]:
    with_ = stmt.args.get("with")
    if not with_:
        return set()
    return {cte.alias.lower() for cte in with_.find_all(exp.CTE) if cte.alias}


def _check_schema_references(stmt: exp.Expression, schema: dict[str, set[str]], result: ValidationResult) -> None:
    cte_names = _cte_names(stmt)
    alias_to_table: dict[str, str] = {}
    referenced_tables: set[str] = set()
    tables_ok = True

    for t in stmt.find_all(exp.Table):
        name = t.name.lower()
        if name in cte_names:
            continue
        if name not in schema:
            result.fail(f"Unknown table '{t.name}' — it does not exist in the database.")
            tables_ok = False
            continue
        referenced_tables.add(name)
        if t.alias:
            alias_to_table[t.alias.lower()] = name

    result.add_check("tables_exist", tables_ok)

    valid_columns: set[str] = set()
    for t in referenced_tables:
        valid_columns |= schema[t]

    # SELECT-list output aliases (e.g. COUNT(*) AS total) are legitimate
    # even though "total" isn't a real column — they get referenced again
    # in ORDER BY / GROUP BY / HAVING.
    output_aliases = {a.alias.lower() for a in stmt.find_all(exp.Alias) if a.alias}

    columns_ok = True
    for c in stmt.find_all(exp.Column):
        col_name = c.name.lower()
        if not col_name or col_name == "*":
            continue
        if col_name in output_aliases:
            continue
        qualifier = c.table.lower() if c.table else None
        if qualifier:
            real_table = alias_to_table.get(qualifier) or (qualifier if qualifier in schema else None)
            if real_table is None:
                continue  # subquery/CTE alias we can't resolve further; don't false-positive
            if col_name not in schema[real_table]:
                result.fail(f"Unknown column '{c.table}.{c.name}'.")
                columns_ok = False
        else:
            if valid_columns and col_name not in valid_columns:
                result.fail(f"Unknown column '{c.name}'.")
                columns_ok = False

    result.add_check("columns_exist", columns_ok)


def validate_sql(sql: str, *, intent: str = "analytics", engine: Engine = read_only_engine) -> ValidationResult:
    """Full deterministic validation pipeline: statement whitelist, single
    statement, WHERE-clause requirement, and schema-reference checks.

    Does NOT check the row-impact ceiling — that requires an actual dry-run
    COUNT(*) against the live DB, so it's a separate call (check_row_impact)
    made only after this passes.
    """
    result = ValidationResult()

    if intent == "manipulation" and settings.read_only_mode:
        result.fail("Manipulation is disabled: the system is running in READ_ONLY_MODE.")
        return result

    stmt = _parse_single_statement(sql, result)
    if stmt is None:
        return result
    result.parsed = stmt

    _check_where_clause(stmt, result)

    schema = get_live_schema(engine)
    _check_schema_references(stmt, schema, result)

    return result


def inject_limit_if_missing(sql: str, limit: int | None = None) -> str:
    """Analytics-path only: append LIMIT <limit> to a SELECT that lacks one."""
    limit = settings.auto_limit if limit is None else limit
    stmt = sqlglot.parse_one(sql, read=DIALECT)
    if isinstance(stmt, exp.Select) and stmt.args.get("limit") is None:
        stmt = stmt.limit(limit)
    return stmt.sql(dialect=DIALECT)


def check_row_impact(
    sql: str, *, engine: Engine = read_only_engine, ceiling: int | None = None
) -> ValidationResult:
    """Runs the dry-run COUNT(*) for a DELETE/UPDATE and blocks if it exceeds
    the configured ceiling. SELECT and INSERT are exempt: SELECT gets
    auto-LIMIT instead, and INSERT affects exactly the one row given.

    For DELETE, also walks the live foreign-key graph: rows in tables with
    an ON DELETE CASCADE relationship count toward the ceiling too (a
    "small" delete that cascades into hundreds of child rows shouldn't
    sneak under the limit), and any dependent rows in tables WITHOUT a
    cascade path block the delete outright with a clear reason - instead of
    the request reaching the confirmation prompt and only then failing with
    a raw FOREIGN KEY constraint error at commit time.
    """
    result = ValidationResult(passed=True)
    ceiling = settings.row_impact_ceiling if ceiling is None else ceiling

    stmt = sqlglot.parse_one(sql, read=DIALECT)
    if not isinstance(stmt, (exp.Delete, exp.Update)):
        return result

    preview = build_preview(sql)
    with engine.connect() as conn:
        count = conn.execute(text(preview.count_sql)).scalar_one()

    total = count
    if isinstance(stmt, exp.Delete):
        refs = get_referencing_tables(preview.table, engine=engine)
        cascade_refs = [(r.child_table, r.child_column) for r in refs if r.on_delete == "CASCADE"]
        blocking_refs = [r for r in refs if r.on_delete != "CASCADE"]

        impacts = build_cascade_impact_queries(preview, cascade_refs)
        with engine.connect() as conn:
            for impact in impacts:
                total += conn.execute(text(impact.count_sql)).scalar_one()

            fk_cascade_clear = True
            for ref in blocking_refs:
                blocked_count = conn.execute(
                    text(
                        f'SELECT COUNT(*) FROM "{ref.child_table}" WHERE "{ref.child_column}" IN '
                        f"({preview.id_subquery_sql})"
                    )
                ).scalar_one()
                if blocked_count > 0:
                    fk_cascade_clear = False
                    result.fail(
                        f"Can't delete this: {blocked_count} row(s) in '{ref.child_table}' still reference "
                        f"it via '{ref.child_column}'. Remove or reassign those first."
                    )
        result.add_check("fk_cascade_clear", fk_cascade_clear)

        if not result.passed:
            return result

    within_ceiling = total <= ceiling
    if not within_ceiling:
        detail = f" (including {total - count} row(s) that would cascade-delete)" if total != count else ""
        result.fail(
            f"This would affect {total} rows{detail}, which exceeds the safety limit of {ceiling}. "
            "Please narrow your request (e.g. add more specific conditions)."
        )
    result.add_check("row_impact_within_ceiling", within_ceiling)
    return result
