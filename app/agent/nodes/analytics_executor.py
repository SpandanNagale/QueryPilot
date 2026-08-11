from __future__ import annotations

from app.agent.state import AgentState
from app.config import settings
from app.core import executor as sql_executor
from app.core.validator import validate_sql


def analytics_executor_node(state: AgentState) -> dict:
    sql = state["generated_sql"]

    # Defense in depth: a self-corrected query is still LLM output. Re-check
    # it deterministically before it ever reaches the DB, same as the first
    # attempt did.
    validation = validate_sql(sql, intent="analytics")
    if not validation.passed:
        return {"error": "; ".join(validation.reasons), "retry_count": state.get("retry_count", 0) + 1}

    try:
        result = sql_executor.execute_select(sql)
    except Exception as exc:  # noqa: BLE001 - genuinely want to catch any DB error and self-correct on it
        return {"error": str(exc), "retry_count": state.get("retry_count", 0) + 1}

    return {
        "execution_result": {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.row_count >= settings.auto_limit,
        },
        "error": None,
    }


def route_after_analytics_execution(state: AgentState) -> str:
    if state.get("error") is None:
        return "insight_summarizer"
    if state.get("retry_count", 0) < settings.max_self_correction_attempts:
        return "self_corrector"
    return "error_response"
