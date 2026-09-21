from __future__ import annotations

from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agent.nodes.result_formatting import rows_to_markdown
from app.agent.prompts import build_followup_messages
from app.agent.state import AgentState
from app.core.llm import LLMAlias, byok_from_config, chat


def followup_suggester_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    # Optional[...], not "RunnableConfig | None" -- see
    # intent_classifier_node's identical comment on why (LangGraph's
    # config-injection signature check).
    byok = byok_from_config(config)
    exec_result = state["execution_result"]
    markdown = rows_to_markdown(exec_result["columns"], exec_result["rows"])

    raw = chat(
        alias=LLMAlias.CHAT,
        messages=build_followup_messages(state["user_input"], markdown),
        temperature=0.5,
        byok=byok,
    )
    suggestions = [line.strip("-•* ").strip() for line in raw.splitlines() if line.strip()]
    return {"followups": suggestions[:3]}
