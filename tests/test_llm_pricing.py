from __future__ import annotations

from app.core.llm_pricing import estimate_cost


def test_returns_none_when_token_counts_unavailable():
    assert estimate_cost("groq", "openai/gpt-oss-120b", None, None) is None
    assert estimate_cost("groq", "openai/gpt-oss-120b", 10, None) is None


def test_unknown_model_defaults_to_zero_cost():
    assert estimate_cost("ollama_cloud", "some-new-tag", 1000, 500) == 0.0


def test_known_model_computes_nonzero_cost():
    cost = estimate_cost("groq", "openai/gpt-oss-120b", 1_000_000, 1_000_000)
    assert cost is not None
    assert cost > 0
