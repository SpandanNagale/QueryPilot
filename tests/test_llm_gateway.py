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
