from __future__ import annotations

from app.agent.state import AgentState
from app.core.validator import inject_limit_if_missing


def auto_limit_injector_node(state: AgentState) -> dict:
    """Analytics-only. Also resets retry_count: it was used for the
    generation-validation loop upstream, and the execution/self-correction
    loop downstream needs its own independent budget."""
    sql = inject_limit_if_missing(state["generated_sql"])
    return {"generated_sql": sql, "retry_count": 0, "error": None}
