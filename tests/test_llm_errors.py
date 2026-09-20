"""classify_exception() must route every provider-SDK exception this gateway
can see (openai's wrapped exceptions for OpenRouter/Groq, plus raw httpx
transport errors as ollama/google-genai raise them directly) to the right
fallback action. See app/core/llm_errors.py for what each action means."""
from __future__ import annotations

import httpx
import httpx
import openai
import pytest

from app.core.llm_errors import FallbackAction, classify_exception


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://example.com")


def _api_status_error(status_code: int) -> openai.APIStatusError:
    response = httpx.Response(status_code, request=_request(), json={"error": {"message": "boom"}})
    return openai.APIStatusError("boom", response=response, body=None)


@pytest.mark.parametrize(
    "exc",
    [
        openai.APITimeoutError(request=_request()),
        openai.APIConnectionError(request=_request()),
        httpx.ConnectError("connection refused"),
        httpx.TimeoutException("timed out"),
    ],
)
def test_transient_errors_retry_then_advance(exc):
    assert classify_exception(exc) is FallbackAction.RETRY_THEN_ADVANCE


def test_openai_internal_server_error_is_transient():
    response = httpx.Response(500, request=_request(), json={"error": {"message": "boom"}})
    exc = openai.InternalServerError("boom", response=response, body=None)
    assert classify_exception(exc) is FallbackAction.RETRY_THEN_ADVANCE


@pytest.mark.parametrize(
    "exc_cls,status",
    [
        (openai.RateLimitError, 429),
        (openai.AuthenticationError, 401),
        (openai.PermissionDeniedError, 403),
        (openai.NotFoundError, 404),
    ],
)
def test_named_openai_exceptions_advance_immediately(exc_cls, status):
    response = httpx.Response(status, request=_request(), json={"error": {"message": "boom"}})
    exc = exc_cls("boom", response=response, body=None)
    assert classify_exception(exc) is FallbackAction.ADVANCE_IMMEDIATELY


@pytest.mark.parametrize("status_code", [408, 413, 503, 529])
def test_generic_advance_worthy_status_codes(status_code):
    assert classify_exception(_api_status_error(status_code)) is FallbackAction.ADVANCE_IMMEDIATELY


@pytest.mark.parametrize("status_code", [400, 422])
def test_malformed_request_codes_never_advance(status_code):
    assert classify_exception(_api_status_error(status_code)) is FallbackAction.NO_ADVANCE


def test_generic_5xx_status_is_transient():
    assert classify_exception(_api_status_error(500)) is FallbackAction.RETRY_THEN_ADVANCE


def test_ollama_response_error_uses_status_code_attribute():
    import ollama

    assert classify_exception(ollama.ResponseError("not found", 404)) is FallbackAction.ADVANCE_IMMEDIATELY


def test_gemini_client_error_uses_code_attribute():
    from google.genai import errors

    exc = errors.ClientError(code=429, response_json={"error": {"message": "rate limited"}})
    assert classify_exception(exc) is FallbackAction.ADVANCE_IMMEDIATELY


def test_unrecognized_exception_never_advances():
    assert classify_exception(ValueError("something unrelated")) is FallbackAction.NO_ADVANCE


def test_all_providers_failed_error_carries_alias_and_last_error():
    from app.core.llm_errors import AllProvidersFailedError

    original = RuntimeError("boom")
    exc = AllProvidersFailedError("sql_gen", original)
    assert exc.alias == "sql_gen"
    assert exc.last_error is original
    assert "sql_gen" in str(exc)
