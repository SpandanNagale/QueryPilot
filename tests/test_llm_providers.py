"""Each build_* function returns a Deployment whose .call()/.stream() delegate
to a real (here, mocked) provider client. Tests patch the client class each
builder module-level-imports, not the client instance, so we can assert on
how the client was constructed (host, headers, base_url) as well as how it
was called."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import provider_credentials
from app.core.llm_errors import EmptyProviderResponseError, LLMConfigError
from app.core.llm_providers import build_groq, build_ollama_cloud, build_ollama_local, build_openrouter


def test_ollama_local_call_returns_content_and_usage():
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        instance = MockClient.return_value
        instance.chat.return_value = {"message": {"content": "hi"}, "prompt_eval_count": 5, "eval_count": 3}

        deployment = build_ollama_local("qwen3:14b")
        text = deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0)

        assert text == "hi"
        assert deployment.last_usage == {"prompt_tokens": 5, "completion_tokens": 3}
        assert deployment.structured_mode == "native_schema"
        assert MockClient.call_args.kwargs["headers"] == {}


def test_ollama_local_stream_yields_nonempty_chunks_only():
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        MockClient.return_value.chat.return_value = iter(
            [
                {"message": {"content": "he"}},
                {"message": {"content": ""}},
                {"message": {"content": "llo"}},
            ]
        )
        deployment = build_ollama_local("qwen3:14b")
        chunks = list(deployment.stream(messages=[{"role": "user", "content": "hey"}], temperature=0.0))
        assert chunks == ["he", "llo"]


def test_ollama_cloud_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "ollama_api_key", "")
    with pytest.raises(LLMConfigError):
        build_ollama_cloud("gpt-oss:120b-cloud")


def test_ollama_cloud_passes_bearer_header_and_cloud_host(monkeypatch):
    monkeypatch.setattr(provider_credentials, "ollama_api_key", "test-key")
    monkeypatch.setattr(provider_credentials, "ollama_cloud_host", "https://ollama.com")
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        MockClient.return_value.chat.return_value = {"message": {"content": "ok"}}
        build_ollama_cloud("gpt-oss:120b-cloud")
        assert MockClient.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}
        assert MockClient.call_args.kwargs["host"] == "https://ollama.com"


def test_openrouter_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "")
    with pytest.raises(LLMConfigError):
        build_openrouter("some/free-model")


def test_openrouter_call_uses_json_object_response_format(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=4)
        MockClient.return_value.chat.completions.create.return_value = response

        deployment = build_openrouter("some/free-model")
        text = deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0, format="json")

        assert text == '{"ok": true}'
        assert deployment.last_usage == {"prompt_tokens": 10, "completion_tokens": 4}
        assert deployment.structured_mode == "json_mode"
        call_kwargs = MockClient.return_value.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_format"] == {"type": "json_object"}
        assert MockClient.call_args.kwargs["base_url"] == "https://openrouter.ai/api/v1"


def test_openrouter_call_without_format_omits_response_format(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="plain text"))]
        response.usage = None
        MockClient.return_value.chat.completions.create.return_value = response

        deployment = build_openrouter("some/free-model")
        deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0)

        call_kwargs = MockClient.return_value.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_format"] is None


def test_openrouter_call_with_no_choices_raises_empty_provider_response_error(monkeypatch):
    """Live-verified regression: a free-tier fallback model can return a
    well-formed response with response.choices=None (observed repeatedly
    against openrouter's nvidia/nemotron-3-super-120b-a12b:free) instead of
    an HTTP error the openai SDK would raise for. Before this fix, indexing
    response.choices[0] raised a raw, unclassified TypeError that aborted
    the entire fallback chain (app.core.llm_errors.classify_exception's
    NO_ADVANCE default) instead of advancing to the next deployment."""
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        response = MagicMock()
        response.choices = None
        MockClient.return_value.chat.completions.create.return_value = response

        deployment = build_openrouter("some/free-model")
        with pytest.raises(EmptyProviderResponseError):
            deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0)


def test_openrouter_stream_skips_chunks_with_no_choices(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        chunk1 = MagicMock(choices=[MagicMock(delta=MagicMock(content="he"))])
        keepalive_chunk = MagicMock(choices=None)
        chunk2 = MagicMock(choices=[MagicMock(delta=MagicMock(content="llo"))])
        MockClient.return_value.chat.completions.create.return_value = iter([chunk1, keepalive_chunk, chunk2])

        deployment = build_openrouter("some/free-model")
        chunks = list(deployment.stream(messages=[{"role": "user", "content": "hey"}], temperature=0.0))
        assert chunks == ["he", "llo"]


def test_groq_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "groq_api_key", "")
    with pytest.raises(LLMConfigError):
        build_groq("openai/gpt-oss-20b")


def test_groq_stream_yields_delta_content(monkeypatch):
    monkeypatch.setattr(provider_credentials, "groq_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        chunk1 = MagicMock(choices=[MagicMock(delta=MagicMock(content="he"))])
        chunk2 = MagicMock(choices=[MagicMock(delta=MagicMock(content=None))])
        chunk3 = MagicMock(choices=[MagicMock(delta=MagicMock(content="llo"))])
        MockClient.return_value.chat.completions.create.return_value = iter([chunk1, chunk2, chunk3])

        deployment = build_groq("openai/gpt-oss-20b")
        chunks = list(deployment.stream(messages=[{"role": "user", "content": "hey"}], temperature=0.0))
        assert chunks == ["he", "llo"]


from app.core.llm_providers import build_deployment, build_gemini


def test_gemini_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "")
    with pytest.raises(LLMConfigError):
        build_gemini("gemini-3.6-flash")


def test_gemini_call_extracts_text_and_usage(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "test-key")
    with patch("app.core.llm_providers.genai.Client") as MockClient:
        response = MagicMock()
        response.text = "hello"
        response.usage_metadata = MagicMock(prompt_token_count=7, candidates_token_count=2)
        MockClient.return_value.models.generate_content.return_value = response

        deployment = build_gemini("gemini-3.6-flash")
        text = deployment.call(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hey"}], temperature=0.0
        )

        assert text == "hello"
        assert deployment.last_usage == {"prompt_tokens": 7, "completion_tokens": 2}
        assert deployment.structured_mode == "native_schema"


def test_gemini_passes_json_schema_when_format_given(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "test-key")
    with patch("app.core.llm_providers.genai.Client") as MockClient:
        response = MagicMock()
        response.text = '{"intent": "analytics"}'
        response.usage_metadata = None
        MockClient.return_value.models.generate_content.return_value = response

        deployment = build_gemini("gemini-3.6-flash")
        deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0, format={"type": "object"})

        call_kwargs = MockClient.return_value.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema == {"type": "object"}


def test_build_deployment_dispatches_to_the_right_builder(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "test-key")
    with patch("app.core.llm_providers.genai.Client"):
        deployment = build_deployment("gemini", "gemini-3.6-flash")
        assert deployment.provider_key == "gemini"
        assert deployment.model == "gemini-3.6-flash"


def test_build_deployment_rejects_unknown_provider():
    with pytest.raises(LLMConfigError):
        build_deployment("not_a_real_provider", "some-model")


# ---------------------------------------------------------------------------
# BYOK: api_key_override threaded through build_deployment -> each builder.
# ---------------------------------------------------------------------------


def test_ollama_cloud_api_key_override_takes_precedence_over_configured_credential(monkeypatch):
    monkeypatch.setattr(provider_credentials, "ollama_api_key", "shared-key")
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        build_ollama_cloud("gpt-oss:120b-cloud", api_key_override="byok-key")
        assert MockClient.call_args.kwargs["headers"] == {"Authorization": "Bearer byok-key"}


def test_ollama_cloud_falls_back_to_configured_credential_when_no_override(monkeypatch):
    monkeypatch.setattr(provider_credentials, "ollama_api_key", "shared-key")
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        build_ollama_cloud("gpt-oss:120b-cloud", api_key_override=None)
        assert MockClient.call_args.kwargs["headers"] == {"Authorization": "Bearer shared-key"}


def test_openrouter_api_key_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "shared-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        build_openrouter("some/free-model", api_key_override="byok-key")
        assert MockClient.call_args.kwargs["api_key"] == "byok-key"


def test_groq_api_key_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(provider_credentials, "groq_api_key", "shared-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        build_groq("openai/gpt-oss-20b", api_key_override="byok-key")
        assert MockClient.call_args.kwargs["api_key"] == "byok-key"


def test_gemini_api_key_override_takes_precedence(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "shared-key")
    with patch("app.core.llm_providers.genai.Client") as MockClient:
        build_gemini("gemini-3.6-flash", api_key_override="byok-key")
        assert MockClient.call_args.kwargs["api_key"] == "byok-key"


def test_ollama_local_ignores_api_key_override_since_it_has_no_credential_concept():
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        build_ollama_local("qwen3:14b", api_key_override="byok-key")
        assert MockClient.call_args.kwargs["headers"] == {}  # no Authorization header at all


def test_build_deployment_threads_api_key_override_through(monkeypatch):
    with patch("app.core.llm_providers.genai.Client") as MockClient:
        build_deployment("gemini", "gemini-3.6-flash", api_key_override="byok-key")
        assert MockClient.call_args.kwargs["api_key"] == "byok-key"
