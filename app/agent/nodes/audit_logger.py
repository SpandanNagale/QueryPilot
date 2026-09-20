from __future__ import annotations

import time

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.audit import log_event


def audit_logger_node(state: AgentState, config: RunnableConfig) -> dict:
    started_at = state.get("manipulation_started_at")
    log_event(
        event="manipulation_executed" if not state.get("error") else "manipulation_failed",
        session_id=config["configurable"]["thread_id"],
        user_input=state["user_input"],
        generated_sql=state.get("generated_sql"),
        validation_result=state.get("validation_result"),
        preview=state.get("preview"),
        confirmed=state.get("confirmed"),
        row_count=(state.get("execution_result") or {}).get("row_count"),
        error=state.get("error"),
        retry_count=state.get("retry_count", 0),
        latency_seconds=round(time.time() - started_at, 3) if started_at else None,
    )
    return {}
