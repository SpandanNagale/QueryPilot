"""Regenerates SQL after a DB execution error, feeding the exact error and
failed SQL back to the generator. Part of the executor <-> self_corrector
loop, bounded by settings.max_self_correction_attempts."""
from __future__ import annotations

from app.agent.nodes.sql_generator import generate_sql_raw
from app.agent.state import AgentState
from app.core.validator import inject_limit_if_missing


def self_corrector_node(state: AgentState) -> dict:
    sql = generate_sql_raw(
        user_input=state["user_input"],
        relevant_schema=state.get("relevant_schema", ""),
        history=state.get("query_history", []),
        prior_error=f"SQL: {state['generated_sql']}\nError: {state.get('error')}",
    )
    sql = inject_limit_if_missing(sql)
    return {"generated_sql": sql}
