# tests/test_node_state_events.py
"""Phase 4: node_state SSE events, the not_reached fan-out on a terminal
failure, the SSE heartbeat, and the per-run summary carried on "done".
Same pattern as test_main_errors.py -- fake graphs with a plain `.stream()`
generator, driving app.main._run_graph_stream directly."""
from __future__ import annotations

import time

import app.main as main_module


class _TwoNodeGraph:
    """guardrail -> intent_classifier, both succeeding."""

    def stream(self, *args, **kwargs):
        yield "tasks", {"id": "1", "name": "guardrail", "input": {}, "triggers": ()}
        yield "updates", {"guardrail": {"guardrail_verdict": {"blocked": False}}}
        yield "tasks", {
            "id": "1",
            "name": "guardrail",
            "error": None,
            "result": {"guardrail_verdict": {"blocked": False}},
            "interrupts": [],
        }
        yield "tasks", {"id": "2", "name": "intent_classifier", "input": {}, "triggers": ()}
        yield "updates", {"intent_classifier": {"intent": "analytics"}}
        yield "tasks", {
            "id": "2",
            "name": "intent_classifier",
            "error": None,
            "result": {"intent": "analytics"},
            "interrupts": [],
        }


class _GuardrailBlockedGraph:
    def stream(self, *args, **kwargs):
        yield "tasks", {"id": "1", "name": "guardrail", "input": {}, "triggers": ()}
        result = {"guardrail_verdict": {"blocked": True}, "error": "Guardrail blocked: prompt injection"}
        yield "updates", {"guardrail": result}
        yield "tasks", {"id": "1", "name": "guardrail", "error": None, "result": result, "interrupts": []}


class _DegradedSchemaRetrieverGraph:
    def stream(self, *args, **kwargs):
        yield "tasks", {"id": "1", "name": "schema_retriever", "input": {}, "triggers": ()}
        yield "updates", {"schema_retriever": {"relevant_schema": "full schema", "degraded": True}}
        yield "tasks", {
            "id": "1",
            "name": "schema_retriever",
            "error": None,
            "result": {"relevant_schema": "full schema", "degraded": True},
            "interrupts": [],
        }


class _SlowGraph:
    def stream(self, *args, **kwargs):
        time.sleep(0.3)
        yield "tasks", {"id": "1", "name": "guardrail", "input": {}, "triggers": ()}
        yield "updates", {"guardrail": {"guardrail_verdict": {"blocked": False}}}
        yield "tasks", {
            "id": "1",
            "name": "guardrail",
            "error": None,
            "result": {"guardrail_verdict": {"blocked": False}},
            "interrupts": [],
        }


def test_node_state_transitions_running_then_succeeded(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _TwoNodeGraph())
    body = "".join(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))

    assert 'event: node_state\ndata: {"node": "guardrail", "state": "running"}' in body
    assert 'event: node_state\ndata: {"node": "guardrail", "state": "succeeded"}' in body
    assert 'event: node_state\ndata: {"node": "intent_classifier", "state": "succeeded"}' in body


def test_guardrail_block_marks_downstream_nodes_not_reached(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _GuardrailBlockedGraph())
    body = "".join(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))

    assert 'data: {"node": "guardrail", "state": "failed"}' in body
    for node in ("intent_classifier", "schema_retriever", "sql_generator", "validator", "ambiguity_check"):
        assert f'data: {{"node": "{node}", "state": "not_reached"}}' in body


def test_schema_embedding_fallback_reports_degraded_node_state(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _DegradedSchemaRetrieverGraph())
    body = "".join(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))

    assert 'data: {"node": "schema_retriever", "state": "degraded"}' in body


def test_heartbeat_fires_while_a_node_call_blocks(monkeypatch):
    monkeypatch.setattr(main_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.1)
    monkeypatch.setitem(main_module._state, "graph", _SlowGraph())
    body = "".join(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))

    assert body.count("event: heartbeat") >= 1


def test_done_event_carries_a_run_summary(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _TwoNodeGraph())
    events = main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"})
    body = "".join(events)

    assert '"run_id"' in body
    assert '"total_latency_ms"' in body
    assert '"node_latencies_ms"' in body
    assert '"llm_attempts"' in body
    assert '"cache_hits"' in body
