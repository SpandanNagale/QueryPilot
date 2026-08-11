"""Wraps app.core.validator for the generation-time validation loop:
sql_generator -> validator -> (back to sql_generator on failure, up to
settings.max_generation_retries) -> ambiguity_check.
"""
from __future__ import annotations

from app.agent.state import AgentState
from app.config import settings
from app.core.validator import validate_sql


def validator_node(state: AgentState) -> dict:
    result = validate_sql(state["generated_sql"], intent=state.get("intent", "analytics"))
    update: dict = {"validation_result": result.to_dict()}
    if result.passed:
        update["error"] = None
    else:
        update["error"] = "; ".join(result.reasons)
        update["retry_count"] = state.get("retry_count", 0) + 1
    return update


def route_after_validation(state: AgentState) -> str:
    if state["validation_result"]["passed"]:
        return "ambiguity_check"
    if state.get("retry_count", 0) <= settings.max_generation_retries:
        return "sql_generator"
    return "error_response"
