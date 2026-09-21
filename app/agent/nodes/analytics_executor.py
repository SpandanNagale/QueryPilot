from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.config import settings
from app.core import executor as sql_executor
from app.core.db import get_engines
from app.core.validator import validate_sql

logger = logging.getLogger(__name__)


def analytics_executor_node(state: AgentState, config: RunnableConfig) -> dict:
    sql = state["generated_sql"]
    db_path = config["configurable"]["db_path"]
    read_only_engine, _ = get_engines(db_path)

    validation = validate_sql(sql, intent="analytics", engine=read_only_engine)
    if not validation.passed:
        return {"error": "; ".join(validation.reasons), "retry_count": state.get("retry_count", 0) + 1}

    try:
        result = sql_executor.execute_select(sql, engine=read_only_engine)
    except Exception as exc:  # noqa: BLE001 - genuinely want to catch any DB error and self-correct on it
        logger.warning("analytics_executor DB error (sql=%r): %s", sql, exc)
        return {
            "error": f"execution failed: {exc}",
            "client_error": "An unexpected database error occurred while running this query.",
            "retry_count": state.get("retry_count", 0) + 1,
        }

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
