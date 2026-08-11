"""Rewrites the validated DELETE/UPDATE/INSERT into a preview: exact rows
(and, for UPDATE, before/after values) the write would touch, run as a
plain SELECT against the read-only engine — nothing is written yet."""
from __future__ import annotations

import time

from sqlalchemy import text

from app.agent.state import AgentState
from app.core.audit import log_event
from app.core.db import get_referencing_tables, read_only_engine
from app.core.dry_run import build_cascade_impact_queries, build_preview
from app.core.validator import check_row_impact


def dry_run_preview_node(state: AgentState) -> dict:
    sql = state["generated_sql"]
    started_at = time.time()

    impact = check_row_impact(sql)
    if not impact.passed:
        log_event(
            event="manipulation_blocked",
            user_input=state["user_input"],
            generated_sql=sql,
            reason="; ".join(impact.reasons),
            latency_seconds=round(time.time() - started_at, 3),
        )
        return {"error": "; ".join(impact.reasons), "manipulation_started_at": started_at}

    preview = build_preview(sql)

    if preview.operation == "INSERT":
        preview_info = {
            "operation": preview.operation,
            "table": preview.table,
            "rows": [],
            "row_count": 1,
            "insert_values": preview.insert_values,
        }
    else:
        with read_only_engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(text(preview.preview_sql)).fetchall()]
        preview_info = {"operation": preview.operation, "table": preview.table, "rows": rows, "row_count": len(rows)}

        if preview.operation == "DELETE":
            # check_row_impact already vetted this: any non-cascade dependents
            # would have blocked above, so every ref left here is a real
            # cascade the user should see before confirming.
            cascade_refs = [
                (r.child_table, r.child_column)
                for r in get_referencing_tables(preview.table)
                if r.on_delete == "CASCADE"
            ]
            impacts = build_cascade_impact_queries(preview, cascade_refs)
            cascades = []
            with read_only_engine.connect() as conn:
                for impact in impacts:
                    cascade_rows = [dict(row._mapping) for row in conn.execute(text(impact.preview_sql)).fetchall()]
                    if cascade_rows:
                        cascades.append(
                            {
                                "table": impact.table,
                                "column": impact.column,
                                "rows": cascade_rows,
                                "row_count": len(cascade_rows),
                            }
                        )
            if cascades:
                preview_info["cascades"] = cascades

    return {
        "preview": preview_info,
        "awaiting_confirmation": True,
        "error": None,
        "manipulation_started_at": started_at,
    }


def route_after_dry_run_preview(state: AgentState) -> str:
    if state.get("error"):
        return "error_response"
    return "confirmation_gate"
