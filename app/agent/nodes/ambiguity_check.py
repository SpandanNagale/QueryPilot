from __future__ import annotations

from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agent.prompts import AmbiguityCheck, build_ambiguity_check_messages
from app.agent.state import AgentState
from app.core.llm import LLMAlias, byok_from_config, structured_chat


def ambiguity_check_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    # Optional[...], not "RunnableConfig | None" -- see
    # intent_classifier_node's identical comment on why (LangGraph's
    # config-injection signature check).
    byok = byok_from_config(config)
    result = structured_chat(
        alias=LLMAlias.CHAT,
        messages=build_ambiguity_check_messages(
            state["user_input"],
            state["generated_sql"],
            state.get("relevant_schema", ""),
            state.get("query_history", []),
        ),
        schema=AmbiguityCheck,
        byok=byok,
    )
    ambiguity = {
        "is_ambiguous": result.is_ambiguous,
        "clarifying_question": result.clarifying_question,
        "assumptions": result.assumptions,
    }
    return {"ambiguity": ambiguity}


def route_after_ambiguity_check(state: AgentState) -> str:
    if state["ambiguity"]["is_ambiguous"]:
        return "ask_clarifying_question"
    if state.get("intent") == "manipulation":
        return "dry_run_preview"
    return "auto_limit_injector"
