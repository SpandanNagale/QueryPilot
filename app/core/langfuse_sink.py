"""Optional Langfuse tracing (Phase 4). Consumes the same per-node lifecycle
and llm_attempt/cache_hit signals app/main.py::_run_graph_stream already
computes for its SSE events, and turns them into one Langfuse trace per
graph run (one /chat or /confirm call), with one nested span per node and a
nested generation per llm_attempt. Mirrors the sibling
Crewai-Research-pipeline repo's observability/langfuse_sink.py, adapted to
QueryPilot's own event shapes instead of a shared RunEvent bus, and to a
simpler nested-span API (`span.start_observation(...)`) than that repo's
pinned langfuse version exposed.

A Langfuse outage must never break a request: every public method here is
wrapped so a failure degrades to "this trace/span/generation goes
unrecorded", never a broken response -- same contract as every other
observability sink in this app (app/core/llm_events.py's listener
isolation, app/core/audit.py's best-effort logging).
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from app.config import langfuse_settings

logger = logging.getLogger(__name__)

_client_lock = threading.Lock()
_client: Any = None


def is_configured() -> bool:
    return bool(langfuse_settings.langfuse_public_key and langfuse_settings.langfuse_secret_key)


def _get_client():
    """Lazy, process-wide singleton so the SDK's own batching/flush thread is reused."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                from langfuse import Langfuse

                _client = Langfuse(
                    public_key=langfuse_settings.langfuse_public_key,
                    secret_key=langfuse_settings.langfuse_secret_key,
                    host=langfuse_settings.langfuse_host,
                )
    return _client


def _trace_id_for(run_id: str) -> str:
    """Pure SHA-256 hash of run_id -- no client, no network call needed to
    recompute the same trace_id elsewhere (e.g. to build a trace URL)."""
    from langfuse import Langfuse

    return Langfuse.create_trace_id(seed=run_id)


def get_trace_url(run_id: str) -> str | None:
    if not is_configured():
        return None
    try:
        return _get_client().get_trace_url(trace_id=_trace_id_for(run_id))
    except Exception:  # noqa: BLE001 - a trace URL is a nice-to-have, never request-blocking
        logger.warning("Failed to build Langfuse trace URL for run %s", run_id, exc_info=True)
        return None


class LangfuseSink:
    """One instance per graph run (app/main.py::_run_graph_stream constructs
    a fresh one per /chat or /confirm call -- not a shared process-wide
    singleton). A no-op throughout when Langfuse isn't configured, so
    callers never need to check `is_configured()` themselves."""

    def __init__(self, run_id: str, *, session_id: str, user_input: str) -> None:
        self._enabled = is_configured()
        self._run_id = run_id
        self._root: Any = None
        self._node_spans: dict[str, Any] = {}
        if not self._enabled:
            return
        try:
            self._root = _get_client().start_observation(
                trace_context={"trace_id": _trace_id_for(run_id), "parent_span_id": None},
                name="chat_turn",
                as_type="span",
                input={"message": user_input},
                metadata={"session_id": session_id, "run_id": run_id},
            )
        except Exception:  # noqa: BLE001
            logger.warning("Failed to start Langfuse trace for run %s", run_id, exc_info=True)
            self._enabled = False

    def node_running(self, node: str) -> None:
        if not self._enabled:
            return
        try:
            self._node_spans[node] = self._root.start_observation(name=node, as_type="span")
        except Exception:  # noqa: BLE001
            logger.warning("LangfuseSink.node_running failed for node=%s", node, exc_info=True)

    def node_finished(self, node: str, *, state: str, detail: str | None = None) -> None:
        if not self._enabled:
            return
        span = self._node_spans.pop(node, None)
        if span is None:
            return
        try:
            level = "ERROR" if state == "failed" else ("WARNING" if state == "degraded" else "DEFAULT")
            span.update(output={"node_state": state}, level=level, status_message=detail)
            span.end()
        except Exception:  # noqa: BLE001
            logger.warning("LangfuseSink.node_finished failed for node=%s", node, exc_info=True)

    def llm_attempt(self, node: str, event: dict) -> None:
        if not self._enabled:
            return
        try:
            parent = self._node_spans.get(node) or self._root
            usage = {}
            if event.get("prompt_tokens") is not None:
                usage["input"] = event["prompt_tokens"]
            if event.get("completion_tokens") is not None:
                usage["output"] = event["completion_tokens"]
            cost = {"total": event["cost_usd"]} if event.get("cost_usd") is not None else None
            gen = parent.start_observation(
                name="llm_call",
                as_type="generation",
                model=event.get("model"),
                usage_details=usage or None,
                cost_details=cost,
                level="DEFAULT" if event.get("success") else "WARNING",
                status_message=event.get("error") or "ok",
                metadata={
                    "alias": event.get("alias"),
                    "provider": event.get("provider"),
                    "latency_ms": event.get("latency_ms"),
                },
            )
            gen.end()
        except Exception:  # noqa: BLE001
            logger.warning("LangfuseSink.llm_attempt failed for node=%s", node, exc_info=True)

    def cache_hit(self, node: str, event: dict) -> None:
        if not self._enabled:
            return
        try:
            parent = self._node_spans.get(node) or self._root
            parent.create_event(name="cache_hit", metadata=event)
        except Exception:  # noqa: BLE001
            logger.warning("LangfuseSink.cache_hit failed for node=%s", node, exc_info=True)

    def guardrail(self, event: dict) -> None:
        if not self._enabled:
            return
        try:
            self._root.create_event(name="guardrail", metadata=event)
        except Exception:  # noqa: BLE001
            logger.warning("LangfuseSink.guardrail failed for run %s", self._run_id, exc_info=True)

    def run_finished(self, *, summary: dict) -> None:
        if not self._enabled:
            return
        try:
            self._root.update(output=summary)
            self._root.end()
            _get_client().flush()
        except Exception:  # noqa: BLE001
            logger.warning("Failed to finish Langfuse trace for run %s", self._run_id, exc_info=True)
