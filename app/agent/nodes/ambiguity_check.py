from __future__ import annotations

from app.agent.prompts import AmbiguityCheck, build_ambiguity_check_messages
from app.agent.state import AgentState
from app.config import settings
from app.core.llm import structured_chat


def ambiguity_check_node(state: AgentState) -> dict:
    result = structured_chat(
        model=settings.chat_model,
        messages=build_ambiguity_check_messages(
            state["user_input"],
            state["generated_sql"],
            state.get("relevant_schema", ""),
            state.get("query_history", []),
        ),
        schema=AmbiguityCheck,
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
