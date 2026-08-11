"""Thin wrapper around the local Ollama client.

Centralizes: which model gets called for which purpose, structured-output
(JSON-schema constrained) decoding via Ollama's `format` parameter, and a
single place that turns "Ollama is unreachable" into a clear error instead
of a hang or a stack trace deep in a node.
"""
from __future__ import annotations

from typing import Any, TypeVar

import ollama
from pydantic import BaseModel

from app.config import settings

T = TypeVar("T", bound=BaseModel)

_client = ollama.Client(host=settings.ollama_host, timeout=settings.llm_request_timeout_seconds)


class OllamaUnavailableError(RuntimeError):
    """Raised when Ollama cannot be reached at all — distinct from a model
    producing a bad response, which callers should handle on its own terms."""


def check_ollama_available() -> tuple[bool, str]:
    try:
        _client.list()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - we want to report any failure reason to the user
        return False, str(exc)


def chat(
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    format: str | dict | None = None,
) -> str:
    """Single non-streaming chat call. Returns the assistant message content."""
    try:
        response = _client.chat(
            model=model,
            messages=messages,
            options={"temperature": temperature},
            format=format,
        )
    except Exception as exc:  # noqa: BLE001
        raise OllamaUnavailableError(
            f"Could not reach Ollama at {settings.ollama_host} (model={model}): {exc}"
        ) from exc
    return response["message"]["content"]


def structured_chat(*, model: str, messages: list[dict[str, str]], schema: type[T], temperature: float = 0.0) -> T:
    """Chat call constrained to a pydantic model's JSON schema via Ollama's
    structured-output support. Guarantees the response parses."""
    raw = chat(model=model, messages=messages, temperature=temperature, format=schema.model_json_schema())
    return schema.model_validate_json(raw)


def stream_chat(*, model: str, messages: list[dict[str, str]], temperature: float = 0.0):
    """Yields content chunks as they arrive, for SSE streaming to the client."""
    try:
        stream = _client.chat(
            model=model,
            messages=messages,
            options={"temperature": temperature},
            stream=True,
        )
        for chunk in stream:
            content = chunk["message"]["content"]
            if content:
                yield content
    except Exception as exc:  # noqa: BLE001
        raise OllamaUnavailableError(
            f"Could not reach Ollama at {settings.ollama_host} (model={model}): {exc}"
        ) from exc
