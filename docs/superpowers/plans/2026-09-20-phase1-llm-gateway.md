# Phase 1: LLM Gateway with Ordered Fallback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `app/core/llm.py`'s single local-Ollama client with a provider-agnostic gateway that fans out across four providers (Ollama Cloud, OpenRouter, Gemini, Groq) per alias (`sql_gen`, `chat`), with conditional fallback, cooldown, structured-output repair retry, streaming, and per-attempt metrics — so the deployed backend never depends on a local Ollama server.

**Architecture:** A `Deployment` value object wraps one already-configured provider client behind a uniform `call()`/`stream()` interface. `app/core/llm_providers.py` builds one per `{provider, model}` config entry. `app/core/llm_router.py` loads `config/model_groups.yaml` and returns the ordered `Deployment` list for an alias (`sql_gen` or `chat`), swapping in a single `ollama_local` deployment when `QP_LLM_PROFILE=local`. `app/core/llm.py` is the public gateway (`chat()`, `structured_chat()`, `stream_chat()`, `check_providers()`) — it owns the fallback loop: classify the exception (`app/core/llm_errors.py`), retry once or advance to the next deployment with a cooldown, emit an `llm_attempt` event (`app/core/llm_events.py`) per attempt. Node code changes from `model=settings.sql_gen_model` to `alias=LLMAlias.SQL_GEN` and nothing else.

**Tech Stack:** `ollama` (Python client, upgraded to 0.6.2 for Bearer-header support), `openai` (OpenAI-compatible client for OpenRouter/Groq), `google-genai` (Gemini), `pyyaml` (config), `pytest` + `unittest.mock` for provider-mocked tests.

**Spec:** `C:\Users\shree\Downloads\Trash\querypilot-deploy-prompt.md`, "Phase 1: LLM gateway with ordered fallback" section. Adapted from the sibling repo `github.com/SpandanNagale/Crewai-Research-pipeline`'s `llm/router_llm.py`, `llm/provider.py`, `llm/router.py` (cloned locally to `../Crewai-Research-pipeline-ref` for reference) — that repo is CrewAI-based (its deployments are `crewai.LLM` objects with a `.call()` tool-loop built in); QueryPilot calls `ollama`/`openai`/`google-genai` clients directly, so the `Deployment` abstraction here is new, but the fallback state machine (cooldown dict, transient-retry-once, `FallbackAction` classification) mirrors `RouterLLM.call()` closely.

## Global Constraints

