"""Rough per-attempt cost estimate for the llm_attempt event, so Phase 4's
run summary and Phase 3's budget kill-switch have a number to work with.
USD per 1,000,000 tokens (prompt, completion). Free-tier or not-yet-priced
deployments default to 0.0 rather than raising -- this is a best-effort
estimate, never a safety boundary. Confirm/refresh these against each
provider's pricing page during Task 12's live verification."""
from __future__ import annotations

_PRICE_PER_MILLION_TOKENS_USD: dict[tuple[str, str], tuple[float, float]] = {
    ("groq", "openai/gpt-oss-120b"): (0.15, 0.75),
    ("groq", "openai/gpt-oss-20b"): (0.05, 0.20),
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
