from __future__ import annotations

from app.agent.prompts import IntentClassification, build_intent_classification_messages
from app.agent.state import AgentState
from app.core.llm import LLMAlias, structured_chat


def intent_classifier_node(state: AgentState) -> dict:
    result = structured_chat(
        alias=LLMAlias.CHAT,
        messages=build_intent_classification_messages(state["user_input"], state.get("query_history", [])),
        schema=IntentClassification,
    )
    return {"intent": result.intent}