- `app/core/validator.py`, the row-impact ceiling, the FK-aware delete preview, and the confirmation gate are never touched by this phase — this phase only changes how SQL/chat text gets generated, not what happens to it afterward.
- Every test in `tests/test_guardrails.py` must still pass after every task (run `pytest` — it's fast, 40 tests, <1s. If a task's changes ever make it fail, that task is not done).
- No node's public behavior changes: `sql_generator_node`, `intent_classifier_node`, etc. still return the same shape of dict. Only the `model=` kwarg passed to `app.core.llm` functions changes.
- No raw exception text reaches an API response in this phase (there is no API-boundary error mapping yet — that's Phase 3 — but internal exceptions raised here must be typed, not bare strings, so Phase 3 has something to map).
- `.env` stays out of git. Every new env var goes in `.env.example` with a one-line comment.
- All new provider SDK calls (`ollama.Client`, `openai.OpenAI`, `google.genai.Client`) are verified against the **actually installed** package versions in this repo's `.venv` (`ollama==0.6.2`, `openai==3.16.2`, `google-genai==2.24.0`) — every method signature and exception constructor used below was confirmed live via `inspect.signature()` against those installed packages before this plan was written, not assumed from training knowledge. Task 12 does the one thing that still needs your input: a real network call with real keys to confirm exact model tags.

---

## File Structure

New files:
- `config/model_groups.yaml` — ordered `{provider, model}` chain per alias, plus a `local:` section for the `QP_LLM_PROFILE=local` dev override.
- `app/core/llm_errors.py` — `LLMConfigError`, `AllProvidersFailedError`, `StructuredOutputRepairFailed`, `FallbackAction` enum, `classify_exception()`.
- `app/core/llm_events.py` — minimal pub/sub so Phase 4 can later wire `llm_attempt` events into SSE/Langfuse without this phase building that infrastructure.
- `app/core/llm_pricing.py` — `estimate_cost()`, a small hardcoded USD-per-million-token table.
- `app/core/llm_providers.py` — `Deployment` dataclass + one builder function per provider (`build_ollama_local`, `build_ollama_cloud`, `build_openrouter`, `build_groq`, `build_gemini`) + `build_deployment(provider_key, model)` registry lookup.
- `app/core/llm_router.py` — loads `config/model_groups.yaml`, `get_deployments(alias)`, `check_providers()`.
- `tests/test_llm_errors.py`, `tests/test_llm_events.py`, `tests/test_llm_pricing.py`, `tests/test_llm_providers.py`, `tests/test_llm_router.py`, `tests/test_llm_gateway.py`
- `docs/phases/01-llm-gateway.md` — written last, in Task 12.

Modified files:
- `app/config.py` — add `ProviderCredentials` settings (unprefixed env vars: `OLLAMA_API_KEY`, `OLLAMA_CLOUD_HOST`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY`), add `Settings.llm_profile` (`QP_LLM_PROFILE`) and `Settings.model_groups_path`; remove `sql_gen_model`/`chat_model` (superseded by `config/model_groups.yaml`).
- `app/core/llm.py` — full rewrite: `chat()`, `structured_chat()`, `stream_chat()`, `check_providers()`, `LLMAlias`.
- `app/core/schema_store.py` — stop importing `_client` from `app.core.llm`; build its own local-Ollama client for embeddings (Phase 2b replaces this with Gemini embeddings; keeping it decoupled from the gateway now means Phase 1 doesn't have to preserve a private implementation detail for another module).
- `app/agent/nodes/sql_generator.py`, `intent_classifier.py`, `ambiguity_check.py`, `followup_suggester.py`, `insight_summarizer.py` — swap `model=settings.sql_gen_model` / `model=settings.chat_model` for `alias=LLMAlias.SQL_GEN` / `alias=LLMAlias.CHAT`.
- `app/main.py` — replace `check_ollama_available()` with `check_providers()`; add `GET /health/providers`; `/health` drops the Ollama-specific field (now just DB + `read_only_mode` — pinging every provider on every health check would be slow and burn quota, so `/health/providers` is the dedicated, slower endpoint).
- `requirements.txt` — bump `ollama` to `0.6.2`, bump `httpx` to `0.28.1`, add `openai==3.16.2`, `google-genai==2.24.0`, `pyyaml==6.0.3`.
- `.env.example` — add the new vars with comments.

---

### Task 1: Error taxonomy

**Files:**
- Create: `app/core/llm_errors.py`
- Test: `tests/test_llm_errors.py`

**Interfaces:**
- Produces: `LLMConfigError(RuntimeError)`, `AllProvidersFailedError(RuntimeError)` with `.alias`/`.last_error`, `StructuredOutputRepairFailed(RuntimeError)` with `.provider_key`/`.model`, `FallbackAction` enum (`NO_ADVANCE`, `RETRY_THEN_ADVANCE`, `ADVANCE_IMMEDIATELY`), `classify_exception(exc: Exception) -> FallbackAction`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_errors.py
"""classify_exception() must route every provider-SDK exception this gateway
can see (openai's wrapped exceptions for OpenRouter/Groq, plus raw httpx
transport errors as ollama/google-genai raise them directly) to the right
fallback action. See app/core/llm_errors.py for what each action means."""
from __future__ import annotations

import httpx
import httpx2
import openai
import pytest

from app.core.llm_errors import FallbackAction, classify_exception


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://example.com")


def _api_status_error(status_code: int) -> openai.APIStatusError:
    response = httpx2.Response(status_code, request=_request(), json={"error": {"message": "boom"}})
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
    response = httpx2.Response(500, request=_request(), json={"error": {"message": "boom"}})
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
    response = httpx2.Response(status, request=_request(), json={"error": {"message": "boom"}})
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.llm_errors'` (and `httpx2`/`google-genai` import errors if those packages aren't installed yet — install them first: `.venv/Scripts/python -m pip install "ollama==0.6.2" "openai==3.16.2" "google-genai==2.24.0" "httpx==0.28.1" "pyyaml==6.0.3"`)

- [ ] **Step 3: Write the implementation**

```python
# app/core/llm_errors.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_errors.py -v`
Expected: PASS, all cases green.

- [ ] **Step 5: Commit**

```bash
git add app/core/llm_errors.py tests/test_llm_errors.py
git commit -m "feat(llm): add provider-agnostic error taxonomy for the LLM gateway"
```

---

### Task 2: Event emission hook

**Files:**
- Create: `app/core/llm_events.py`
- Test: `tests/test_llm_events.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `emit_llm_attempt(**fields) -> None`, `register_llm_attempt_listener(fn: Callable[[dict], None]) -> None`, `clear_llm_attempt_listeners() -> None` (test/Phase-4-wiring helper). Phase 4 will register an SSE-forwarding listener here; this phase just needs the hook to exist and be safe to call with no listeners registered.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_events.py
from __future__ import annotations

from app.core import llm_events


def test_emit_with_no_listeners_is_a_safe_noop():
    llm_events.clear_llm_attempt_listeners()
    llm_events.emit_llm_attempt(alias="chat", provider="groq", model="x", success=True)


def test_registered_listener_receives_the_event():
    llm_events.clear_llm_attempt_listeners()
    received = []
    llm_events.register_llm_attempt_listener(received.append)

    llm_events.emit_llm_attempt(alias="sql_gen", provider="gemini", model="gemini-3.6-flash", success=True, latency_ms=12.5)

    assert len(received) == 1
    assert received[0]["alias"] == "sql_gen"
    assert received[0]["provider"] == "gemini"
    assert received[0]["latency_ms"] == 12.5
    llm_events.clear_llm_attempt_listeners()


def test_a_raising_listener_does_not_break_emission_or_other_listeners():
    llm_events.clear_llm_attempt_listeners()
    received = []

    def bad_listener(event):
        raise RuntimeError("listener bug")

    llm_events.register_llm_attempt_listener(bad_listener)
    llm_events.register_llm_attempt_listener(received.append)

    llm_events.emit_llm_attempt(alias="chat", provider="groq", model="x", success=False, error="boom")

    assert len(received) == 1
    llm_events.clear_llm_attempt_listeners()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_events.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.llm_events'`

- [ ] **Step 3: Write the implementation**

```python
# app/core/llm_events.py
"""Minimal pub/sub for llm_attempt events. The LLM gateway (app/core/llm.py)
emits one of these per provider attempt, win or lose. This phase only needs
the hook to exist and never crash a request -- Phase 4 registers a listener
here that forwards these into the SSE stream and, if configured, a Langfuse
generation. A sink failure must never break the request that triggered it,
so every listener call is isolated."""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

_listeners: list[Callable[[dict], None]] = []


def register_llm_attempt_listener(fn: Callable[[dict], None]) -> None:
    _listeners.append(fn)


def clear_llm_attempt_listeners() -> None:
    _listeners.clear()


def emit_llm_attempt(**fields) -> None:
    event = {"event_type": "llm_attempt", **fields}
    for listener in list(_listeners):
        try:
            listener(event)
        except Exception:  # noqa: BLE001 - a broken sink must never break the LLM call it's observing
            logger.exception("llm_attempt listener raised, dropping this delivery")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_events.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/llm_events.py tests/test_llm_events.py
git commit -m "feat(llm): add llm_attempt event pub/sub hook for the gateway"
```

---

### Task 3: Cost estimator

**Files:**
- Create: `app/core/llm_pricing.py`
- Test: `tests/test_llm_pricing.py`

**Interfaces:**
- Produces: `estimate_cost(provider_key: str, model: str, prompt_tokens: int | None, completion_tokens: int | None) -> float | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_pricing.py
from __future__ import annotations

from app.core.llm_pricing import estimate_cost


def test_returns_none_when_token_counts_unavailable():
    assert estimate_cost("groq", "openai/gpt-oss-120b", None, None) is None
    assert estimate_cost("groq", "openai/gpt-oss-120b", 10, None) is None


def test_unknown_model_defaults_to_zero_cost():
    assert estimate_cost("ollama_cloud", "some-new-tag", 1000, 500) == 0.0


def test_known_model_computes_nonzero_cost():
    cost = estimate_cost("groq", "openai/gpt-oss-120b", 1_000_000, 1_000_000)
    assert cost is not None
    assert cost > 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_pricing.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the implementation**

```python
# app/core/llm_pricing.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_pricing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/llm_pricing.py tests/test_llm_pricing.py
git commit -m "feat(llm): add per-attempt cost estimator"
```

---

### Task 4: Provider credentials + Deployment abstraction + Ollama providers

**Files:**
- Modify: `app/config.py`
- Create: `app/core/llm_providers.py`
- Modify: `.env.example`
- Test: `tests/test_llm_providers.py` (this task writes the Ollama-provider cases; Tasks 5 and 6 append to this same file)

**Interfaces:**
- Consumes: `app.core.llm_errors.LLMConfigError`
- Produces: `Deployment` dataclass with `.provider_key: str`, `.model: str`, `.structured_mode: Literal["native_schema","json_mode","none"]`, `.last_usage: dict`, `.call(*, messages, temperature, format=None) -> str`, `.stream(*, messages, temperature) -> Iterator[str]`. `build_ollama_local(model: str) -> Deployment`, `build_ollama_cloud(model: str) -> Deployment`. `app.config.provider_credentials` (a `ProviderCredentials` instance).

- [ ] **Step 1: Modify `app/config.py`**

Add a second settings class below the existing `Settings` (unprefixed env vars, per the spec's exact names), and update `Settings` itself:

```python
# app/config.py -- add these fields to Settings (replacing sql_gen_model/chat_model):
    # --- LLM gateway ---
    llm_profile: str = "cloud"   # "cloud" (default, deployed chain) or "local" (dev-only, forces ollama_local)
    model_groups_path: Path = PROJECT_ROOT / "config" / "model_groups.yaml"
    ollama_host: str = "http://localhost:11434"   # local Ollama: used by QP_LLM_PROFILE=local and by schema embeddings
    embed_model: str = "nomic-embed-text"
    llm_request_timeout_seconds: int = 60
```

Remove the old `sql_gen_model: str = "qwen2.5-coder:14b"` and `chat_model: str = "qwen3:14b"` fields (superseded by `config/model_groups.yaml`'s `local:` section). Then add, in the same file, below `settings = Settings()`:

```python
class ProviderCredentials(BaseSettings):
    """Cloud LLM provider credentials. Deliberately NOT under the QP_ prefix
    -- these are the exact env var names the deployment docs and Render
    blueprint use (OLLAMA_API_KEY, GEMINI_API_KEY, etc.), matching the
    sibling Crewai-Research-pipeline repo's config/settings.py convention."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    ollama_api_key: str = ""
    ollama_cloud_host: str = "https://ollama.com"
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    gemini_api_key: str = ""
    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"


provider_credentials = ProviderCredentials()
```

- [ ] **Step 2: Update `.env.example`**

```bash
# Copy to .env and adjust as needed. All QP_-prefixed vars are read by app/config.py's
# Settings; the provider credential vars below are unprefixed on purpose -- they're the
# exact names Render's blueprint (Phase 2d) and the deployment docs use.
QP_READ_ONLY_MODE=false
QP_ROW_IMPACT_CEILING=50
QP_AUTO_LIMIT=200
QP_CONFIRMATION_TIMEOUT_SECONDS=60

# QP_LLM_PROFILE=local forces every alias (sql_gen, chat) onto a single local
# Ollama deployment -- offline dev only. Leave unset (or "cloud") for the
# normal deployed provider chain in config/model_groups.yaml.
QP_LLM_PROFILE=cloud
QP_OLLAMA_HOST=http://localhost:11434
QP_EMBED_MODEL=nomic-embed-text

# --- Cloud LLM provider credentials (config/model_groups.yaml's deployed chain) ---
# Ollama Cloud -- primary provider for sql_gen and chat. Get a key after `ollama signin` at https://ollama.com
OLLAMA_API_KEY=
OLLAMA_CLOUD_HOST=https://ollama.com
# OpenRouter -- second fallback. Get a key at https://openrouter.ai/keys
OPENROUTER_API_KEY=
# Gemini -- third fallback (and Phase 2b's embedding provider). Get a key at https://aistudio.google.com/apikey
GEMINI_API_KEY=
# Groq -- last fallback, fast inference. Get a key at https://console.groq.com/keys
GROQ_API_KEY=
```

- [ ] **Step 3: Write the failing tests (Ollama providers)**

```python
# tests/test_llm_providers.py
"""Each build_* function returns a Deployment whose .call()/.stream() delegate
to a real (here, mocked) provider client. Tests patch the client class each
builder module-level-imports, not the client instance, so we can assert on
how the client was constructed (host, headers, base_url) as well as how it
was called."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.config import provider_credentials
from app.core.llm_errors import LLMConfigError
from app.core.llm_providers import build_ollama_cloud, build_ollama_local


def test_ollama_local_call_returns_content_and_usage():
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        instance = MockClient.return_value
        instance.chat.return_value = {"message": {"content": "hi"}, "prompt_eval_count": 5, "eval_count": 3}

        deployment = build_ollama_local("qwen3:14b")
        text = deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0)

        assert text == "hi"
        assert deployment.last_usage == {"prompt_tokens": 5, "completion_tokens": 3}
        assert deployment.structured_mode == "native_schema"
        assert MockClient.call_args.kwargs["headers"] == {}


def test_ollama_local_stream_yields_nonempty_chunks_only():
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        MockClient.return_value.chat.return_value = iter(
            [
                {"message": {"content": "he"}},
                {"message": {"content": ""}},
                {"message": {"content": "llo"}},
            ]
        )
        deployment = build_ollama_local("qwen3:14b")
        chunks = list(deployment.stream(messages=[{"role": "user", "content": "hey"}], temperature=0.0))
        assert chunks == ["he", "llo"]


def test_ollama_cloud_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "ollama_api_key", "")
    with pytest.raises(LLMConfigError):
        build_ollama_cloud("gpt-oss:120b-cloud")


def test_ollama_cloud_passes_bearer_header_and_cloud_host(monkeypatch):
    monkeypatch.setattr(provider_credentials, "ollama_api_key", "test-key")
    monkeypatch.setattr(provider_credentials, "ollama_cloud_host", "https://ollama.com")
    with patch("app.core.llm_providers.ollama.Client") as MockClient:
        MockClient.return_value.chat.return_value = {"message": {"content": "ok"}}
        build_ollama_cloud("gpt-oss:120b-cloud")
        assert MockClient.call_args.kwargs["headers"] == {"Authorization": "Bearer test-key"}
        assert MockClient.call_args.kwargs["host"] == "https://ollama.com"
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_providers.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.llm_providers'`

- [ ] **Step 5: Write the implementation**

```python
# app/core/llm_providers.py
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_providers.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Run the full suite and commit**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all existing + new tests pass.

```bash
git add app/config.py .env.example app/core/llm_providers.py tests/test_llm_providers.py
git commit -m "feat(llm): add provider credentials, Deployment abstraction, Ollama providers"
```

---

### Task 5: OpenAI-compatible providers (OpenRouter, Groq)

**Files:**
- Modify: `app/core/llm_providers.py`
- Modify: `tests/test_llm_providers.py` (append)

**Interfaces:**
- Produces: `build_openrouter(model: str) -> Deployment`, `build_groq(model: str) -> Deployment` (both `structured_mode="json_mode"`).

- [ ] **Step 1: Append the failing tests**

```python
# tests/test_llm_providers.py -- append
from app.core.llm_providers import build_groq, build_openrouter


def test_openrouter_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "")
    with pytest.raises(LLMConfigError):
        build_openrouter("some/free-model")


