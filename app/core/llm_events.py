"""Minimal pub/sub for llm_attempt events. The LLM gateway (app/core/llm.py)
emits one of these per provider attempt, win or lose. This phase only needs
the hook to exist and never crash a request -- Phase 4 registers a listener
here that forwards these into the SSE stream and, if configured, a Langfuse
generation. A sink failure must never break the request that triggered it,
so every listener call is isolated."""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

_listeners: list[Callable[[dict], None]] = []


def register_llm_attempt_listener(fn: Callable[[dict], None]) -> None:
    _listeners.append(fn)


def clear_llm_attempt_listeners() -> None:
    _listeners.clear()


def emit_llm_attempt(**fields) -> None:
    event = {"event_type": "llm_attempt", **fields}
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - a broken sink must never break the LLM call it's observing
            logger.exception("llm_attempt listener raised, dropping this delivery")
