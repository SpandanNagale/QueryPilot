from __future__ import annotations

import time

from app.agent.prompts import IntentClassification, build_intent_classification_messages
from app.agent.state import AgentState
from app.core import llm_cache
from app.core.llm import LLMAlias, structured_chat


def intent_classifier_node(state: AgentState) -> dict:
    query_history = state.get("query_history", [])
    key = llm_cache.intent_classifier_key(state["user_input"], query_history)
    cached = llm_cache.lookup("intent_classifier", key)
    if cached is not llm_cache.MISS:
        return {"intent": cached}

    started = time.monotonic()
    result = structured_chat(
        alias=LLMAlias.CHAT,
        messages=build_intent_classification_messages(state["user_input"], query_history),
        schema=IntentClassification,
    )
    llm_cache.store(key, result.intent, latency_ms=(time.monotonic() - started) * 1000)
    return {"intent": result.intent}
