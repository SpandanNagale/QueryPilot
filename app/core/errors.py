"""Client-facing error taxonomy.

Every error that crosses the API boundary -- an SSE `error` event or an
`HTTPException` `detail` -- is shaped `{code, message, node}` (or a subset)
via this module. No raw exception text or traceback ever reaches a client;
`AppError.detail` exists for server-side logging only.

This is a different concern from `app/core/llm_errors.py`'s
`FallbackAction` classifier, which decides provider-fallback *behavior*
(retry/advance/no-advance) while a request is still being served internally.
`classify_exception_to_code` below sits on top of it, translating a
terminal, unrecoverable exception into the code a client sees.
"""
from __future__ import annotations

from enum import Enum

from app.core.llm_errors import AllProvidersFailedError, _is_timeout_or_connection_error, _status_code


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ErrorCode(str, Enum):
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    ALL_PROVIDERS_FAILED = "ALL_PROVIDERS_FAILED"
    GUARDRAIL_BLOCKED = "GUARDRAIL_BLOCKED"
    VALIDATION_BLOCKED = "VALIDATION_BLOCKED"
    ROW_CEILING_EXCEEDED = "ROW_CEILING_EXCEEDED"
    FK_BLOCKED = "FK_BLOCKED"
    SCHEMA_VALIDATION_FAILED = "SCHEMA_VALIDATION_FAILED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    SESSION_INVALID = "SESSION_INVALID"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    INTERNAL = "INTERNAL"


_CATALOG: dict[ErrorCode, tuple[str, Severity]] = {
    ErrorCode.PROVIDER_TIMEOUT: ("The AI provider took too long to respond.", Severity.WARNING),
    ErrorCode.PROVIDER_RATE_LIMIT: ("The AI provider is rate-limiting us right now.", Severity.WARNING),
    ErrorCode.PROVIDER_AUTH_FAILED: ("The AI provider rejected our credentials.", Severity.ERROR),
    ErrorCode.PROVIDER_UNAVAILABLE: ("The AI provider is currently unavailable.", Severity.WARNING),
    ErrorCode.ALL_PROVIDERS_FAILED: ("Every configured AI provider failed to respond. Please try again shortly.", Severity.ERROR),
    ErrorCode.GUARDRAIL_BLOCKED: ("Your message was blocked by the input safety filter.", Severity.WARNING),
    ErrorCode.VALIDATION_BLOCKED: ("The generated query didn't pass safety validation.", Severity.WARNING),
    ErrorCode.ROW_CEILING_EXCEEDED: ("This change would affect too many rows and was blocked.", Severity.WARNING),
    ErrorCode.FK_BLOCKED: ("This change is blocked by related rows in other tables.", Severity.WARNING),
    ErrorCode.SCHEMA_VALIDATION_FAILED: ("The generated query referenced an unknown table or column.", Severity.WARNING),
    ErrorCode.SESSION_EXPIRED: (
        "No pending confirmation for this session. It may have expired or already been resolved.",
        Severity.INFO,
    ),
    ErrorCode.SESSION_INVALID: ("session_id must be a valid UUID4.", Severity.ERROR),
    ErrorCode.RATE_LIMITED: ("Too many requests. Please slow down and try again shortly.", Severity.WARNING),
    ErrorCode.BUDGET_EXCEEDED: ("The daily usage budget has been reached. Please try again tomorrow.", Severity.ERROR),
    ErrorCode.INTERNAL: ("Something went wrong on our end.", Severity.ERROR),
}


class AppError(Exception):
    """Client-facing error. Construct with an `ErrorCode`; `.message` and
    `.severity` come from `_CATALOG`. `detail` (e.g. `str(original_exc)`) is
    for server-side logging only -- `to_dict()` never includes it."""

    def __init__(self, code: ErrorCode, *, detail: str | None = None, node: str | None = None):
        message, severity = _CATALOG[code]
        self.code = code
        self.message = message
        self.severity = severity
        self.detail = detail
        self.node = node
        super().__init__(detail or message)

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message, "node": self.node}


def classify_exception_to_code(exc: Exception) -> ErrorCode:
    """Maps a terminal exception escaping the graph/provider layer to a
    client-facing ErrorCode. `AllProvidersFailedError` (every deployment in
    an alias's fallback chain exhausted) takes priority; otherwise this
    falls back to the same status-code/timeout extraction
    `app.core.llm_errors.classify_exception` uses, for a raw provider
    exception surfacing before the fallback chain is exhausted. An exception
    with no recognizable provider shape (no timeout, no status code) maps to
    INTERNAL rather than guessing."""
    if isinstance(exc, AllProvidersFailedError):
        return ErrorCode.ALL_PROVIDERS_FAILED
    if _is_timeout_or_connection_error(exc):
        return ErrorCode.PROVIDER_TIMEOUT
    status = _status_code(exc)
    if status == 429:
        return ErrorCode.PROVIDER_RATE_LIMIT
    if status in (401, 403):
        return ErrorCode.PROVIDER_AUTH_FAILED
    if status is not None:
        return ErrorCode.PROVIDER_UNAVAILABLE
    return ErrorCode.INTERNAL


def classify_reason_to_code(reason: str) -> ErrorCode:
    """Categorizes an already-curated validator/guardrail reason string (per
    Global Constraints' substring rule) -- never call this on raw exception
    text. mentions of "row" + "ceiling"/"limit" -> ROW_CEILING_EXCEEDED;
    "cascade"/"fk" -> FK_BLOCKED; everything else -> VALIDATION_BLOCKED."""
    lowered = reason.lower()
    if "row" in lowered and ("ceiling" in lowered or "limit" in lowered):
        return ErrorCode.ROW_CEILING_EXCEEDED
    if "cascade" in lowered or "fk" in lowered:
        return ErrorCode.FK_BLOCKED
    return ErrorCode.VALIDATION_BLOCKED
