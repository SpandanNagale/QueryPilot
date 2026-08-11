"""Shared helper: render query results as a compact markdown table for
prompt injection. Capped at 200 rows so a large result set doesn't blow the
context window — the insight summarizer and follow-up suggester both only
need to *see* the shape of the data, not every row."""
from __future__ import annotations

MAX_ROWS_IN_PROMPT = 200


def rows_to_markdown(columns: list[str], rows: list[dict]) -> str:
    if not columns:
        return "(no results)"
    shown = rows[:MAX_ROWS_IN_PROMPT]
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join("---" for _ in columns) + " |"
    body = "\n".join("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |" for row in shown)
    table = "\n".join([header, separator, body])
    if len(rows) > MAX_ROWS_IN_PROMPT:
        table += f"\n\n(showing {MAX_ROWS_IN_PROMPT} of {len(rows)} rows)"
    return table
