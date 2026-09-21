# tests/test_langfuse_sink.py
"""Phase 4: optional Langfuse tracing. Unconfigured (the default, no keys
set) must be a complete no-op -- every public method callable without ever
touching the network or raising, so app/main.py can call LangfuseSink
unconditionally without an `is_configured()` check at every call site."""
from __future__ import annotations

from app.config import langfuse_settings
from app.core import langfuse_sink


def test_is_configured_false_when_keys_are_unset(monkeypatch):
    monkeypatch.setattr(langfuse_settings, "langfuse_public_key", "")
    monkeypatch.setattr(langfuse_settings, "langfuse_secret_key", "")
    assert langfuse_sink.is_configured() is False


def test_is_configured_true_when_both_keys_set(monkeypatch):
    monkeypatch.setattr(langfuse_settings, "langfuse_public_key", "pk-x")
    monkeypatch.setattr(langfuse_settings, "langfuse_secret_key", "sk-x")
    assert langfuse_sink.is_configured() is True


def test_unconfigured_sink_never_touches_the_network(monkeypatch):
    monkeypatch.setattr(langfuse_settings, "langfuse_public_key", "")
    monkeypatch.setattr(langfuse_settings, "langfuse_secret_key", "")
    # A real client is only ever constructed lazily inside _get_client();
    # never calling it here is the proof this stays fully local.
    monkeypatch.setattr(langfuse_sink, "_get_client", lambda: (_ for _ in ()).throw(AssertionError("network touched")))

    sink = langfuse_sink.LangfuseSink("run-1", session_id="sess-1", user_input="hi")
    sink.node_running("guardrail")
    sink.node_finished("guardrail", state="succeeded")
    sink.llm_attempt("sql_generator", {"provider": "groq", "model": "x", "success": True})
    sink.cache_hit("intent_classifier", {"seconds_saved": 0.1})
    sink.guardrail({"blocked": False})
    sink.run_finished(summary={"total_latency_ms": 1.0})


def test_get_trace_url_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(langfuse_settings, "langfuse_public_key", "")
    monkeypatch.setattr(langfuse_settings, "langfuse_secret_key", "")
    assert langfuse_sink.get_trace_url("run-1") is None


def test_sink_construction_failure_degrades_to_disabled_not_raising(monkeypatch):
    monkeypatch.setattr(langfuse_settings, "langfuse_public_key", "pk-x")
    monkeypatch.setattr(langfuse_settings, "langfuse_secret_key", "sk-x")

    class _BrokenClient:
        def start_observation(self, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(langfuse_sink, "_get_client", lambda: _BrokenClient())
    monkeypatch.setattr(langfuse_sink, "_trace_id_for", lambda run_id: "deadbeef")

    sink = langfuse_sink.LangfuseSink("run-1", session_id="sess-1", user_input="hi")

    assert sink._enabled is False
    sink.node_running("guardrail")  # must not raise even though construction failed
    sink.run_finished(summary={})
