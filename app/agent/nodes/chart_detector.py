"""Detects whether a result set is chart-worthy: a categorical/date column
plus a numeric column, with more than 4 rows. Deterministic — no LLM call,
since this is a structural check on the actual result shape, not a
judgment call."""
from __future__ import annotations

import re

from app.agent.state import AgentState

_DATE_RE = re.compile(r"^\d{4}(-\d{2}){0,2}$")


def _is_numeric_column(col: str, rows: list[dict]) -> bool:
    values = [row[col] for row in rows if row.get(col) is not None]
    if not values:
        return False
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values)


def _looks_like_date(value) -> bool:
    return isinstance(value, str) and bool(_DATE_RE.match(value))


def chart_detector_node(state: AgentState) -> dict:
    exec_result = state.get("execution_result")
    if not exec_result or exec_result["row_count"] <= 4:
        return {"chart_spec": None}

    columns = exec_result["columns"]
    rows = exec_result["rows"]

    numeric_cols = [c for c in columns if _is_numeric_column(c, rows)]
    other_cols = [c for c in columns if c not in numeric_cols]

    if not numeric_cols or not other_cols:
        return {"chart_spec": None}

    x, y = other_cols[0], numeric_cols[0]
    first_x_value = rows[0].get(x)

    if _looks_like_date(first_x_value):
        chart_type = "line"
    elif exec_result["row_count"] <= 8:
        chart_type = "pie"
    else:
        chart_type = "bar"

    return {"chart_spec": {"type": chart_type, "x": x, "y": y, "title": f"{y} by {x}"}}
