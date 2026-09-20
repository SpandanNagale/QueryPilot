from __future__ import annotations

import time
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agent.prompts import IntentClassification, build_intent_classification_messages
from app.agent.state import AgentState
from app.core import llm_cache
from app.core.llm import LLMAlias, byok_from_config, structured_chat


def intent_classifier_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    # Optional[...], not "RunnableConfig | None": LangGraph's config-injection
    # signature check (langgraph._internal._runnable.RunnableCallable) only
    # recognizes the exact annotation spellings RunnableConfig/Optional[RunnableConfig]
    # (as strings, since this file has `from __future__ import annotations`)
    # -- PEP 604's "RunnableConfig | None" silently falls through to "not
    # injected" despite its own warning claiming otherwise. Verified via
    # langgraph._internal._runnable.KWARGS_CONFIG_KEYS.
    query_history = state.get("query_history", [])
    # config is optional here (not required) because the startup cache
    # pre-warm thread (app/main.py::_prewarm_llm_cache) and existing tests
    # call this node with state only, no LangGraph config -- see
    # app.core.llm.byok_from_config.
    byok = byok_from_config(config)

    # A BYOK request bypasses the cache entirely (miss AND store): the whole
    # point of BYOK is that the caller's own key answers THIS request, not a
    # cached answer a shared-budget key produced (or vice versa -- storing a
    # BYOK response would let a later non-BYOK request "spend" someone
    # else's key for free from the cache's point of view).
    key = None
    if byok is None:
        key = llm_cache.intent_classifier_key(state["user_input"], query_history)
        cached = llm_cache.lookup("intent_classifier", key)
        if cached is not llm_cache.MISS:
            return {"intent": cached}

    started = time.monotonic()
    result = structured_chat(
        alias=LLMAlias.CHAT,
        messages=build_intent_classification_messages(state["user_input"], query_history),
        schema=IntentClassification,
        byok=byok,
    )
    if key is not None:
        llm_cache.store(key, result.intent, latency_ms=(time.monotonic() - started) * 1000)
    return {"intent": result.intent}
