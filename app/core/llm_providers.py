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
from app.core.llm_errors import EmptyProviderResponseError, LLMConfigError

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


def build_ollama_local(model: str, *, api_key_override: str | None = None) -> Deployment:
    # ollama_local has no API key concept (plain local host) -- override is a no-op.
    return _build_ollama("ollama_local", model, settings.ollama_host, headers={})


def build_ollama_cloud(model: str, *, api_key_override: str | None = None) -> Deployment:
    key = api_key_override or provider_credentials.ollama_api_key
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
        if not response.choices:
            raise EmptyProviderResponseError(f"{provider_key}:{model} returned a response with no choices.")
        return response.choices[0].message.content, _openai_usage(response)

    def _stream(*, messages, temperature):
        stream = client.chat.completions.create(model=model, messages=messages, temperature=temperature, stream=True)
        for chunk in stream:
            if not chunk.choices:  # some providers send a keep-alive/usage-only chunk with no choices
                continue
            content = chunk.choices[0].delta.content
            if content:
                yield content

    return Deployment(provider_key=provider_key, model=model, structured_mode="json_mode", _call=_call, _stream=_stream)


def build_openrouter(model: str, *, api_key_override: str | None = None) -> Deployment:
    key = api_key_override or provider_credentials.openrouter_api_key
    if not key:
        raise LLMConfigError(
            "provider=openrouter requires OPENROUTER_API_KEY to be set (get one at https://openrouter.ai/keys)."
        )
    return _build_openai_compatible("openrouter", model, provider_credentials.openrouter_base_url, key)


def build_groq(model: str, *, api_key_override: str | None = None) -> Deployment:
    key = api_key_override or provider_credentials.groq_api_key
    if not key:
        raise LLMConfigError(
            "provider=groq requires GROQ_API_KEY to be set (get one at https://console.groq.com/keys)."
        )
    return _build_openai_compatible("groq", model, provider_credentials.groq_base_url, key)


from google import genai
from google.genai import types


def _gemini_usage(response) -> dict:
    usage = {}
    meta = response.usage_metadata
    if meta is not None:
        if meta.prompt_token_count is not None:
            usage["prompt_tokens"] = meta.prompt_token_count
        if meta.candidates_token_count is not None:
            usage["completion_tokens"] = meta.candidates_token_count
    return usage


def _messages_to_gemini(messages: list[dict]) -> tuple[str | None, list]:
    system_instruction: str | None = None
    contents = []
    for msg in messages:
        if msg["role"] == "system":
            system_instruction = f"{system_instruction}\n{msg['content']}" if system_instruction else msg["content"]
            continue
        gemini_role = "model" if msg["role"] == "assistant" else "user"
        contents.append(types.Content(role=gemini_role, parts=[types.Part(text=msg["content"])]))
    return system_instruction, contents


def build_gemini(model: str, *, api_key_override: str | None = None) -> Deployment:
    key = api_key_override or provider_credentials.gemini_api_key
    if not key:
        raise LLMConfigError(
            "provider=gemini requires GEMINI_API_KEY to be set (get one at https://aistudio.google.com/apikey)."
        )
    client = genai.Client(api_key=key)

    def _call(*, messages, temperature, format=None):
        system_instruction, contents = _messages_to_gemini(messages)
        config_kwargs = {"temperature": temperature, "system_instruction": system_instruction}
        if format is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_json_schema"] = format
        response = client.models.generate_content(
            model=model, contents=contents, config=types.GenerateContentConfig(**config_kwargs)
        )
        return response.text, _gemini_usage(response)

    def _stream(*, messages, temperature):
        system_instruction, contents = _messages_to_gemini(messages)
        config = types.GenerateContentConfig(temperature=temperature, system_instruction=system_instruction)
        for chunk in client.models.generate_content_stream(model=model, contents=contents, config=config):
            if chunk.text:
                yield chunk.text

    return Deployment(provider_key="gemini", model=model, structured_mode="native_schema", _call=_call, _stream=_stream)


PROVIDER_BUILDERS: dict[str, Callable[..., Deployment]] = {
    "ollama_local": build_ollama_local,
    "ollama_cloud": build_ollama_cloud,
    "openrouter": build_openrouter,
    "groq": build_groq,
    "gemini": build_gemini,
}


def build_deployment(provider_key: str, model: str, *, api_key_override: str | None = None) -> Deployment:
    builder = PROVIDER_BUILDERS.get(provider_key)
    if builder is None:
        raise LLMConfigError(f"Unknown provider {provider_key!r}. Supported: {', '.join(PROVIDER_BUILDERS)}")
    return builder(model, api_key_override=api_key_override)
