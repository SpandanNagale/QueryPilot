"""Provider-agnostic LLM gateway. Nodes ask for one of two aliases -- "sql_gen"
or "chat" -- never a literal model name or provider (app/core/llm_router.py
resolves an alias to config/model_groups.yaml's ordered deployment chain).
Every call here falls over to the next deployment on a provider failure,
classifying the exception via app/core/llm_errors.py and emitting an
llm_attempt event (app/core/llm_events.py) per attempt. Mirrors the fallback
semantics of the sibling Crewai-Research-pipeline repo's llm/router_llm.py,
adapted to call ollama/openai/google-genai clients directly instead of
wrapping everything in crewai.LLM.
"""
from __future__ import annotations

import time
from typing import Callable, Iterator, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.llm_errors import (
    AllProvidersFailedError,
    FallbackAction,
    LLMConfigError,
    StructuredOutputRepairFailed,
    classify_exception,
)
from app.core.llm_events import emit_llm_attempt
from app.core.llm_pricing import estimate_cost
from app.core.llm_providers import Deployment, build_deployment
from app.core.llm_router import check_providers, get_deployments

T = TypeVar("T", bound=BaseModel)


class LLMAlias:
    SQL_GEN = "sql_gen"
    CHAT = "chat"


_COOLDOWN_SECONDS = 600.0
_TRANSIENT_RETRY_BACKOFF_SECONDS = 1.0

_cooldown_until: dict[tuple[str, int], float] = {}


def _clear_cooldowns() -> None:
    """Test-only: reset cooldown state between tests."""
    _cooldown_until.clear()


def _ordered_deployments(alias: str) -> list[tuple[int, Deployment]]:
    deployments = get_deployments(alias)
    now = time.monotonic()
    available = [(i, d) for i, d in enumerate(deployments) if _cooldown_until.get((alias, i), 0.0) <= now]
    return available or list(enumerate(deployments))


def _record_attempt(alias: str, deployment: Deployment, *, latency_ms: float, success: bool, error: str = "") -> None:
    cost = None
    prompt_tokens = completion_tokens = None
    if success:
        prompt_tokens = deployment.last_usage.get("prompt_tokens")
        completion_tokens = deployment.last_usage.get("completion_tokens")
        cost = estimate_cost(deployment.provider_key, deployment.model, prompt_tokens, completion_tokens)
    emit_llm_attempt(
        alias=alias,
        provider=deployment.provider_key,
        model=deployment.model,
        latency_ms=latency_ms,
        success=success,
        error=error,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
    )


def byok_from_config(config: dict | None) -> dict | None:
    """Reads config["configurable"]["byok"] safely. config is optional on
    intent_classifier_node/sql_generator_node -- the startup cache pre-warm
    thread and existing tests call them with state only, no config."""
    if not config:
        return None
    return config.get("configurable", {}).get("byok")


def _byok_deployment(alias: str, byok: dict) -> Deployment:
    """One ad-hoc Deployment for a BYOK-covered call: reuses the model
    already configured for this alias+provider in config/model_groups.yaml
    (so BYOK swaps only the credential, not which model gets called), built
    fresh with the caller's own key via api_key_override -- never touches
    provider_credentials or the shared fallback chain's cached Deployments."""
    provider = byok["provider"]
    model = next((d.model for d in get_deployments(alias) if d.provider_key == provider), None)
    if model is None:
        raise LLMConfigError(f"BYOK provider {provider!r} is not configured for alias={alias!r}.")
    return build_deployment(provider, model, api_key_override=byok["api_key"])


def _call_byok(alias: str, byok: dict, fn: Callable[[Deployment], T]) -> T:
    """Single attempt against the caller's own key -- no fallback, no
    cooldown bookkeeping (this deployment isn't part of the shared chain).
    Still records an llm_attempt event (provider/model/cost only -- never
    the key) so budget/audit tooling sees the attempt like any other."""
    deployment = _byok_deployment(alias, byok)
    started = time.monotonic()
    try:
        result = fn(deployment)
    except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the caller to classify
        _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=False, error=str(exc))
        raise
    _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=True)
    return result


