# tests/test_request_context_buffer.py
"""Phase 4: the per-request event buffer app/main.py::_run_graph_stream's
producer thread uses to forward llm_attempt/cache_hit events into the SSE
stream (app/core/request_context.py)."""
from __future__ import annotations

from app.core import request_context


def test_push_event_before_start_is_a_safe_noop():
    request_context.push_event({"event_type": "llm_attempt"})  # no buffer armed -- must not raise
    assert request_context.drain_events() == []


def test_start_push_drain_round_trip():
    request_context.start_event_buffer()
    request_context.push_event({"event_type": "llm_attempt", "provider": "groq"})
    request_context.push_event({"event_type": "cache_hit", "node": "sql_generator"})

    drained = request_context.drain_events()

    assert drained == [
        {"event_type": "llm_attempt", "provider": "groq"},
        {"event_type": "cache_hit", "node": "sql_generator"},
    ]


def test_drain_clears_the_buffer():
    request_context.start_event_buffer()
    request_context.push_event({"event_type": "llm_attempt"})
    request_context.drain_events()

    assert request_context.drain_events() == []