def test_openrouter_call_uses_json_object_response_format(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content='{"ok": true}'))]
        response.usage = MagicMock(prompt_tokens=10, completion_tokens=4)
        MockClient.return_value.chat.completions.create.return_value = response

        deployment = build_openrouter("some/free-model")
        text = deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0, format="json")

        assert text == '{"ok": true}'
        assert deployment.last_usage == {"prompt_tokens": 10, "completion_tokens": 4}
        assert deployment.structured_mode == "json_mode"
        call_kwargs = MockClient.return_value.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_format"] == {"type": "json_object"}
        assert MockClient.call_args.kwargs["base_url"] == "https://openrouter.ai/api/v1"


def test_openrouter_call_without_format_omits_response_format(monkeypatch):
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        response = MagicMock()
        response.choices = [MagicMock(message=MagicMock(content="plain text"))]
        response.usage = None
        MockClient.return_value.chat.completions.create.return_value = response

        deployment = build_openrouter("some/free-model")
        deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0)

        call_kwargs = MockClient.return_value.chat.completions.create.call_args.kwargs
        assert call_kwargs["response_format"] is None


def test_groq_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "groq_api_key", "")
    with pytest.raises(LLMConfigError):
        build_groq("openai/gpt-oss-20b")


def test_groq_stream_yields_delta_content(monkeypatch):
    monkeypatch.setattr(provider_credentials, "groq_api_key", "test-key")
    with patch("app.core.llm_providers.openai.OpenAI") as MockClient:
        chunk1 = MagicMock(choices=[MagicMock(delta=MagicMock(content="he"))])
        chunk2 = MagicMock(choices=[MagicMock(delta=MagicMock(content=None))])
        chunk3 = MagicMock(choices=[MagicMock(delta=MagicMock(content="llo"))])
        MockClient.return_value.chat.completions.create.return_value = iter([chunk1, chunk2, chunk3])

        deployment = build_groq("openai/gpt-oss-20b")
        chunks = list(deployment.stream(messages=[{"role": "user", "content": "hey"}], temperature=0.0))
        assert chunks == ["he", "llo"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_providers.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_groq'`

- [ ] **Step 3: Append the implementation**

```python
# app/core/llm_providers.py -- add import and append below the Ollama builders
import openai


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_providers.py -v`
Expected: PASS (9 tests total)

- [ ] **Step 5: Commit**

```bash
git add app/core/llm_providers.py tests/test_llm_providers.py
git commit -m "feat(llm): add OpenRouter and Groq providers"
```

---

### Task 6: Gemini provider + provider registry

**Files:**
- Modify: `app/core/llm_providers.py`
- Modify: `tests/test_llm_providers.py` (append)

**Interfaces:**
- Produces: `build_gemini(model: str) -> Deployment` (`structured_mode="native_schema"`), `PROVIDER_BUILDERS: dict[str, Callable[[str], Deployment]]`, `build_deployment(provider_key: str, model: str) -> Deployment`.

- [ ] **Step 1: Append the failing tests**

```python
# tests/test_llm_providers.py -- append
from app.core.llm_providers import build_deployment, build_gemini


def test_gemini_requires_api_key(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "")
    with pytest.raises(LLMConfigError):
        build_gemini("gemini-3.6-flash")


def test_gemini_call_extracts_text_and_usage(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "test-key")
    with patch("app.core.llm_providers.genai.Client") as MockClient:
        response = MagicMock()
        response.text = "hello"
        response.usage_metadata = MagicMock(prompt_token_count=7, candidates_token_count=2)
        MockClient.return_value.models.generate_content.return_value = response

        deployment = build_gemini("gemini-3.6-flash")
        text = deployment.call(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hey"}], temperature=0.0
        )

        assert text == "hello"
        assert deployment.last_usage == {"prompt_tokens": 7, "completion_tokens": 2}
        assert deployment.structured_mode == "native_schema"


def test_gemini_passes_json_schema_when_format_given(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "test-key")
    with patch("app.core.llm_providers.genai.Client") as MockClient:
        response = MagicMock()
        response.text = '{"intent": "analytics"}'
        response.usage_metadata = None
        MockClient.return_value.models.generate_content.return_value = response

        deployment = build_gemini("gemini-3.6-flash")
        deployment.call(messages=[{"role": "user", "content": "hey"}], temperature=0.0, format={"type": "object"})

        call_kwargs = MockClient.return_value.models.generate_content.call_args.kwargs
        config = call_kwargs["config"]
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema == {"type": "object"}


def test_build_deployment_dispatches_to_the_right_builder(monkeypatch):
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "test-key")
    with patch("app.core.llm_providers.genai.Client"):
        deployment = build_deployment("gemini", "gemini-3.6-flash")
        assert deployment.provider_key == "gemini"
        assert deployment.model == "gemini-3.6-flash"


def test_build_deployment_rejects_unknown_provider():
    with pytest.raises(LLMConfigError):
        build_deployment("not_a_real_provider", "some-model")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_providers.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_gemini'`

- [ ] **Step 3: Append the implementation**

```python
# app/core/llm_providers.py -- add import and append at the end of the file
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


def build_gemini(model: str) -> Deployment:
    key = provider_credentials.gemini_api_key
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


PROVIDER_BUILDERS: dict[str, Callable[[str], Deployment]] = {
    "ollama_local": build_ollama_local,
    "ollama_cloud": build_ollama_cloud,
    "openrouter": build_openrouter,
    "groq": build_groq,
    "gemini": build_gemini,
}


def build_deployment(provider_key: str, model: str) -> Deployment:
    builder = PROVIDER_BUILDERS.get(provider_key)
    if builder is None:
        raise LLMConfigError(f"Unknown provider {provider_key!r}. Supported: {', '.join(PROVIDER_BUILDERS)}")
    return builder(model)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_providers.py -v`
Expected: PASS (14 tests total)

- [ ] **Step 5: Run the full suite and commit**

Run: `.venv/Scripts/python -m pytest -q`

```bash
git add app/core/llm_providers.py tests/test_llm_providers.py
git commit -m "feat(llm): add Gemini provider and the provider registry"
```

---

### Task 7: Config file + router

**Files:**
- Create: `config/model_groups.yaml`
- Create: `app/core/llm_router.py`
- Test: `tests/test_llm_router.py`

**Interfaces:**
- Consumes: `app.core.llm_providers.build_deployment`, `app.config.settings.llm_profile`, `app.config.settings.model_groups_path`.
- Produces: `get_deployments(alias: str) -> list[Deployment]`, `check_providers() -> dict[str, list[dict]]`, `clear_deployment_cache()` (test helper).

- [ ] **Step 1: Create `config/model_groups.yaml`**

Starting models per the spec — marked `# TO LIVE-VERIFY` where Task 12 must confirm the exact tag against a real call:

```yaml
# Ordered fallback chain per alias. app/core/llm_router.py reads this file;
# node code never references a provider or model name directly, only an
# alias ("sql_gen" or "chat"). Entries under `local:` are used only when
# QP_LLM_PROFILE=local (offline dev against a local Ollama server).
#
# Models marked TO LIVE-VERIFY are the spec's suggested starting points --
# Task 12 confirms the exact tag each provider actually serves and updates
# this file, recording what changed in docs/phases/01-llm-gateway.md.

sql_gen:
  - provider: ollama_cloud
    model: qwen3-coder:480b-cloud  # TO LIVE-VERIFY
  - provider: openrouter
    model: qwen/qwen-2.5-coder-32b-instruct:free  # TO LIVE-VERIFY
  - provider: gemini
    model: gemini-3.6-flash  # TO LIVE-VERIFY
  - provider: groq
    model: openai/gpt-oss-120b  # TO LIVE-VERIFY

chat:
  - provider: ollama_cloud
    model: gpt-oss:120b-cloud  # TO LIVE-VERIFY
  - provider: openrouter
    model: meta-llama/llama-3.3-70b-instruct:free  # TO LIVE-VERIFY
  - provider: gemini
    model: gemini-3.6-flash  # TO LIVE-VERIFY
  - provider: groq
    model: openai/gpt-oss-20b  # TO LIVE-VERIFY

local:
  sql_gen:
    - provider: ollama_local
      model: qwen2.5-coder:14b
  chat:
    - provider: ollama_local
      model: qwen3:14b
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_llm_router.py
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.core.llm_errors import LLMConfigError
from app.core.llm_router import check_providers, clear_deployment_cache, get_deployments


@pytest.fixture(autouse=True)
def _reset_router_state():
    clear_deployment_cache()
    yield
    clear_deployment_cache()


def test_cloud_profile_returns_ordered_chain_for_sql_gen():
    with patch("app.core.llm_providers.ollama.Client"), patch("app.core.llm_providers.openai.OpenAI"), patch(
        "app.core.llm_providers.genai.Client"
    ):
        from app.config import provider_credentials

        with patch.object(provider_credentials, "ollama_api_key", "k"), patch.object(
            provider_credentials, "openrouter_api_key", "k"
        ), patch.object(provider_credentials, "gemini_api_key", "k"), patch.object(
            provider_credentials, "groq_api_key", "k"
        ):
            deployments = get_deployments("sql_gen")
    assert [d.provider_key for d in deployments] == ["ollama_cloud", "openrouter", "gemini", "groq"]


def test_get_deployments_is_cached(monkeypatch):
    from app.config import provider_credentials

    monkeypatch.setattr(provider_credentials, "ollama_api_key", "k")
    monkeypatch.setattr(provider_credentials, "openrouter_api_key", "k")
    monkeypatch.setattr(provider_credentials, "gemini_api_key", "k")
    monkeypatch.setattr(provider_credentials, "groq_api_key", "k")

    with patch("app.core.llm_providers.build_deployment") as mock_build:
        mock_build.side_effect = lambda provider, model: object()
        get_deployments("sql_gen")
        get_deployments("sql_gen")
        assert mock_build.call_count == 4  # one call per configured entry, not per get_deployments() call


def test_local_profile_returns_single_ollama_local_deployment(monkeypatch):
    monkeypatch.setattr(settings, "llm_profile", "local")
    with patch("app.core.llm_providers.ollama.Client"):
        deployments = get_deployments("chat")
    monkeypatch.setattr(settings, "llm_profile", "cloud")
    assert len(deployments) == 1
    assert deployments[0].provider_key == "ollama_local"


def test_unknown_alias_raises_config_error():
    with pytest.raises(LLMConfigError):
        get_deployments("not_a_real_alias")


def test_check_providers_reports_status_per_deployment():
    with patch("app.core.llm_router.get_deployments") as mock_get:
        ok_deployment = type(
            "D", (), {"provider_key": "groq", "model": "x", "call": lambda self, **kw: "pong"}
        )()
        mock_get.return_value = [ok_deployment]
        result = check_providers()
    assert result["sql_gen"][0]["status"] == "ok"
    assert result["chat"][0]["status"] == "ok"


def test_check_providers_reports_error_status_on_failure():
    with patch("app.core.llm_router.get_deployments") as mock_get:
        def _raise(**kw):
            raise RuntimeError("provider down")

        bad_deployment = type("D", (), {"provider_key": "groq", "model": "x", "call": lambda self, **kw: _raise(**kw)})()
        mock_get.return_value = [bad_deployment]
        result = check_providers()
    assert result["sql_gen"][0]["status"] == "error"
    assert "provider down" in result["sql_gen"][0]["detail"]


def test_check_providers_reports_config_error_without_raising():
    with patch("app.core.llm_router.get_deployments") as mock_get:
        mock_get.side_effect = LLMConfigError("missing key")
        result = check_providers()
    assert result["sql_gen"][0]["status"] == "error"
    assert "missing key" in result["sql_gen"][0]["detail"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.core.llm_router'`

- [ ] **Step 4: Write the implementation**

```python
# app/core/llm_router.py
"""Loads config/model_groups.yaml and returns the ordered Deployment chain
for an alias ("sql_gen" or "chat"). Node code never sees a provider or model
name -- only an alias, resolved here. QP_LLM_PROFILE=local swaps in the
single ollama_local deployment from the config's `local:` section instead of
the deployed chain, for offline dev."""
from __future__ import annotations

import threading
import time
from typing import List

import yaml

from app.config import settings
from app.core.llm_errors import LLMConfigError
from app.core.llm_providers import Deployment, build_deployment

_ALIASES = ("sql_gen", "chat")

_config_lock = threading.Lock()
_config_cache: dict | None = None

_deployment_lock = threading.Lock()
_deployment_cache: dict[str, List[Deployment]] = {}


def _load_config() -> dict:
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            with open(settings.model_groups_path) as f:
                _config_cache = yaml.safe_load(f) or {}
        return _config_cache


def clear_deployment_cache() -> None:
    """Test-only: force the next get_deployments() call to rebuild from disk."""
    global _config_cache
    with _config_lock:
        _config_cache = None
    with _deployment_lock:
        _deployment_cache.clear()


def _group_for_alias(alias: str) -> list[dict]:
    config = _load_config()
    group = config.get("local", {}).get(alias) if settings.llm_profile == "local" else config.get(alias)
    if not group:
        raise LLMConfigError(f"No model_groups entry for alias={alias!r} (profile={settings.llm_profile!r}).")
    return group


def get_deployments(alias: str) -> List[Deployment]:
    with _deployment_lock:
        cache_key = f"{settings.llm_profile}:{alias}"
        if cache_key in _deployment_cache:
            return _deployment_cache[cache_key]

        group = _group_for_alias(alias)
        deployments = [build_deployment(entry["provider"], entry["model"]) for entry in group]
        _deployment_cache[cache_key] = deployments
        return deployments


def check_providers() -> dict[str, list[dict]]:
    """Pings every deployment of every alias with one short call. Computed
    fresh each call -- no caching, so it reflects the current cooldown-free
    reality. GET /health/providers is the only caller; keep pings cheap."""
    results: dict[str, list[dict]] = {}
    for alias in _ALIASES:
        entries: list[dict] = []
        try:
            deployments = get_deployments(alias)
        except LLMConfigError as e:
            results[alias] = [{"provider": None, "model": None, "status": "error", "detail": str(e)}]
            continue

        for deployment in deployments:
            started = time.monotonic()
            try:
                deployment.call(messages=[{"role": "user", "content": "Reply with the single word: pong"}], temperature=0.0)
                entries.append(
                    {
                        "provider": deployment.provider_key,
                        "model": deployment.model,
                        "status": "ok",
                        "detail": "",
                        "latency_ms": (time.monotonic() - started) * 1000,
                    }
                )
            except Exception as e:  # noqa: BLE001 - health probe reports any failure reason
                entries.append(
                    {"provider": deployment.provider_key, "model": deployment.model, "status": "error", "detail": str(e)}
                )
        results[alias] = entries
    return results
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_router.py -v`
Expected: PASS

- [ ] **Step 6: Run the full suite and commit**

Run: `.venv/Scripts/python -m pytest -q`

```bash
git add config/model_groups.yaml app/core/llm_router.py tests/test_llm_router.py
git commit -m "feat(llm): add model_groups.yaml and the alias router"
```

---

### Task 8: Gateway core — `chat()` and the fallback loop

**Files:**
- Create: `app/core/llm.py` (replaces the old file entirely)
- Test: `tests/test_llm_gateway.py` (this task writes `chat()` cases; Tasks 9–10 append `structured_chat()`/`stream_chat()` cases to the same file)

**Interfaces:**
- Consumes: `app.core.llm_router.get_deployments`, `app.core.llm_errors.{classify_exception, FallbackAction, AllProvidersFailedError, StructuredOutputRepairFailed}`, `app.core.llm_events.emit_llm_attempt`, `app.core.llm_pricing.estimate_cost`.
- Produces: `LLMAlias` (`.SQL_GEN = "sql_gen"`, `.CHAT = "chat"`), `chat(*, alias: str, messages: list[dict], temperature: float = 0.0) -> str`, `_call_with_fallback` (internal, used by Tasks 9–10 too), `check_providers` (re-exported from `llm_router` so node/main.py code has one import path: `from app.core.llm import check_providers`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm_gateway.py
"""Fallback-semantics tests for the gateway, using a FakeDeployment so the
tests exercise app.core.llm's own state machine (cooldown, retry-once,
advance) without needing real provider SDKs at all -- llm_providers.py and
llm_router.py already have their own dedicated tests for the SDK-facing and
config-loading parts."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.llm_errors import AllProvidersFailedError, FallbackAction


class FakeDeployment:
    def __init__(self, provider_key, script):
        """script: list of callables, one per call() invocation on this
        deployment, each either returning a str or raising an exception."""
        self.provider_key = provider_key
        self.model = "fake-model"
        self.structured_mode = "native_schema"
        self.last_usage = {}
        self._script = list(script)
        self.calls = 0

    def call(self, *, messages, temperature, format=None):
        self.calls += 1
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture(autouse=True)
def _reset_gateway_state():
    from app.core import llm

    llm._clear_cooldowns()
    yield
    llm._clear_cooldowns()


def _patch_deployments(deployments):
    return patch("app.core.llm.get_deployments", return_value=deployments)


def test_chat_returns_first_deployment_result_on_success():
    from app.core.llm import chat

    good = FakeDeployment("groq", ["hello"])
    with _patch_deployments([good]):
        assert chat(alias="chat", messages=[{"role": "user", "content": "hi"}]) == "hello"
    assert good.calls == 1


def test_chat_advances_immediately_on_rate_limit_without_retry():
    from app.core.llm import chat

    class FakeRateLimit(Exception):
        pass

    first = FakeDeployment("groq", [FakeRateLimit("429")])
    second = FakeDeployment("gemini", ["ok from second"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        result = chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert result == "ok from second"
    assert first.calls == 1  # no retry -- advanced immediately


def test_chat_retries_once_on_transient_then_advances():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("timeout"), Exception("timeout again")])
    second = FakeDeployment("gemini", ["ok from second"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.RETRY_THEN_ADVANCE
    ):
        result = chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert result == "ok from second"
    assert first.calls == 2  # one retry on the same deployment before advancing


def test_chat_does_not_advance_on_no_advance_action():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("bad request")])
    second = FakeDeployment("gemini", ["should never be reached"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.NO_ADVANCE
    ):
        with pytest.raises(Exception, match="bad request"):
            chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert second.calls == 0


def test_chat_raises_typed_error_when_every_deployment_fails():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("down")])
    second = FakeDeployment("gemini", [Exception("also down")])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        with pytest.raises(AllProvidersFailedError) as exc_info:
            chat(alias="chat", messages=[{"role": "user", "content": "hi"}])
    assert exc_info.value.alias == "chat"


def test_chat_skips_a_deployment_still_in_cooldown():
    from app.core.llm import chat

    first = FakeDeployment("groq", [Exception("down"), "should not be called again"])
    second = FakeDeployment("gemini", ["from second", "from second again"])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        chat(alias="chat", messages=[{"role": "user", "content": "hi"}])  # puts `first` on cooldown
        chat(alias="chat", messages=[{"role": "user", "content": "hi again"}])
    assert first.calls == 1  # never retried -- still in cooldown
    assert second.calls == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_gateway.py -v`
Expected: FAIL — `app/core/llm.py` still has the old single-Ollama implementation, so `chat()`'s signature doesn't match (`alias=` is unexpected) and `get_deployments`/`classify_exception` aren't imported there yet.

- [ ] **Step 3: Write the implementation**

This fully replaces `app/core/llm.py`. `structured_chat`/`stream_chat` bodies are stubbed here and filled in by Tasks 9 and 10 — write exactly this much now:

```python
# app/core/llm.py
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
    StructuredOutputRepairFailed,
    classify_exception,
)
from app.core.llm_events import emit_llm_attempt
from app.core.llm_pricing import estimate_cost
from app.core.llm_providers import Deployment
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


def chat(*, alias: str, messages: list[dict[str, str]], temperature: float = 0.0) -> str:
    return _call_with_fallback(alias, lambda d: d.call(messages=messages, temperature=temperature))


def structured_chat(*, alias: str, messages: list[dict[str, str]], schema: type[T], temperature: float = 0.0) -> T:
    raise NotImplementedError  # filled in by Task 9


def stream_chat(*, alias: str, messages: list[dict[str, str]], temperature: float = 0.0) -> Iterator[str]:
    raise NotImplementedError  # filled in by Task 10
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_gateway.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: some *other* test files will now fail — `tests/test_llm_providers.py`'s Ollama tests may break if anything imported `app.core.llm._client`, and `app/agent/nodes/*.py` / `app/core/schema_store.py` / `app/main.py` still import names that no longer exist in the new `app/core/llm.py` (`OllamaUnavailableError`, `check_ollama_available`, the old `chat(model=...)` signature, `_client`). **This is expected** — Task 11 fixes every call site in one pass. Do not fix them here; just confirm the failures are exactly those import/signature mismatches and nothing in `llm_errors`/`llm_events`/`llm_pricing`/`llm_providers`/`llm_router`/`llm` itself is broken.

- [ ] **Step 6: Commit**

```bash
git add app/core/llm.py tests/test_llm_gateway.py
git commit -m "feat(llm): rewrite the gateway core -- chat() with ordered fallback"
```

---

### Task 9: `structured_chat()` with repair retry

**Files:**
- Modify: `app/core/llm.py`
- Modify: `tests/test_llm_gateway.py` (append)

**Interfaces:**
- Produces: `structured_chat(*, alias, messages, schema: type[T], temperature=0.0) -> T` (fills in the `NotImplementedError` stub from Task 8).

- [ ] **Step 1: Append the failing tests**

```python
# tests/test_llm_gateway.py -- append
from pydantic import BaseModel


class _Choice(BaseModel):
    intent: str


def test_structured_chat_parses_valid_json_on_first_try():
    from app.core.llm import structured_chat

    good = FakeDeployment("ollama_cloud", ['{"intent": "analytics"}'])
    with _patch_deployments([good]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="analytics")
    assert good.calls == 1


def test_structured_chat_repairs_once_on_invalid_json_then_succeeds():
    from app.core.llm import structured_chat

    deployment = FakeDeployment("ollama_cloud", ["not json at all", '{"intent": "manipulation"}'])
    with _patch_deployments([deployment]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="manipulation")
    assert deployment.calls == 2  # original attempt + one repair retry


def test_structured_chat_advances_to_next_deployment_after_repair_also_fails():
    from app.core.llm import structured_chat

    bad = FakeDeployment("ollama_cloud", ["still not json", "still not json after repair"])
    good = FakeDeployment("gemini", ['{"intent": "off_topic"}'])
    with _patch_deployments([bad, good]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="off_topic")
    assert bad.calls == 2  # original + repair, both invalid -- advanced without a transport-level retry
    assert good.calls == 1


def test_structured_chat_json_mode_deployment_injects_schema_instruction():
    from app.core.llm import structured_chat

    deployment = FakeDeployment("groq", ['{"intent": "schema_question"}'])
    deployment.structured_mode = "json_mode"
    with _patch_deployments([deployment]):
        result = structured_chat(alias="chat", messages=[{"role": "user", "content": "hi"}], schema=_Choice)
    assert result == _Choice(intent="schema_question")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_gateway.py -v`
Expected: FAIL with `NotImplementedError` on the new `structured_chat` tests.

- [ ] **Step 3: Replace the `structured_chat` stub**

```python
# app/core/llm.py -- replace the Task 8 stub with this, and add the helper above it
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


def structured_chat(*, alias: str, messages: list[dict[str, str]], schema: type[T], temperature: float = 0.0) -> T:
    def fn(deployment: Deployment) -> T:
        if deployment.structured_mode == "native_schema":
            return _validate_or_repair(deployment, messages, temperature, schema, format=schema.model_json_schema())
        json_messages = messages + [
            {"role": "system", "content": f"Respond with ONLY valid JSON matching this schema:\n{schema.model_json_schema()}"}
        ]
        fmt = "json" if deployment.structured_mode == "json_mode" else None
        return _validate_or_repair(deployment, json_messages, temperature, schema, format=fmt)

    return _call_with_fallback(alias, fn)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_gateway.py -v`
Expected: PASS (10 tests total)

- [ ] **Step 5: Commit**

```bash
git add app/core/llm.py tests/test_llm_gateway.py
git commit -m "feat(llm): add structured_chat() with one-shot repair retry"
```

---

### Task 10: `stream_chat()` with pre-first-token fallback

**Files:**
- Modify: `app/core/llm.py`
- Modify: `tests/test_llm_gateway.py` (append)

**Interfaces:**
- Produces: `stream_chat(*, alias, messages, temperature=0.0) -> Iterator[str]` (fills in the Task 8 stub).

- [ ] **Step 1: Append the failing tests**

```python
# tests/test_llm_gateway.py -- append
class FakeStreamingDeployment(FakeDeployment):
    def __init__(self, provider_key, stream_script):
        """stream_script: list of either a list-of-chunks (success) or an
        Exception raised on the first next() call."""
        super().__init__(provider_key, [])
        self._stream_script = list(stream_script)

    def stream(self, *, messages, temperature):
        step = self._stream_script.pop(0)
        if isinstance(step, Exception):
            raise step
        return iter(step)


def test_stream_chat_yields_all_chunks_from_the_first_working_deployment():
    from app.core.llm import stream_chat

    good = FakeStreamingDeployment("groq", [["he", "llo"]])
    with _patch_deployments([good]):
        chunks = list(stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}]))
    assert chunks == ["he", "llo"]


def test_stream_chat_falls_back_before_the_first_token():
    from app.core.llm import stream_chat

    class FakeConnErr(Exception):
        pass

    broken = FakeStreamingDeployment("groq", [FakeConnErr("connection refused")])
    good = FakeStreamingDeployment("gemini", [["from", " second"]])
    with _patch_deployments([broken, good]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        chunks = list(stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}]))
    assert chunks == ["from", " second"]


def test_stream_chat_raises_all_providers_failed_when_every_deployment_errors():
    from app.core.llm import stream_chat

    first = FakeStreamingDeployment("groq", [Exception("down")])
    second = FakeStreamingDeployment("gemini", [Exception("also down")])
    with _patch_deployments([first, second]), patch(
        "app.core.llm.classify_exception", return_value=FallbackAction.ADVANCE_IMMEDIATELY
    ):
        with pytest.raises(AllProvidersFailedError):
            list(stream_chat(alias="chat", messages=[{"role": "user", "content": "hi"}]))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_llm_gateway.py -v`
Expected: FAIL with `NotImplementedError` on the new `stream_chat` tests.

- [ ] **Step 3: Replace the `stream_chat` stub**

```python
# app/core/llm.py -- replace the Task 8 stub with this
def stream_chat(*, alias: str, messages: list[dict[str, str]], temperature: float = 0.0) -> Iterator[str]:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_llm_gateway.py -v`
Expected: PASS (13 tests total)

- [ ] **Step 5: Run the full suite and commit**

Run: `.venv/Scripts/python -m pytest -q`
Expected: `tests/test_llm_gateway.py`, `test_llm_errors.py`, `test_llm_events.py`, `test_llm_pricing.py`, `test_llm_providers.py`, `test_llm_router.py` all pass. `tests/test_guardrails.py` still passes (untouched). Other files still fail the same way they did after Task 8 — that's Task 11.

```bash
git add app/core/llm.py tests/test_llm_gateway.py
git commit -m "feat(llm): add stream_chat() with fallback before the first token"
```

---

### Task 11: Rewire call sites, decouple embeddings, add `/health/providers`

**Files:**
- Modify: `app/agent/nodes/sql_generator.py`, `app/agent/nodes/intent_classifier.py`, `app/agent/nodes/ambiguity_check.py`, `app/agent/nodes/followup_suggester.py`, `app/agent/nodes/insight_summarizer.py`
- Modify: `app/core/schema_store.py`
- Modify: `app/main.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `app.core.llm.LLMAlias`, `app.core.llm.check_providers`.
- Produces: nothing new — this task is pure call-site rewiring, verified by the existing `tests/test_guardrails.py` plus a new `main.py` smoke test.

- [ ] **Step 1: Update the five node call sites**

`app/agent/nodes/sql_generator.py`:
```python
from app.agent.prompts import build_sql_generation_messages
from app.agent.state import AgentState
from app.core.llm import LLMAlias, chat
```
```python
    raw = chat(alias=LLMAlias.SQL_GEN, messages=messages, temperature=0.0)
```
(remove the `from app.config import settings` import if nothing else in the file uses `settings`.)

`app/agent/nodes/intent_classifier.py`:
```python
from app.agent.prompts import IntentClassification, build_intent_classification_messages
from app.agent.state import AgentState
from app.core.llm import LLMAlias, structured_chat


def intent_classifier_node(state: AgentState) -> dict:
    result = structured_chat(
        alias=LLMAlias.CHAT,
        messages=build_intent_classification_messages(state["user_input"], state.get("query_history", [])),
        schema=IntentClassification,
    )
    return {"intent": result.intent}
```

`app/agent/nodes/ambiguity_check.py`: same pattern — `from app.core.llm import LLMAlias, structured_chat`, then `alias=LLMAlias.CHAT` in place of `model=settings.chat_model`, drop the now-unused `from app.config import settings` import.

`app/agent/nodes/followup_suggester.py`: `from app.core.llm import LLMAlias, chat`, then `alias=LLMAlias.CHAT` in place of `model=settings.chat_model`, drop the `settings` import.

`app/agent/nodes/insight_summarizer.py`: `from app.core.llm import LLMAlias, stream_chat`, then `alias=LLMAlias.CHAT` in place of `model=settings.chat_model`, drop the `settings` import.

- [ ] **Step 2: Decouple `schema_store.py`'s embedding client**

In `app/core/schema_store.py`, replace:
```python
from app.core.llm import _client as ollama_client  # reuse the single configured Ollama client
```
with:
```python
import ollama

_embedding_client = ollama.Client(host=settings.ollama_host, timeout=settings.llm_request_timeout_seconds)
```
(place it near the top of the module, after the `settings` import — check the existing import block for exactly where `settings` is imported from). Then update the one call site that used `ollama_client.embed(...)` to use `_embedding_client.embed(...)` instead. This stays local-Ollama-only in this phase; Phase 2b replaces it with Gemini embeddings.

- [ ] **Step 3: Update `app/main.py`**

Replace the import:
```python
from app.core.llm import check_providers
```
Replace the lifespan's Ollama gate (the embedding index build should not crash the app if the local embedding server isn't reachable, but it no longer needs to gate on the *new* multi-provider `check_providers()` — that check is about `sql_gen`/`chat`, unrelated to the embedding client):
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.checkpoint_db_path.parent.mkdir(parents=True, exist_ok=True)
    checkpointer_cm = SqliteSaver.from_conn_string(str(settings.checkpoint_db_path))
    checkpointer = checkpointer_cm.__enter__()
    _state["checkpointer_cm"] = checkpointer_cm
    _state["graph"] = build_graph(checkpointer=checkpointer)

    try:
        build_or_refresh_index()  # idempotent; skips re-embedding if already current
    except Exception:  # noqa: BLE001 - a down local embedding server must not crash startup
        logger.exception("Schema index build failed at startup; continuing without it")

    yield

    checkpointer_cm.__exit__(None, None, None)
```
(add `import logging` and `logger = logging.getLogger(__name__)` near the top of `main.py` if not already present.)

Add the new endpoint, right after the existing `/schema` endpoint:
```python
@app.get("/health/providers")
def health_providers() -> dict:
    return check_providers()
```

Update `/health` to drop the Ollama-specific field (pinging every LLM provider on every health check is slow and burns quota — `/health/providers` is the dedicated, deliberately-slower endpoint for that):
```python
@app.get("/health")
def health() -> dict:
    try:
        with read_only_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok, db_msg = True, "ok"
    except Exception as exc:  # noqa: BLE001
        db_ok, db_msg = False, str(exc)

    return {
        "healthy": db_ok,
        "database": {"available": db_ok, "detail": db_msg},
        "read_only_mode": settings.read_only_mode,
    }
```

- [ ] **Step 4: Write a smoke test for the new endpoint**

```python
# tests/test_main_health.py
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


def test_health_providers_endpoint_returns_check_providers_result():
    from app.main import app

    fake_result = {"sql_gen": [{"provider": "groq", "model": "x", "status": "ok", "detail": ""}], "chat": []}
    with patch("app.main.check_providers", return_value=fake_result):
        with TestClient(app) as client:
            response = client.get("/health/providers")
    assert response.status_code == 200
    assert response.json() == fake_result


def test_health_endpoint_no_longer_references_ollama():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")
    body = response.json()
    assert "ollama" not in body
    assert "database" in body
```

- [ ] **Step 5: Update `requirements.txt`**

```
fastapi==0.115.6
uvicorn[standard]==0.34.0
sqlalchemy==2.0.36
sqlglot==26.1.3
chromadb==0.5.23
posthog<3
langgraph==1.2.10
langgraph-checkpoint==4.1.1
langgraph-checkpoint-sqlite==3.1.1
langchain-core==1.5.3
ollama==0.6.2
openai==3.16.2
google-genai==2.24.0
pyyaml==6.0.3
pydantic==2.10.4
pydantic-settings==2.7.1
python-dotenv==1.0.1
pytest==8.3.4
httpx==0.28.1
```

(`chromadb`/`posthog` stay for now — Phase 2b removes them. `httpx` moves from `>=0.27.0,<0.28.0` to a `==0.28.1` pin because `ollama==0.6.2`, `openai==3.16.2`, and `google-genai==2.24.0` all resolve cleanly against it — confirmed by installing all three into this repo's `.venv` together with no dependency-resolver conflicts.)

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python -m pytest -q`
Expected: every test file passes now, including `tests/test_guardrails.py` (40 tests, untouched) and the new `tests/test_main_health.py`.

- [ ] **Step 7: Manual smoke check (no real provider keys needed — everything is mocked in tests, but this confirms nothing else broke)**

Run: `.venv/Scripts/python -c "from app.main import app; print('app imports and builds OK')"`
Expected: prints the message with no exception.

- [ ] **Step 8: Commit**

```bash
git add app/agent/nodes/sql_generator.py app/agent/nodes/intent_classifier.py app/agent/nodes/ambiguity_check.py \
        app/agent/nodes/followup_suggester.py app/agent/nodes/insight_summarizer.py app/core/schema_store.py \
        app/main.py requirements.txt tests/test_main_health.py
git commit -m "feat(llm): rewire nodes onto the alias-based gateway, add GET /health/providers"
```

---

### Task 12: Live verification and phase note

**Files:**
- Modify: `config/model_groups.yaml` (replace `# TO LIVE-VERIFY` tags with confirmed models)
- Modify: `app/core/llm_pricing.py` (fill in real per-provider rates found during verification)
- Create: `docs/phases/01-llm-gateway.md`

This task needs real API keys from you — nothing before this point required them (every prior test mocks the provider SDKs). **Stop and ask for `OLLAMA_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `GROQ_API_KEY` before starting this task if they aren't already in a local (git-ignored) `.env`.**

- [ ] **Step 1: Confirm each model tag with one real call per provider**

For each `provider: model` pair in `config/model_groups.yaml`, run a throwaway script against the real API (not committed — use the scratchpad directory) that calls `app.core.llm_providers.build_deployment(provider_key, model).call(messages=[{"role":"user","content":"Reply with the single word: pong"}], temperature=0.0)` and prints the result. For Ollama Cloud specifically, also list available models first (`ollama.Client(host=..., headers=...).list()`) to confirm whether the exact tag needs a `-cloud` suffix or not, per the spec's explicit callout.

- [ ] **Step 2: Confirm structured-output behavior live**

For one alias, run `app.core.llm.structured_chat(alias=..., messages=..., schema=IntentClassification)` for real against each of the four providers in the chain (temporarily reordering `config/model_groups.yaml` or calling `build_deployment` directly to isolate one at a time) and confirm: Ollama Cloud and Gemini return valid JSON on the first attempt via `native_schema` mode; OpenRouter/Groq's `json_mode` + injected-schema-instruction approach reliably produces valid JSON without needing the repair retry in the common case (the repair path is already unit-tested — this just confirms it isn't needed on *every* call in practice).

- [ ] **Step 3: Update `config/model_groups.yaml`**

Replace every `# TO LIVE-VERIFY` comment with either confirmation (`# confirmed live 2026-09-20`) or a corrected model tag, based on Steps 1–2's results.

- [ ] **Step 4: Update `app/core/llm_pricing.py`**

Add real per-million-token rates for whichever models turned out to be paid (check each provider's current pricing page), keeping the `0.0` default for genuinely free-tier deployments (Ollama Cloud, OpenRouter's `:free` models).

- [ ] **Step 5: Run the full suite one more time**

Run: `.venv/Scripts/python -m pytest -q`
Expected: all tests still pass (the live-verification script itself is throwaway and not part of the suite).

- [ ] **Step 6: Write `docs/phases/01-llm-gateway.md`**

Document: what changed and why (the gateway, the alias indirection, the fallback state machine); which model tags were confirmed live vs. assumed from the spec's suggestions, and what changed as a result; confirmation of the `ollama.Client(host=..., headers=...)` Bearer-auth approach (already verified during planning — note it here as verified, not assumed); the `httpx`/`httpx2` split discovered while pinning `openai==3.16.2` (openai depends on a separate `httpx2` package for its own transport, while `ollama`/`google-genai` still use classic `httpx` — `classify_exception()` only needs to know about `openai.*`'s own exception classes plus raw `httpx.*` for the other two, so this required no design change, just documenting it so nobody re-discovers it the hard way); structured-output repair-retry rate observed live (how often OpenRouter/Groq needed the repair round-trip vs. produced valid JSON on the first attempt); any provider whose real behavior diverged from what's implemented (e.g., a provider that doesn't actually honor `response_format={"type":"json_object"}` the way expected) and what was changed in response.

- [ ] **Step 7: Final commit**

```bash
git add config/model_groups.yaml app/core/llm_pricing.py docs/phases/01-llm-gateway.md
git commit -m "docs(llm): Phase 1 live-verification results and design note"
```

---

## Definition of done for this phase

- `pytest -q` passes in full, including all of `tests/test_guardrails.py` unchanged.
- No node module imports `app.config.settings.sql_gen_model` or `.chat_model` anymore — grep for both to confirm zero hits outside `docs/`.
- `GET /health/providers` returns a real per-alias, per-provider status when called against real keys (Task 12 confirms this manually; no automated test hits real network).
- With `OLLAMA_API_KEY` unset (simulating a revoked key), a real `chat(alias=LLMAlias.CHAT, ...)` call still succeeds by falling through to OpenRouter → Gemini → Groq — this is effectively Phase 1's contribution to the project's overall "Definition of done" bullet about surviving a revoked Ollama Cloud key; the *UI* showing which provider answered is Phase 6's job, but the gateway already reports it via the `llm_attempt` event.
- `config/model_groups.yaml` has no remaining `# TO LIVE-VERIFY` comments.
