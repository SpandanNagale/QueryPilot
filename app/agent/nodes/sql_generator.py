"""SQL generation node. Turns a natural-language request + retrieved schema
into a single SQL statement. Never trusted on its own — every output passes
through app.core.validator before touching the database.
"""
from __future__ import annotations

import re
import time
from typing import Optional

from langchain_core.runnables import RunnableConfig

from app.agent.prompts import build_sql_generation_messages
from app.agent.state import AgentState
from app.core import llm_cache
from app.core.llm import LLMAlias, byok_from_config, chat

_FENCE_RE = re.compile(r"^```(?:sql)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _clean_sql(raw: str) -> str:
    text = _FENCE_RE.sub("", raw).strip()
    # models occasionally prefix with "A:" echoing the few-shot format
    text = re.sub(r"^(sql|a)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip().rstrip(";").strip() + ";"


def generate_sql_raw(
    user_input: str,
    relevant_schema: str,
    history: list | None = None,
    prior_error: str | None = None,
    byok: dict | None = None,
) -> str:
    messages = build_sql_generation_messages(user_input, relevant_schema, history or [], prior_error)
    raw = chat(alias=LLMAlias.SQL_GEN, messages=messages, temperature=0.0, byok=byok)
    return _clean_sql(raw)


def sql_generator_node(state: AgentState, config: Optional[RunnableConfig] = None) -> dict:
    # Optional[...], not "RunnableConfig | None" -- see intent_classifier_node's
    # identical comment on why (LangGraph's config-injection signature check).
    prior_error = state.get("error")
    query_history = state.get("query_history", [])
    relevant_schema = state.get("relevant_schema", "")
    # config is optional (see intent_classifier_node's identical comment).
    byok = byok_from_config(config)

    # Caching only applies to the fresh, first-attempt, non-BYOK call. A
    # non-None prior_error means this is validator_node's retry-on-failure
    # loop (route_after_validation -> back to "sql_generator") -- the cache
    # key doesn't account for prior_error, so looking it up here would
    # replay the exact same (already-rejected) SQL forever, and storing
    # here would poison future fresh lookups with a corrective-context
    # answer. A BYOK request bypasses the cache entirely, same reasoning as
    # intent_classifier_node: the caller's own key must answer THIS
    # request, not a cached answer from the shared budget's key (or leak a
    # BYOK answer into a later non-BYOK request's cache hit).
    key = None
    if prior_error is None and byok is None:
        key = llm_cache.sql_generator_key(state["user_input"], query_history, relevant_schema)
        cached = llm_cache.lookup("sql_generator", key)
        if cached is not llm_cache.MISS:
            return {"generated_sql": cached}

    started = time.monotonic()
    sql = generate_sql_raw(
        user_input=state["user_input"],
        relevant_schema=relevant_schema,
        history=query_history,
        prior_error=prior_error,
        byok=byok,
    )
    if key is not None:
        llm_cache.store(key, sql, latency_ms=(time.monotonic() - started) * 1000)
    return {"generated_sql": sql}
