"""SQL generation node. Turns a natural-language request + retrieved schema
into a single SQL statement. Never trusted on its own — every output passes
through app.core.validator before touching the database.
"""
from __future__ import annotations

import re

from app.agent.prompts import build_sql_generation_messages
from app.agent.state import AgentState
from app.config import settings
from app.core.llm import chat

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
) -> str:
    messages = build_sql_generation_messages(user_input, relevant_schema, history or [], prior_error)
    raw = chat(model=settings.sql_gen_model, messages=messages, temperature=0.0)
    return _clean_sql(raw)


def sql_generator_node(state: AgentState) -> dict:
    sql = generate_sql_raw(
        user_input=state["user_input"],
        relevant_schema=state.get("relevant_schema", ""),
        history=state.get("query_history", []),
        prior_error=state.get("error"),
    )
    return {"generated_sql": sql}
