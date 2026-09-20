"""One Deployment per {provider, model} entry in config/model_groups.yaml.
A Deployment wraps an already-configured, already-authenticated provider
client behind a uniform call()/stream() interface so app/core/llm.py's
fallback loop never needs to know which SDK it's talking to.

structured_mode tells app.core.llm.structured_chat() how to ask this
deployment for JSON:
  - "native_schema": pass the full pydantic JSON schema; the provider
    enforces it server-side (Ollama's `format=<schema>`, Gemini's
    response_json_schema).
  - "json_mode": the provider guarantees syntactically valid JSON but not
    schema conformance (OpenAI-compatible `response_format={"type":
    "json_object"}"), so the caller must also inject the schema into the
    prompt itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator, Literal

import openai
import ollama

from app.config import provider_credentials, settings
from app.core.llm_errors import LLMConfigError

StructuredMode = Literal["native_schema", "json_mode", "none"]


@dataclass
class Deployment:
    provider_key: str
    model: str
    structured_mode: StructuredMode
    _call: Callable[..., tuple[str, dict]]
    _stream: Callable[..., Iterator[str]]
    last_usage: dict = field(default_factory=dict)

    def call(self, *, messages: list[dict], temperature: float, format: str | dict | None = None) -> str:
        self.last_usage = {}
        text, usage = self._call(messages=messages, temperature=temperature, format=format)
        self.last_usage = usage
        return text

    def stream(self, *, messages: list[dict], temperature: float) -> Iterator[str]:
        self.last_usage = {}
        return self._stream(messages=messages, temperature=temperature)


def _ollama_usage(response) -> dict:
    usage = {}
    if response.get("prompt_eval_count") is not None:
        usage["prompt_tokens"] = response["prompt_eval_count"]
    if response.get("eval_count") is not None:
        usage["completion_tokens"] = response["eval_count"]
    return usage


def _build_ollama(provider_key: str, model: str, host: str, headers: dict) -> Deployment:
    client = ollama.Client(host=host, headers=headers, timeout=settings.llm_request_timeout_seconds)

    def _call(*, messages, temperature, format=None):
        response = client.chat(model=model, messages=messages, options={"temperature": temperature}, format=format)
        return response["message"]["content"], _ollama_usage(response)

    def _stream(*, messages, temperature):
        for chunk in client.chat(model=model, messages=messages, options={"temperature": temperature}, stream=True):
            content = chunk["message"]["content"]
            if content:
                yield content

    return Deployment(provider_key=provider_key, model=model, structured_mode="native_schema", _call=_call, _stream=_stream)


def build_ollama_local(model: str) -> Deployment:
    return _build_ollama("ollama_local", model, settings.ollama_host, headers={})


def build_ollama_cloud(model: str) -> Deployment:
    key = provider_credentials.ollama_api_key
    if not key:
        raise LLMConfigError(
            "provider=ollama_cloud requires OLLAMA_API_KEY to be set "
            "(get one at https://ollama.com after `ollama signin`)."
        )
    return _build_ollama(
        "ollama_cloud", model, provider_credentials.ollama_cloud_host, headers={"Authorization": f"Bearer {key}"}
    )


def _openai_usage(response) -> dict:
    usage = {}
    if response.usage is not None:
        usage["prompt_tokens"] = response.usage.prompt_tokens
        usage["completion_tokens"] = response.usage.completion_tokens
    return usage


def _build_openai_compatible(provider_key: str, model: str, base_url: str, api_key: str) -> Deployment:
    client = openai.OpenAI(base_url=base_url, api_key=api_key, timeout=settings.llm_request_timeout_seconds)

    def _call(*, messages, temperature, format=None):
        response_format = {"type": "json_object"} if format == "json" else None
        response = client.chat.completions.create(
            model=model, messages=messages, temperature=temperature, response_format=response_format
        )
        return response.choices[0].message.content, _openai_usage(response)

    def _stream(*, messages, temperature):
        stream = client.chat.completions.create(model=model, messages=messages, temperature=temperature, stream=True)
        for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                yield content

    return Deployment(provider_key=provider_key, model=model, structured_mode="json_mode", _call=_call, _stream=_stream)


def build_openrouter(model: str) -> Deployment:
    key = provider_credentials.openrouter_api_key
    if not key:
        raise LLMConfigError(
            "provider=openrouter requires OPENROUTER_API_KEY to be set (get one at https://openrouter.ai/keys)."
        )
    return _build_openai_compatible("openrouter", model, provider_credentials.openrouter_base_url, key)


def build_groq(model: str) -> Deployment:
    key = provider_credentials.groq_api_key
    if not key:
        raise LLMConfigError(
            "provider=groq requires GROQ_API_KEY to be set (get one at https://console.groq.com/keys)."
        )
    return _build_openai_compatible("groq", model, provider_credentials.groq_base_url, key)
