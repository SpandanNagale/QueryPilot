"""Runs SQL against the database. Two entry points, deliberately separate:

- execute_select(): read-only engine, for the analytics path.
- execute_write_in_transaction(): read-write engine wrapped in BEGIN/COMMIT
  with ROLLBACK on error, for the manipulation path (Phase 5).

Every statement gets an EXPLAIN pre-flight first — cheap, and catches
reference/syntax errors before they touch real data or even a real query
plan for a SELECT.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.core.db import read_only_engine, read_write_engine


@dataclass
class QueryExecutionResult:
    columns: list[str]
    rows: list[dict]
    row_count: int


def explain(sql: str, conn: Connection) -> None:
    """Cheap pre-flight: raises before running the real statement if SQLite's
    query planner can't even build a plan for it (unknown function, bad
    expression, etc. — things AST-level validation can't always catch)."""
    conn.execute(text(f"EXPLAIN {sql}"))


def execute_select(sql: str, engine: Engine = read_only_engine) -> QueryExecutionResult:
    with engine.connect() as conn:
        explain(sql, conn)
        result = conn.execute(text(sql))
        columns = list(result.keys())
        rows = [dict(row._mapping) for row in result.fetchall()]
    return QueryExecutionResult(columns=columns, rows=rows, row_count=len(rows))


@contextmanager
def transaction(engine: Engine = read_write_engine):
    """BEGIN on entry; COMMIT on clean exit; ROLLBACK on exception or if the
    caller explicitly requests it by raising inside the block."""
    conn = engine.connect()
    trans = conn.begin()
    try:
        yield conn
        trans.commit()
    except Exception:
        trans.rollback()
        raise
    finally:
        conn.close()


def execute_write(sql: str, conn: Connection) -> QueryExecutionResult:
    """Runs one write statement on an already-open transactional connection.
    Caller (the transactional_executor node, Phase 5) owns the transaction
    boundary via the `transaction()` context manager above."""
    explain(sql, conn)
    result = conn.execute(text(sql))
    columns = list(result.keys()) if result.returns_rows else []
    rows = [dict(row._mapping) for row in result.fetchall()] if result.returns_rows else []
    return QueryExecutionResult(columns=columns, rows=rows, row_count=result.rowcount)
