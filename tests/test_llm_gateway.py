"""Fallback-semantics tests for the gateway, using a FakeDeployment so the
tests exercise app.core.llm's own state machine (cooldown, retry-once,
advance) without needing real provider SDKs at all -- llm_providers.py and
llm_router.py already have their own dedicated tests for the SDK-facing and
config-loading parts."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.llm_errors import AllProvidersFailedError, FallbackAction


class FakeDeployment:
    def __init__(self, provider_key, script):
        """script: list of callables, one per call() invocation on this
        deployment, each either returning a str or raising an exception."""
        self.provider_key = provider_key
        self.model = "fake-model"
        self.structured_mode = "native_schema"
        self.last_usage = {}
        self._script = list(script)
        self.calls = 0

    def call(self, *, messages, temperature, format=None):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    from app.core import llm

    llm._clear_cooldowns()
    yield
    llm._clear_cooldowns()


def _patch_deployments(deployments):
    return patch("app.core.llm.get_deployments", return_value=deployments)


def test_chat_returns_first_deployment_result_on_success():
    from app.core.llm import chat

    good = FakeDeployment("groq", ["hello"])
    with _patch_deployments([good]):
        assert chat(alias="chat", messages=[{"role": "user", "content": "hi"}]) == "hello"
    assert good.calls == 1


def test_chat_advances_immediately_on_rate_limit_without_retry():
    from app.core.llm import chat

    class FakeRateLimit(Exception):
        pass

    first = FakeDeployment("groq", [FakeRateLimit("429")])
    second = FakeDeployment("gemini", ["ok from second"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        result = chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert result == "ok from second"
    assert first.calls == 1  # no retry -- advanced immediately


def test_chat_retries_once_on_transient_then_advances():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("timeout"), Exception("timeout again")])
    second = FakeDeployment("gemini", ["ok from second"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.RETRY_THEN_ADVANCE
    ):
        result = chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert result == "ok from second"
    assert first.calls == 2  # one retry on the same deployment before advancing


def test_chat_does_not_advance_on_no_advance_action():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("bad request")])
    second = FakeDeployment("gemini", ["should never be reached"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.NO_ADVANCE
    ):
        with pytest.raises(Exception, match="bad request"):
            chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert second.calls == 0


def test_chat_raises_typed_error_when_every_deployment_fails():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("down")])
    second = FakeDeployment("gemini", [Exception("also down")])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        with pytest.raises(AllProvidersFailedError) as exc_info:
            chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert exc_info.value.alias == "chat"


def test_chat_skips_a_deployment_still_in_cooldown():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("down"), "should not be called again"])
    second = FakeDeployment("gemini", ["from second", "from second again"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        chat(alias="chat", messages=[{"role": "user", "content": "hi"}])  # puts `first` on cooldown
        chat(alias="chat", messages=[{"role": "user", "content": "hi again"}])
    assert first.calls == 1  # never retried -- still in cooldown
    assert second.calls == 2


from pydantic import BaseModel


class _Choice(BaseModel):
    intent: str


def test_structured_chat_parses_valid_json_on_first_try():
    from app.core.llm import structured_chat

    good = FakeDeployment("ollama_cloud", ['{"intent": "analytics"}'])
    with _patch_deployments([good]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="analytics")
    assert good.calls == 1


def test_structured_chat_repairs_once_on_invalid_json_then_succeeds():
    from app.core.llm import structured_chat

    deployment = FakeDeployment("ollama_cloud", ["not json at all", '{"intent": "manipulation"}'])
    with _patch_deployments([deployment]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="manipulation")
    assert deployment.calls == 2  # original attempt + one repair retry


def test_structured_chat_advances_to_next_deployment_after_repair_also_fails():
    from app.core.llm import structured_chat

    bad = FakeDeployment("ollama_cloud", ["still not json", "still not json after repair"])
    good = FakeDeployment("gemini", ['{"intent": "off_topic"}'])
    with _patch_deployments([bad, good]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="off_topic")
    assert bad.calls == 2  # original + repair, both invalid -- advanced without a transport-level retry
    assert good.calls == 1


def test_structured_chat_json_mode_deployment_injects_schema_instruction():
    from app.core.llm import structured_chat

    deployment = FakeDeployment("groq", ['{"intent": "schema_question"}'])
    deployment.structured_mode = "json_mode"
    with _patch_deployments([deployment]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="schema_question")


class FakeStreamingDeployment(FakeDeployment):
    def __init__(self, provider_key, stream_script):
        """stream_script: list of either a list-of-chunks (success) or an
        Exception raised on the first next() call."""
        super().__init__(provider_key, [])
        self._stream_script = list(stream_script)

    def stream(self, *, messages, temperature):
        step = self._stream_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return iter(step)


def test_stream_chat_yields_all_chunks_from_the_first_working_deployment():
    from app.core.llm import stream_chat

    good = FakeStreamingDeployment("groq", [["he", "llo"]])
    with _patch_deployments([good]):
        chunks = list(stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}]))
    assert chunks == ["he", "llo"]


def test_stream_chat_falls_back_before_the_first_token():
    from app.core.llm import stream_chat

    class FakeConnErr(Exception):
        pass

    broken = FakeStreamingDeployment("groq", [FakeConnErr("connection refused")])
    good = FakeStreamingDeployment("gemini", [["from", " second"]])
    with _patch_deployments([broken, good]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        chunks = list(stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}]))
    assert chunks == ["from", " second"]


def test_stream_chat_raises_all_providers_failed_when_every_deployment_errors():
    from app.core.llm import stream_chat

    first = FakeStreamingDeployment("groq", [Exception("down")])
    second = FakeStreamingDeployment("gemini", [Exception("also down")])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        with pytest.raises(AllProvidersFailedError):
            list(stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}]))


# ---------------------------------------------------------------------------
# BYOK: byok=... bypasses _ordered_deployments/fallback entirely and calls a
# single ad-hoc Deployment built via app.core.llm_providers.build_deployment.
# ---------------------------------------------------------------------------

_BYOK_SECRET = "sk-byok-secret-should-never-be-logged"


def _patch_byok_deployment(monkeypatch, deployment):
    """_byok_deployment looks up the configured model for byok["provider"]
    via get_deployments(alias), then builds a fresh Deployment via
    build_deployment -- patch both so the test never touches a real
    provider client, and can assert api_key_override was forwarded."""
    monkeypatch.setattr("app.core.llm.get_deployments", lambda alias: [deployment])
    captured = {}

    def fake_build_deployment(provider_key, model, *, api_key_override=None):
        captured["provider_key"] = provider_key
        captured["model"] = model
        captured["api_key_override"] = api_key_override
        return deployment

    monkeypatch.setattr("app.core.llm.build_deployment", fake_build_deployment)
    return captured


def test_chat_byok_bypasses_fallback_and_uses_the_override_key(monkeypatch):
    from app.core.llm import chat

    deployment = FakeDeployment("gemini", ["byok response"])
    captured = _patch_byok_deployment(monkeypatch, deployment)

    result = chat(
        alias="chat",
        messages=[{"role": "user", "content": "hi"}],
        byok={"provider": "gemini", "api_key": _BYOK_SECRET},
    )

    assert result == "byok response"
    assert captured["provider_key"] == "gemini"
    assert captured["api_key_override"] == _BYOK_SECRET


def test_chat_byok_never_calls_ordered_deployments(monkeypatch):
    """The brief is explicit: BYOK bypasses _ordered_deployments/fallback
    entirely -- not just "falls through to it and it happens to work"."""
    from app.core.llm import chat

    deployment = FakeDeployment("gemini", ["byok response"])
    _patch_byok_deployment(monkeypatch, deployment)

    def _boom(alias):
        raise AssertionError("_ordered_deployments must not be called for a BYOK request")

    monkeypatch.setattr("app.core.llm._ordered_deployments", _boom)

    chat(alias="chat", messages=[{"role": "user", "content": "hi"}], byok={"provider": "gemini", "api_key": "k"})


def test_structured_chat_byok_uses_override_key():
    from app.core.llm import structured_chat

    with patch("app.core.llm.get_deployments", return_value=[FakeDeployment("gemini", [])]):
        captured = {}

        def fake_build_deployment(provider_key, model, *, api_key_override=None):
            captured["api_key_override"] = api_key_override
            return FakeDeployment("gemini", ['{"intent": "analytics"}'])

        with patch("app.core.llm.build_deployment", fake_build_deployment):
            result = structured_chat(
                alias="chat",
                messages=[{"role": "user", "content": "hi"}],
                schema=_Choice,
                byok={"provider": "gemini", "api_key": _BYOK_SECRET},
            )
    assert result == _Choice(intent="analytics")
    assert captured["api_key_override"] == _BYOK_SECRET


def test_stream_chat_byok_yields_chunks_from_the_override_deployment(monkeypatch):
    from app.core.llm import stream_chat

    deployment = FakeStreamingDeployment("gemini", [["by", "ok"]])
    _patch_byok_deployment(monkeypatch, deployment)

    chunks = list(
        stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], byok={"provider": "gemini", "api_key": "k"})
    )
    assert chunks == ["by", "ok"]


def test_byok_missing_provider_in_alias_chain_raises_config_error(monkeypatch):
    from app.core.llm import chat
    from app.core.llm_errors import LLMConfigError

    monkeypatch.setattr("app.core.llm.get_deployments", lambda alias: [FakeDeployment("groq", [])])
    with pytest.raises(LLMConfigError):
        chat(alias="chat", messages=[{"role": "user", "content": "hi"}], byok={"provider": "gemini", "api_key": "k"})


def test_byok_key_never_appears_in_emitted_llm_attempt_event(monkeypatch):
    """Never write the BYOK key into an llm_attempt event -- register a
    listener and inspect every field of every event it receives."""
    from app.core import llm_events
    from app.core.llm import chat

    deployment = FakeDeployment("gemini", ["byok response"])
    _patch_byok_deployment(monkeypatch, deployment)

    received = []
    llm_events.register_llm_attempt_listener(received.append)
    try:
        chat(
            alias="chat",
            messages=[{"role": "user", "content": "hi"}],
            byok={"provider": "gemini", "api_key": _BYOK_SECRET},
        )
    finally:
        llm_events.clear_llm_attempt_listeners()

    assert len(received) == 1
    import json

    assert _BYOK_SECRET not in json.dumps(received[0], default=str)
