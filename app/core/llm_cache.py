"""Exact-match, in-memory, process-lifetime cache for intent_classifier's and
sql_generator's LLM outputs. This only ever replaces the LLM call itself --
callers still run the returned value through every downstream step exactly
as on a miss (sql_generator's caller still sends the SQL through
validate_sql / dry_run_preview / confirmation_gate unchanged). Never used
for insight_summarizer.

Storage is a bare module-level dict: Render's free tier is a single process
with no shared-cache-across-processes need, and nothing here asks for
persistence across restarts.

Cache key composition lives here (not duplicated per node): SHA-256 of the
normalized question, a hash of the conversation context (query_history), and
-- for sql_generator only -- a hash of the retrieved schema. intent_classifier
runs before schema retrieval, so its key omits that term entirely rather than
hashing an empty string in its place.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.config import settings
from app.core.llm_events import emit_cache_hit

MISS = object()  # distinct from any legitimate cached value (an intent string, a SQL string)


@dataclass
class _Entry:
    value: Any
    latency_ms: float  # latency of the original (miss) call -- best-effort seconds_saved on a hit


_cache: dict[str, _Entry] = {}


def _normalize(question: str) -> str:
    return " ".join(question.lower().split())


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _context_hash(query_history: list | None) -> str:
    # json.dumps over repr(): stable regardless of dict key ordering.
    # query_history entries are plain JSON-safe TypedDicts.
    return _sha256(json.dumps(query_history or [], sort_keys=True, default=str))


def intent_classifier_key(question: str, query_history: list | None) -> str:
    """intent_classifier runs before schema retrieval -- no schema term."""
    return _sha256(f"{_normalize(question)}|{_context_hash(query_history)}")


def sql_generator_key(question: str, query_history: list | None, relevant_schema: str) -> str:
    return _sha256(f"{_normalize(question)}|{_context_hash(query_history)}|{_sha256(relevant_schema or '')}")


def lookup(node: str, key: str) -> Any:
    """Returns the cached value on a hit, emitting a cache_hit event as a
    side effect; returns the MISS sentinel otherwise (a plain None would be
    ambiguous if a future cached type could legitimately be None)."""
    if not settings.llm_cache_enabled:
        return MISS
    entry = _cache.get(key)
    if entry is None:
        return MISS
    # provider/model/tokens_saved/cost_saved: not cheaply available here --
    # app.core.llm.chat()/structured_chat() don't return deployment metadata
    # to their callers (only app.core.llm's internal _record_attempt sees
    # it), and widening that public signature would ripple into
    # ambiguity_check.py too, which is out of scope for this cache. Left
    # best-effort None, same as the brief explicitly allows for seconds_saved.
    emit_cache_hit(
        node=node,
        tokens_saved=None,
        cost_saved=None,
        seconds_saved=(entry.latency_ms / 1000) if entry.latency_ms is not None else None,
    )
    return entry.value


def store(key: str, value: Any, *, latency_ms: float = 0.0) -> None:
    if not settings.llm_cache_enabled:
        return
    _cache[key] = _Entry(value=value, latency_ms=latency_ms)


def clear() -> None:
    """Test-only: reset cache state between tests."""
    _cache.clear()
