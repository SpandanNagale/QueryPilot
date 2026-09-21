from __future__ import annotations

from app.agent.state import AgentState
from app.core.validator import inject_limit_if_missing


def auto_limit_injector_node(state: AgentState) -> dict:
    """Analytics-only. Also resets retry_count: it was used for the
    generation-validation loop upstream, and the execution/self-correction
    loop downstream needs its own independent budget."""
    original = state["generated_sql"]
    sql = inject_limit_if_missing(original)
    # "passed": true always -- this check never blocks a request, it just
    # records whether a LIMIT was actually added (vs. one already present).
    return {
        "generated_sql": sql,
        "retry_count": 0,
        "error": None,
        "validator_verdict": {"checks": [{"name": "auto_limit_injected", "passed": True, "injected": sql != original}]},
    }
