"""Rewrites DELETE/UPDATE statements into read-only SELECTs carrying the
exact same WHERE clause, so the agent can show the user precisely which
rows — and, for UPDATE, which before/after values — a write will touch
before anything executes against the real database.

Nothing in this module ever runs SQL; it only builds SQL strings for the
validator / executor to run against the read-only engine.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"


@dataclass
class PreviewQueries:
    operation: str                     # "DELETE" | "UPDATE" | "INSERT"
    table: str
    preview_sql: str                   # SELECT showing affected/changed rows ("" for INSERT)
    count_sql: str                     # SELECT COUNT(*) for the row-impact ceiling check ("" for INSERT)
    insert_values: dict[str, str] | None = None  # populated only for INSERT
    id_subquery_sql: str | None = None  # "SELECT id FROM <table> WHERE <same condition>" - DELETE only, for cascade lookups


@dataclass
class CascadeImpact:
    table: str
    column: str
    preview_sql: str
    count_sql: str


def build_cascade_impact_queries(
    preview: PreviewQueries, children: list[tuple[str, str]]
) -> list[CascadeImpact]:
    """Given the parent DELETE's id_subquery_sql and a list of
    (child_table, child_column) foreign keys pointing at the deleted table,
    build preview/count SQL for the rows in each child table that would
    also be affected - either cascade-deleted (if the FK is ON DELETE
    CASCADE) or that would block the delete outright (if not)."""
    if not preview.id_subquery_sql:
        return []
    impacts = []
    for child_table, child_column in children:
        impacts.append(
            CascadeImpact(
                table=child_table,
                column=child_column,
                preview_sql=f'SELECT * FROM "{child_table}" WHERE "{child_column}" IN ({preview.id_subquery_sql})',
                count_sql=(
                    f'SELECT COUNT(*) AS affected_count FROM "{child_table}" '
                    f'WHERE "{child_column}" IN ({preview.id_subquery_sql})'
                ),
            )
        )
    return impacts


def build_preview(sql: str) -> PreviewQueries:
    """sql must already have passed validator.validate_sql (single statement,
    correct type, WHERE present on writes) — this does no safety checking of
    its own."""
    stmt = sqlglot.parse_one(sql, read=DIALECT)

    if isinstance(stmt, exp.Delete):
        return _preview_delete(stmt)
    if isinstance(stmt, exp.Update):
        return _preview_update(stmt)
    if isinstance(stmt, exp.Insert):
        return _preview_insert(stmt)
    raise ValueError(f"Cannot build a dry-run preview for statement type '{type(stmt).__name__}'.")


def _where_clause(stmt: exp.Expression) -> exp.Expression | None:
    where = stmt.args.get("where")
    return where.this.copy() if where else None


def _preview_delete(stmt: exp.Delete) -> PreviewQueries:
    table = stmt.args["this"]
    where = _where_clause(stmt)

    preview = exp.select("*").from_(table.copy())
    count = exp.select("COUNT(*) AS affected_count").from_(table.copy())
    id_select = exp.select("id").from_(table.copy())
    if where:
        preview = preview.where(where.copy())
        count = count.where(where.copy())
        id_select = id_select.where(where.copy())

    return PreviewQueries(
        operation="DELETE",
        table=table.name,
        preview_sql=preview.sql(dialect=DIALECT),
        count_sql=count.sql(dialect=DIALECT),
        id_subquery_sql=id_select.sql(dialect=DIALECT),
    )


def _preview_update(stmt: exp.Update) -> PreviewQueries:
    table = stmt.args["this"]
    where = _where_clause(stmt)
    assignments = stmt.args.get("expressions", [])

    projections = ["id"]
    for assign in assignments:
        col = assign.this
        new_expr = assign.args["expression"]
        colname = col.name
        projections.append(f'{col.sql(dialect=DIALECT)} AS "old_{colname}"')
        projections.append(f'{new_expr.sql(dialect=DIALECT)} AS "new_{colname}"')

    preview = exp.select(*projections).from_(table.copy())
    count = exp.select("COUNT(*) AS affected_count").from_(table.copy())
    if where:
        preview = preview.where(where.copy())
        count = count.where(where.copy())

    return PreviewQueries(
        operation="UPDATE",
        table=table.name,
        preview_sql=preview.sql(dialect=DIALECT),
        count_sql=count.sql(dialect=DIALECT),
    )


def _preview_insert(stmt: exp.Insert) -> PreviewQueries:
    schema_node = stmt.args["this"]
    if isinstance(schema_node, exp.Schema):
        table_name = schema_node.this.name
        columns = [c.name for c in schema_node.expressions]
    else:
        table_name = schema_node.name
        columns = []

    values_expr = stmt.args.get("expression")
    row_values: list[str] = []
    if isinstance(values_expr, exp.Values) and values_expr.expressions:
        first_tuple = values_expr.expressions[0]
        row_values = [v.sql(dialect=DIALECT) for v in first_tuple.expressions]

    insert_values = dict(zip(columns, row_values)) if columns else {}

    return PreviewQueries(
        operation="INSERT",
        table=table_name,
        preview_sql="",
        count_sql="",
        insert_values=insert_values,
    )
