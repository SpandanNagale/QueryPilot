"""Rough per-attempt cost estimate for the llm_attempt event, so Phase 4's
run summary and Phase 3's budget kill-switch have a number to work with.
USD per 1,000,000 tokens (prompt, completion). Free-tier or not-yet-priced
deployments default to 0.0 rather than raising -- this is a best-effort
estimate, never a safety boundary.

Rates below were refreshed 2026-09-20 (Task 12 live verification) against
each provider's pricing page:
  - Groq: console.groq.com/docs/model/openai/gpt-oss-120b and .../20b
  - Gemini: ai.google.dev pricing page, gemini-3.x Flash tier (promotional
    rate through 2026-12-31; rises to $1.50/$7.50 on 2027-01-01 -- revisit
    this entry after that date)
Ollama Cloud (gpt-oss:120b, gpt-oss:20b) and OpenRouter's `:free`-suffixed
models are genuinely free-tier for the deployments this gateway actually
uses (confirmed live -- calls succeeded with no payment-required error) and
intentionally have no entry here, falling through to the 0.0 default."""
from __future__ import annotations

_PRICE_PER_MILLION_TOKENS_USD: dict[tuple[str, str], tuple[float, float]] = {
    ("groq", "openai/gpt-oss-120b"): (0.15, 0.60),
    ("groq", "openai/gpt-oss-20b"): (0.075, 0.30),
    ("gemini", "gemini-3.6-flash"): (0.75, 3.75),
}


def estimate_cost(
    provider_key: str, model: str, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    if prompt_tokens is None or completion_tokens is None:
        return None
    rates = _PRICE_PER_MILLION_TOKENS_USD.get((provider_key, model))
    if rates is None:
        return 0.0
    prompt_rate, completion_rate = rates
    return (prompt_tokens / 1_000_000) * prompt_rate + (completion_tokens / 1_000_000) * completion_rate
