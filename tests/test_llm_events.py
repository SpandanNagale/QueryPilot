from __future__ import annotations

from app.core import llm_events


def test_emit_with_no_listeners_is_a_safe_noop():
    llm_events.clear_llm_attempt_listeners()
    llm_events.emit_llm_attempt(alias="chat", provider="groq", model="x", success=True)


def test_registered_listener_receives_the_event():
    llm_events.clear_llm_attempt_listeners()
    received = []
    llm_events.register_llm_attempt_listener(received.append)

    llm_events.emit_llm_attempt(alias="sql_gen", provider="gemini", model="gemini-3.6-flash", success=True, latency_ms=12.5)

    assert len(received) == 1
    assert received[0]["alias"] == "sql_gen"
    assert received[0]["provider"] == "gemini"
    assert received[0]["latency_ms"] == 12.5
    llm_events.clear_llm_attempt_listeners()


def test_a_raising_listener_does_not_break_emission_or_other_listeners():
    llm_events.clear_llm_attempt_listeners()
    received = []

    def bad_listener(event):
        raise RuntimeError("listener bug")

    llm_events.register_llm_attempt_listener(bad_listener)
    llm_events.register_llm_attempt_listener(received.append)

    llm_events.emit_llm_attempt(alias="chat", provider="groq", model="x", success=False, error="boom")

    assert len(received) == 1
    llm_events.clear_llm_attempt_listeners()
