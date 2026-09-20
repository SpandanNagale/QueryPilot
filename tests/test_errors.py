# tests/test_errors.py
from __future__ import annotations

import pytest

from app.core.errors import (
    _CATALOG,
    AppError,
    ErrorCode,
    Severity,
    classify_exception_to_code,
    classify_reason_to_code,
)
from app.core.llm_errors import AllProvidersFailedError


class _FakeStatusError(Exception):
    """Mimics ollama.ResponseError's shape: a plain exception with a
    `.status_code` int attribute -- what `llm_errors._status_code` looks
    for once it's ruled out `openai.APIStatusError` and google-genai's
    `.code`."""

    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_every_error_code_has_a_catalog_entry():
    for code in ErrorCode:
        message, severity = _CATALOG[code]  # KeyError if missing
        assert isinstance(message, str) and message
        assert isinstance(severity, Severity)


def test_app_error_to_dict_shape():
    err = AppError(ErrorCode.SESSION_INVALID, detail="raw db trace", node="validator")
    assert err.to_dict() == {
        "code": "SESSION_INVALID",
        "message": "session_id must be a valid UUID4.",
        "node": "validator",
    }


def test_app_error_detail_never_in_to_dict():
    err = AppError(ErrorCode.INTERNAL, detail="Traceback (most recent call last): secret db path /foo")
    assert "secret" not in str(err.to_dict())
    assert "foo" not in str(err.to_dict())


@pytest.mark.parametrize(
    "exc,expected",
    [
        (TimeoutError("timed out"), ErrorCode.PROVIDER_TIMEOUT),
        (ConnectionError("connection reset"), ErrorCode.PROVIDER_TIMEOUT),
        (_FakeStatusError(429), ErrorCode.PROVIDER_RATE_LIMIT),
        (_FakeStatusError(401), ErrorCode.PROVIDER_AUTH_FAILED),
        (_FakeStatusError(403), ErrorCode.PROVIDER_AUTH_FAILED),
        (_FakeStatusError(500), ErrorCode.PROVIDER_UNAVAILABLE),
        (AllProvidersFailedError("chat", RuntimeError("boom")), ErrorCode.ALL_PROVIDERS_FAILED),
        (ValueError("some totally unrelated bug"), ErrorCode.INTERNAL),
    ],
)
def test_classify_exception_to_code(exc, expected):
    assert classify_exception_to_code(exc) is expected


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("This would affect 500 rows, which exceeds the safety limit of 50.", ErrorCode.ROW_CEILING_EXCEEDED),
        ("Multiple statements in a single request are not allowed.", ErrorCode.VALIDATION_BLOCKED),
        ("Unknown table 'foo' — it does not exist in the database.", ErrorCode.VALIDATION_BLOCKED),
        ("Blocked: rows in child_table have a CASCADE reference.", ErrorCode.FK_BLOCKED),
        ("FK constraint would be violated.", ErrorCode.FK_BLOCKED),
        ("Guardrail blocked: Your message looks like a prompt-injection attempt.", ErrorCode.GUARDRAIL_BLOCKED),
    ],
)
def test_classify_reason_to_code(reason, expected):
    assert classify_reason_to_code(reason) is expected