def _call_with_fallback(alias: str, fn: Callable[[Deployment], T]) -> T:
    last_error: Exception | None = None
    for index, deployment in _ordered_deployments(alias):
        for attempt in range(2):
            started = time.monotonic()
            try:
                result = fn(deployment)
            except StructuredOutputRepairFailed as exc:
                last_error = exc
                _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=False, error=str(exc))
                break  # advance to the next deployment -- no cooldown, this one isn't unhealthy
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                action = classify_exception(exc)
                last_error = exc
                _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=False, error=str(exc))
                if action is FallbackAction.NO_ADVANCE:
                    raise
                if action is FallbackAction.RETRY_THEN_ADVANCE and attempt == 0:
                    time.sleep(_TRANSIENT_RETRY_BACKOFF_SECONDS)
                    continue
                _cooldown_until[(alias, index)] = time.monotonic() + _COOLDOWN_SECONDS
                break
            else:
                _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=True)
                return result
    raise AllProvidersFailedError(alias, last_error)


def chat(*, alias: str, messages: list[dict[str, str]], temperature: float = 0.0, byok: dict | None = None) -> str:
    fn = lambda d: d.call(messages=messages, temperature=temperature)  # noqa: E731
    if byok is not None:
        return _call_byok(alias, byok, fn)
    return _call_with_fallback(alias, fn)


def _validate_or_repair(
    deployment: Deployment, messages: list[dict], temperature: float, schema: type[T], format: str | dict | None
) -> T:
    raw = deployment.call(messages=messages, temperature=temperature, format=format)
    try:
        return schema.model_validate_json(raw)
    except ValidationError as e:
        repair_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": f"That was not valid JSON matching the required schema. Error: {e}. "
                "Respond again with ONLY valid JSON matching the schema.",
            },
        ]
        raw2 = deployment.call(messages=repair_messages, temperature=temperature, format=format)
        try:
            return schema.model_validate_json(raw2)
        except ValidationError as e2:
            raise StructuredOutputRepairFailed(deployment.provider_key, deployment.model, str(e2)) from e2


def structured_chat(
    *, alias: str, messages: list[dict[str, str]], schema: type[T], temperature: float = 0.0, byok: dict | None = None
) -> T:
    def fn(deployment: Deployment) -> T:
        if deployment.structured_mode == "native_schema":
            return _validate_or_repair(deployment, messages, temperature, schema, format=schema.model_json_schema())
        json_messages = messages + [
            {"role": "system", "content": f"Respond with ONLY valid JSON matching this schema:\n{schema.model_json_schema()}"}
        ]
        fmt = "json" if deployment.structured_mode == "json_mode" else None
        return _validate_or_repair(deployment, json_messages, temperature, schema, format=fmt)

    if byok is not None:
        return _call_byok(alias, byok, fn)
    return _call_with_fallback(alias, fn)


def stream_chat(
    *, alias: str, messages: list[dict[str, str]], temperature: float = 0.0, byok: dict | None = None
) -> Iterator[str]:
    if byok is not None:
        deployment = _byok_deployment(alias, byok)
        started = time.monotonic()
        try:
            stream = deployment.stream(messages=messages, temperature=temperature)
            first_chunk = next(stream)
        except StopIteration:
            _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=True)
            return
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised (no fallback for BYOK)
            _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=False, error=str(exc))
            raise
        _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=True)
        yield first_chunk
        yield from stream
        return

    last_error: Exception | None = None
    for index, deployment in _ordered_deployments(alias):
        started = time.monotonic()
        try:
            stream = deployment.stream(messages=messages, temperature=temperature)
            first_chunk = next(stream)
        except StopIteration:
            _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=True)
            return
        except Exception as exc:  # noqa: BLE001
            action = classify_exception(exc)
            last_error = exc
            _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=False, error=str(exc))
            if action is FallbackAction.NO_ADVANCE:
                raise
            _cooldown_until[(alias, index)] = time.monotonic() + _COOLDOWN_SECONDS
            continue
        _record_attempt(alias, deployment, latency_ms=(time.monotonic() - started) * 1000, success=True)
        yield first_chunk
        yield from stream
        return
    raise AllProvidersFailedError(alias, last_error)
