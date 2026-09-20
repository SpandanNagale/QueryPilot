"""LLM provider error taxonomy. classify_exception() maps any exception a
provider SDK can raise (openai's wrapped exceptions for OpenRouter/Groq;
raw httpx transport errors and each SDK's own status-code exception for
ollama/google-genai, which don't wrap transport errors the way openai does)
to one of three fallback actions. Adapted from the sibling
Crewai-Research-pipeline repo's llm/router_llm.py classify_provider_exception,
generalized to be SDK-agnostic via status-code extraction rather than
assuming every deployment is an openai.* client.
"""
from __future__ import annotations

from enum import Enum

import httpx
import openai


class LLMConfigError(RuntimeError):
    """A configured deployment is missing required config (e.g. an API key)."""


class AllProvidersFailedError(RuntimeError):
    """Every deployment in an alias's chain failed. `last_error` is the most
    recent underlying exception, for server-side logging only -- Phase 3
    maps this to ALL_PROVIDERS_FAILED before it ever reaches a client."""

    def __init__(self, alias: str, last_error: Exception | None):
        super().__init__(f"All deployments for alias={alias!r} failed. Last error: {last_error}")
        self.alias = alias
        self.last_error = last_error


class StructuredOutputRepairFailed(RuntimeError):
    """A deployment's structured-output response failed schema validation
    twice in a row (the original attempt plus one repair retry). The
    deployment itself isn't necessarily unhealthy -- callers should advance
    to the next deployment without putting this one on cooldown."""

    def __init__(self, provider_key: str, model: str, detail: str):
        super().__init__(f"{provider_key}:{model} failed structured-output validation twice: {detail}")
        self.provider_key = provider_key
        self.model = model


class FallbackAction(Enum):
    NO_ADVANCE = "no_advance"                    # malformed request -- fails identically on every deployment
    RETRY_THEN_ADVANCE = "retry_then_advance"     # transient -- retry once on the same deployment, then advance
    ADVANCE_IMMEDIATELY = "advance_immediately"   # deployment-specific failure -- advance without retrying


_NO_ADVANCE_STATUS_CODES = frozenset({400, 422})
_ADVANCE_IMMEDIATELY_STATUS_CODES = frozenset({401, 403, 404, 408, 413, 429, 503, 529})


def _status_code(exc: Exception) -> int | None:
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code
    code = getattr(exc, "code", None)  # google.genai.errors.APIError
    if isinstance(code, int):
        return code
    code = getattr(exc, "status_code", None)  # ollama.ResponseError
    if isinstance(code, int):
        return code
    return None


def _is_timeout_or_connection_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            openai.APITimeoutError,
            openai.APIConnectionError,
            httpx.TimeoutException,
            httpx.ConnectError,
            TimeoutError,
            ConnectionError,
        ),
    )


def classify_exception(exc: Exception) -> FallbackAction:
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return FallbackAction.NO_ADVANCE
    if _is_timeout_or_connection_error(exc):
        return FallbackAction.RETRY_THEN_ADVANCE
    if isinstance(exc, openai.InternalServerError):
        return FallbackAction.RETRY_THEN_ADVANCE
    if isinstance(
        exc,
        (openai.RateLimitError, openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError),
    ):
        return FallbackAction.ADVANCE_IMMEDIATELY

    status = _status_code(exc)
    if status in _NO_ADVANCE_STATUS_CODES:
        return FallbackAction.NO_ADVANCE
    if status in _ADVANCE_IMMEDIATELY_STATUS_CODES:
        return FallbackAction.ADVANCE_IMMEDIATELY
    if status is not None and 500 <= status < 600:
        return FallbackAction.RETRY_THEN_ADVANCE

    # Unrecognized exception shape: fail loudly rather than silently cycling
    # every deployment for an error class this gateway doesn't understand yet.
    return FallbackAction.NO_ADVANCE
