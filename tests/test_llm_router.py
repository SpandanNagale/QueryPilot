# tests/test_llm_router.py
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.core.llm_errors import LLMConfigError
from app.core.llm_router import check_providers, clear_deployment_cache, get_deployments


@pytest.fixture(autouse=True)
def _reset_router_state():
    clear_deployment_cache()
    yield
    clear_deployment_cache()


def test_cloud_profile_returns_ordered_chain_for_sql_gen():
    with patch("app.core.llm_providers.ollama.Client"), patch("app.core.llm_providers.openai.OpenAI"), patch(
        "app.core.llm_providers.genai.Client"
    ):
        from app.config import provider_credentials

        with patch.object(provider_credentials, "ollama_api_key", "k"), patch.object(
            provider_credentials, "openrouter_api_key", "k"
        ), patch.object(provider_credentials, "gemini_api_key", "k"), patch.object(
            provider_credentials, "groq_api_key", "k"
        ):
            deployments = get_deployments("sql_gen")
    assert [d.provider_key for d in deployments] == ["ollama_cloud", "openrouter", "gemini", "groq"]


def test_get_deployments_is_cached(monkeypatch):
    from app.config import provider_credentials

    monkeypatch.setattr(provider_credentials, "ollama_api_key", "k")
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "k")
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "k")
    monkeypatch.setattr(provider_credentials, "groq_api_key", "k")

    with patch("app.core.llm_providers.build_deployment") as mock_build:
        mock_build.side_effect = lambda provider, model: object()
        get_deployments("sql_gen")
        get_deployments("sql_gen")
        assert mock_build.call_count == 4  # one call per configured entry, not per get_deployments() call


def test_local_profile_returns_single_ollama_local_deployment(monkeypatch):
    monkeypatch.setattr(settings, "llm_profile", "local")
    with patch("app.core.llm_providers.ollama.Client"):
        deployments = get_deployments("chat")
    monkeypatch.setattr(settings, "llm_profile", "cloud")
    assert len(deployments) == 1
    assert deployments[0].provider_key == "ollama_local"


def test_unknown_alias_raises_config_error():
    with pytest.raises(LLMConfigError):
        get_deployments("not_a_real_alias")


def test_check_providers_reports_status_per_deployment():
    with patch("app.core.llm_router.get_deployments") as mock_get:
        ok_deployment = type(
            "D", (), {"provider_key": "groq", "model": "x", "call": lambda self, **kw: "pong"}
        )()
        mock_get.return_value = [ok_deployment]
        result = check_providers()
    assert result["sql_gen"][0]["status"] == "ok"
    assert result["chat"][0]["status"] == "ok"


def test_check_providers_reports_error_status_on_failure():
    with patch("app.core.llm_router.get_deployments") as mock_get:
        def _raise(**kw):
            raise RuntimeError("provider down")

        bad_deployment = type("D", (), {"provider_key": "groq", "model": "x", "call": lambda self, **kw: _raise(**kw)})()
        mock_get.return_value = [bad_deployment]
        result = check_providers()
    assert result["sql_gen"][0]["status"] == "error"
    assert "provider down" in result["sql_gen"][0]["detail"]


def test_check_providers_reports_config_error_without_raising():
    with patch("app.core.llm_router.get_deployments") as mock_get:
        mock_get.side_effect = LLMConfigError("missing key")
        result = check_providers()
    assert result["sql_gen"][0]["status"] == "error"
    assert "missing key" in result["sql_gen"][0]["detail"]
