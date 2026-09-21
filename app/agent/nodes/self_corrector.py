"""Regenerates SQL after a DB execution error, feeding the exact error and
failed SQL back to the generator. Part of the executor <-> self_corrector
loop, bounded by settings.max_self_correction_attempts."""
from __future__ import annotations

from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agent.nodes.sql_generator import generate_sql_raw
from app.agent.state import AgentState
from app.core.llm import byok_from_config
from app.core.validator import inject_limit_if_missing


def self_corrector_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    # Optional[...], not "RunnableConfig | None" -- see
    # intent_classifier_node's identical comment on why (LangGraph's
    # config-injection signature check).
    #
    # This is the same request's retry loop after an execution error
    # (executor -> self_corrector -> executor again), not a separate
    # concern from sql_generator_node's own BYOK call -- a BYOK-covered
    # request must keep using the caller's own key here too, not silently
    # fall back to the shared/budgeted key for this one retry.
    byok = byok_from_config(config)
    sql = generate_sql_raw(
        user_input=state["user_input"],
        relevant_schema=state.get("relevant_schema", ""),
        history=state.get("query_history", []),
        prior_error=f"SQL: {state['generated_sql']}\nError: {state.get('error')}",
        byok=byok,
    )
    sql = inject_limit_if_missing(sql)
    return {"generated_sql": sql}
