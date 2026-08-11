from __future__ import annotations

from app.agent.nodes.result_formatting import rows_to_markdown
from app.agent.prompts import build_followup_messages
from app.agent.state import AgentState
from app.config import settings
from app.core.llm import chat


def followup_suggester_node(state: AgentState) -> dict:
    exec_result = state["execution_result"]
    markdown = rows_to_markdown(exec_result["columns"], exec_result["rows"])

    raw = chat(
        model=settings.chat_model,
        messages=build_followup_messages(state["user_input"], markdown),
        temperature=0.5,
    )
    suggestions = [line.strip("-•* ").strip() for line in raw.splitlines() if line.strip()]
    return {"followups": suggestions[:3]}
