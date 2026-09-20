# Phase 1: Provider-agnostic LLM gateway

Status: complete. Live-verified 2026-09-20 (Task 12) against real Ollama
Cloud, OpenRouter, Gemini, and Groq accounts.

## What changed and why

QueryPilot's nodes used to call a single hardcoded local Ollama model
directly. That's a single point of failure: if the local Ollama process is
down, or (once deployed) a cloud provider's free tier is rate-limited or a
key gets revoked, every LLM-backed request fails with no recourse.

Phase 1 replaces that direct call with a provider-agnostic gateway:

- **Alias indirection.** Node code never names a provider or a model. It
  asks `app.core.llm` for one of two aliases -- `LLMAlias.SQL_GEN` or
  `LLMAlias.CHAT` -- and `app/core/llm_router.py` resolves the alias to an
  ordered list of `Deployment`s from `config/model_groups.yaml`. Swapping,
  reordering, or re-pricing a provider is a config edit, not a code change.
- **One `Deployment` per {provider, model}.** `app/core/llm_providers.py`
  wraps each already-authenticated provider client (`ollama.Client`,
  `openai.OpenAI` pointed at OpenRouter's or Groq's OpenAI-compatible base
  URL, `google.genai.Client`) behind a uniform `call()` / `stream()`
  interface, so the fallback loop in `app/core/llm.py` never needs to know
  which SDK it's talking to.
- **Ordered fallback state machine.** `app/core/llm.py`'s
  `_call_with_fallback()` walks the alias's deployment chain in order. Each
  exception is classified by `app/core/llm_errors.py::classify_exception()`
  into one of three actions: `NO_ADVANCE` (malformed request -- would fail
  identically on every deployment, so raise immediately), `RETRY_THEN_ADVANCE`
  (transient -- timeout/connection/5xx, retry once same deployment then
  advance), or `ADVANCE_IMMEDIATELY` (deployment-specific -- 401/403/404/429,
  advance without retrying and put the deployment on a 10-minute cooldown so
  subsequent requests skip it without re-probing).
- **Structured output with a repair retry.** `structured_chat()` asks each
  deployment for JSON one of two ways, based on `Deployment.structured_mode`:
  `native_schema` (Ollama's `format=<json schema>`, Gemini's
  `response_json_schema`) or `json_mode` (OpenAI-compatible
  `response_format={"type": "json_object"}` plus the schema injected into
  the prompt as an instruction, since `json_mode` only guarantees syntactic
  JSON, not schema conformance). If the first response fails
  `schema.model_validate_json()`, one repair round-trip is attempted with the
  validation error appended to the conversation. If that also fails,
  `StructuredOutputRepairFailed` is raised and the gateway advances to the
  next deployment *without* a cooldown penalty -- the deployment itself
  isn't necessarily unhealthy, just bad at this one schema.
- **Health visibility.** `GET /health/providers` (`check_providers()` in
  `app/core/llm_router.py`) pings every deployment of every alias with one
  short "pong" call and reports per-provider status and latency, live, with
  no caching.

## Live verification: model tags

All entries in `config/model_groups.yaml` were live-tested with one real
call per `{provider, model}` pair via
`app.core.llm_providers.build_deployment(provider, model).call(...)` --
the gateway's own code path, not a hand-rolled SDK call.

### `sql_gen` chain

| Provider | Spec suggestion | Live result |
|---|---|---|
| ollama_cloud | `qwen3-coder:480b-cloud` | **Changed** -> `gpt-oss:120b` |
| openrouter | `qwen/qwen-2.5-coder-32b-instruct:free` | **Changed** -> `cohere/north-mini-code:free` |
| gemini | `gemini-3.6-flash` | Confirmed as-is |
| groq | `openai/gpt-oss-120b` | Confirmed as-is |

### `chat` chain

| Provider | Spec suggestion | Live result |
|---|---|---|
| ollama_cloud | `gpt-oss:120b-cloud` | **Changed** -> `gpt-oss:20b` |
| openrouter | `meta-llama/llama-3.3-70b-instruct:free` | **Changed** -> `nvidia/nemotron-3-super-120b-a12b:free` |
| gemini | `gemini-3.6-flash` | Confirmed as-is |
| groq | `openai/gpt-oss-20b` | Confirmed as-is |

### Ollama Cloud: the `-cloud` suffix question, resolved

The spec explicitly flagged uncertainty about whether Ollama Cloud's direct
API needs a `-cloud` suffix on model tags. Verified with:

```python
ollama.Client(
    host=provider_credentials.ollama_cloud_host,
    headers={"Authorization": f"Bearer {provider_credentials.ollama_api_key}"},
).list()
```

**Answer: no suffix.** The account's available tags (20 models) are plain
names with no `-cloud` suffix at all -- e.g. `gpt-oss:120b`, `gpt-oss:20b`,
`gemma4:31b`, `nemotron-3-nano:30b`, `kimi-k2.7-code`, `qwen3.5:397b`, etc.
Neither `qwen3-coder:480b-cloud` nor `gpt-oss:120b-cloud` exist in any form
(with or without the suffix) -- `qwen3-coder` isn't offered on this account
at all, and `gpt-oss:120b`/`gpt-oss:20b` exist but without the suffix.

This also confirms the **Bearer-auth pattern itself**
(`ollama.Client(host=..., headers={"Authorization": f"Bearer {key}"})`,
pointed at `https://ollama.com` instead of a local Ollama server) works
exactly as assumed during planning -- `build_ollama_cloud()` in
`app/core/llm_providers.py` needed no changes.

### Why the other three tags changed

- **`qwen3-coder:480b-cloud` (ollama_cloud, sql_gen):** not offered on this
  account at all. Of the 20 available tags, only one has an explicit
  coder-model name (`kimi-k2.7-code`), and calling it returned HTTP 402
  `"this model is not included in your free usage, add usage credits to pay
  as you go"`. Free-tier probing across the full model list found only four
  models included at no cost: `gpt-oss:120b`, `gpt-oss:20b`, `gemma4:31b`,
  `nemotron-3-nano:30b` (everything else -- `deepseek-v4-flash`,
  `glm-5.3-flash`, `qwen3.5:397b`, `glm-5.1`, etc. -- returned the same 402).
  `gpt-oss:120b` was picked for `sql_gen` (larger, reasoning-capable, good at
  code) since QueryPilot's whole point is a free public demo.
- **`gpt-oss:120b-cloud` (ollama_cloud, chat):** the tag simply doesn't
  exist (see above). Replaced with the free, confirmed-working `gpt-oss:20b`
  -- deliberately the *smaller* free tag, distinct from `sql_gen`'s
  `gpt-oss:120b`, since chat doesn't need the larger model's reasoning depth.
- **`qwen/qwen-2.5-coder-32b-instruct:free` (openrouter, sql_gen):**
  OpenRouter returned HTTP 404: `"This model is unavailable for free. The
  paid version is available now -- use this slug instead:
  qwen/qwen-2.5-coder-32b-instruct"` (i.e. OpenRouter delisted the free
  variant). Queried `client.models.list()` against OpenRouter's
  OpenAI-compatible `/models` endpoint (446 total models, 21 with a
  `:free` suffix) and picked `cohere/north-mini-code:free` -- a
  coder-focused free model, confirmed working live.
- **`meta-llama/llama-3.3-70b-instruct:free` (openrouter, chat):** same
  404 pattern -- OpenRouter delisted this free variant too. Replaced with
  `nvidia/nemotron-3-super-120b-a12b:free`, a large general-purpose
  instruct model from the same `:free` listing, confirmed working live.
  Three other `:free` candidates tried during this search
  (`google/gemma-4-31b-it:free`, `z-ai/glm-5.2:free`, `qwen/qwen3.8-27b:free`)
  all returned HTTP 429 `"temporarily rate-limited upstream"` from their
  respective upstream providers on OpenRouter's shared free pool -- a real
  observed rate-limit data point, not a reason to prefer them over
  `nvidia/nemotron-3-super-120b-a12b:free`, which answered immediately both
  times it was tried.

`gemini-3.6-flash` and Groq's `openai/gpt-oss-120b` / `openai/gpt-oss-20b`
all responded correctly on the first live call with no changes needed.

`config/model_groups.yaml` now records each decision inline as a comment
next to the model tag (either `# confirmed live 2026-09-20` or a `# changed
2026-09-20: ...` explanation), so the reasoning travels with the config.

## Structured-output verification (live)

Tested `IntentClassification` (`app.agent.prompts.IntentClassification`,
fields `intent: Literal[...]` + `reasoning: str`) against all four `chat`
deployments, mirroring `structured_chat()`'s own per-deployment logic
(`build_deployment` + the correct `format=` argument for each
`structured_mode`) rather than a hand-rolled call.

| Provider (chat) | Mode | First-attempt result |
|---|---|---|
| gemini (`gemini-3.6-flash`) | `native_schema` | Valid JSON, first try, every time tried |
| groq (`openai/gpt-oss-20b`) | `json_mode` | Valid JSON, first try, 4/4 sampled prompts |
| openrouter (`nvidia/nemotron-3-super-120b-a12b:free`) | `json_mode` | Valid JSON, first try, 3/4 sampled prompts (see below) |
| ollama_cloud (`gpt-oss:20b`) | `native_schema` | **Never** valid on first try in this testing -- see below |

**Gemini and Groq confirm the design assumption cleanly**: Gemini's
`response_json_schema` enforces the schema server-side and always returned
parseable, schema-conformant JSON. Groq's `json_mode` + injected-schema
prompt also reliably produced valid JSON without needing the repair retry
in every sample tried (4 different prompts: an analytics ask, a
manipulation/delete ask, a schema question, and an off-topic ask -- all
4/4 valid on the first attempt).

**OpenRouter (`nvidia/nemotron-3-super-120b-a12b:free`) mostly confirms the
design too, with one flaky data point worth recording:** 3 of 4 sampled
prompts returned valid JSON on the first attempt. The fourth call (a
"delete every order older than a year" prompt) raised a `TypeError:
'NoneType' object is not subscriptable` from inside
`_build_openai_compatible._call()`'s `response.choices[0].message.content`
access -- the returned `ChatCompletion.choices` was `None`. Re-running the
identical request immediately afterward against OpenRouter directly (same
model, same messages, same `response_format`) succeeded normally and
returned well-formed JSON, so this reads as upstream flakiness on
OpenRouter's shared free pool for this model rather than a deterministic
failure tied to the prompt content.

This surfaces a real gap worth flagging for a future hardening task (not
fixed here -- Task 12's scope is config/docs only): a `choices=None`
response produces a raw `TypeError`, which isn't one of the exception types
`classify_exception()` recognizes (not an `openai.*` exception, no
`.status_code`/`.code` attribute), so it falls through to the
`FallbackAction.NO_ADVANCE` default ("fail loudly rather than silently
cycling every deployment for an error class this gateway doesn't understand
yet") instead of advancing to the next deployment the way a normal
provider hiccup would. In practice this means: if `choices=None` happens on
the *last* deployment in a chain, the caller gets an unhandled `TypeError`
instead of `AllProvidersFailedError`. Recommend adding `TypeError` (or a
guard on `response.choices` being falsy) to the transient/advance
classification in a follow-up.

**Ollama Cloud's `native_schema` mode did not work as designed, live,
for either `gpt-oss` model on this account's cloud host:**
`build_ollama_cloud(...).call(messages=..., format=schema.model_json_schema())`
was tried against `gpt-oss:20b`, `gpt-oss:120b`, `gemma4:31b`, and
`nemotron-3-nano:30b` (all four of the account's confirmed-free models) --
every one ignored the JSON-schema `format` parameter and returned prose
instead of JSON (`'**Classification:** Data Retrieval (SQL Query)'`,
`'**Classification:** Database Query / Data Retrieval Request'`, etc.), or
in `gpt-oss:120b`'s case, an outright refusal
(`"I'm sorry, but I can't help with that."`) unrelated to any actual policy
violation in the prompt. This is a genuine divergence from the assumption
that Ollama's `format=<schema>` behaves the same on the remote Ollama Cloud
API as it does against a local Ollama server (which is exercised only via
mocks in this repo's test suite, never live, since `local:` profile testing
needs a real local Ollama process).

Running the real `_validate_or_repair()` (from `app.core.llm`) against
`ollama_cloud`/`gpt-oss:20b` confirmed the designed recovery path works
correctly: the repair round-trip got closer (produced `{"intent": "Data
Retrieval"}` -- syntactically valid-ish JSON but a non-enum `intent` value
and a missing `reasoning` field), still failed
`schema.model_validate_json()`, and correctly raised
`StructuredOutputRepairFailed`. Per `_call_with_fallback()`'s design, this
type is caught separately from a general exception and advances to the next
deployment (OpenRouter) *without* a cooldown penalty, since the deployment
isn't unhealthy -- just unreliable for this one schema. **No code change
was made in response** -- this is exactly the scenario `native_schema`
mode's repair-retry-then-advance-without-cooldown was built for, and it
works. The practical upshot: `structured_chat()` on the `chat` alias will,
in production, reliably skip past `ollama_cloud` and succeed on
`openrouter` -- worth knowing rather than being surprised by later, since
it means `ollama_cloud` currently only usefully serves the `chat` alias's
free-form `chat()`/`stream_chat()` paths, not `structured_chat()`.

## `httpx` / `httpx2` split (openai==3.16.2)

Confirmed live in this environment: `openai==3.16.2` depends on a separate
`httpx2` package (`.venv/Lib/site-packages/httpx2/`) for its own HTTP
transport, while `ollama` and `google-genai` still use classic `httpx`.
`openai`'s exception hierarchy (`openai.APIStatusError`,
`openai.APIConnectionError`, `openai.APITimeoutError`,
`openai.RateLimitError`, etc., in `openai/_exceptions.py`) is entirely
self-contained and doesn't reference `httpx2` types directly in its public
surface, so `app/core/llm_errors.py::classify_exception()` only ever needs
to recognize `openai.*`'s own exception classes for the OpenRouter/Groq
deployments, plus raw `httpx.*` (`httpx.TimeoutException`,
`httpx.ConnectError`) for the `ollama`/`google-genai` deployments. This
required **no design change** -- `classify_exception()` was already written
this way -- but is worth documenting explicitly so a future contributor
doesn't waste time trying to catch `httpx2.TimeoutException` or similar
before discovering it's irrelevant to this gateway's error classification.

## Pricing (`app/core/llm_pricing.py`)

Refreshed `_PRICE_PER_MILLION_TOKENS_USD` (prompt rate, completion rate)
against each provider's current pricing:

| Provider | Model | Prompt $/M | Completion $/M | Source |
|---|---|---|---|---|
| groq | `openai/gpt-oss-120b` | 0.15 | 0.60 | console.groq.com/docs/model/openai/gpt-oss-120b (was 0.15/0.75, corrected) |
| groq | `openai/gpt-oss-20b` | 0.075 | 0.30 | console.groq.com pricing (was 0.05/0.20, corrected) |
| gemini | `gemini-3.6-flash` | 0.75 | 3.75 | ai.google.dev pricing, promotional rate through 2026-12-31 (rises to 1.50/7.50 on 2027-01-01 -- **revisit this entry after that date**) |

Rates were found via web search against each provider's current pricing
page/docs rather than assumed. `ollama_cloud` (`gpt-oss:120b`,
`gpt-oss:20b`) and every OpenRouter `:free`-suffixed model deliberately have
**no entry** and fall through to the `0.0` default: this was live-confirmed,
not assumed -- both `gpt-oss` tags on `ollama_cloud` answered calls in this
verification session with no HTTP 402 "not included in your free usage"
error, and OpenRouter's `:free` suffix is a hard price signal for that
provider.

## Definition-of-done checks (Task 12, manual)

- **`pytest -q`**: 102 passed, 0 failed, after the config/pricing changes
  above (this task changes no code, only data, so this was expected --
  confirmed rather than assumed).
- **No node references `settings.sql_gen_model` / `.chat_model`**: grepped
  the repo; the only hits are in this plan document
  (`docs/superpowers/plans/2026-09-20-phase1-llm-gateway.md`), zero hits in
  application code.
- **`GET /health/providers` against real keys**: called `check_providers()`
  directly (the endpoint's implementation) against the corrected
  `config/model_groups.yaml`. All 8 deployments (4 per alias) reported
  `status: "ok"` with real latencies, e.g. groq responded in ~560-780ms,
  openrouter ~860-1100ms, gemini ~2100-3600ms, ollama_cloud ~900-4700ms
  (its `gpt-oss:20b` chat probe was the slowest single call observed in this
  session).
- **Revoked Ollama Cloud key still falls through to OpenRouter -> Gemini ->
  Groq**: verified with a live call using a garbage, non-empty
  `OLLAMA_API_KEY` (simulating an actually-revoked key, i.e. present but
  rejected by the provider) -- `chat(alias=LLMAlias.CHAT, ...)` correctly
  fell through `ollama_cloud`'s 401/auth failure and returned `'pong'` from
  the next healthy deployment. **One nuance worth recording**: this is
  different from `OLLAMA_API_KEY` being completely *unset/empty*. In that
  case, `app/core/llm_router.py::get_deployments()` builds every
  deployment in the chain eagerly, up front, in one list comprehension --
  and `build_ollama_cloud()` raises `LLMConfigError` (a fail-fast
  "you forgot to configure this" signal, deliberately not part of
  `classify_exception()`'s runtime-failure taxonomy) *before* any deployment
  is ever called, so no fallback is attempted at all and the whole alias's
  `chat()` call raises. This matches `LLMConfigError`'s own docstring intent
  ("a configured deployment is missing required config") but means the
  *literal* "unset" scenario doesn't self-heal the way a genuinely revoked
  (present-but-rejected) key does. Since a real revoked key is present
  (just invalid) rather than absent, the production scenario this DoD
  bullet cares about is confirmed working; the unset-env-var edge case is
  flagged here as a known gap for a future task (e.g. catching
  `LLMConfigError` per-deployment inside `get_deployments()` and skipping
  that entry, the way `check_providers()` already does at the whole-alias
  level) rather than fixed in this task, since it requires a code change to
  `app/core/llm_router.py`, outside Task 12's declared file scope
  (`config/model_groups.yaml`, `app/core/llm_pricing.py`,
  `docs/phases/01-llm-gateway.md`).
- **No remaining `# TO LIVE-VERIFY` comments** in `config/model_groups.yaml`:
  confirmed -- every entry now carries either a `# confirmed live
  2026-09-20` or a `# changed 2026-09-20: ...` comment.

## Summary of what to watch going forward

1. `nvidia/nemotron-3-super-120b-a12b:free` on OpenRouter occasionally
   returns `choices=None`; `classify_exception()` doesn't yet turn that into
   a fallback-eligible action for non-`openai.*`/non-status-coded exceptions
   like the resulting `TypeError`.
2. `ollama_cloud`'s `native_schema` structured-output mode is unreliable in
   practice for every free-tier model tried on this account -- the fallback
   design absorbs this correctly today, but it means `ollama_cloud`
   effectively never serves `structured_chat()` calls in production, only
   `chat()`/`stream_chat()`.
3. `get_deployments()`'s eager, whole-chain construction means a literally
   unset (not just invalid) credential for the *first* deployment in a
   chain prevents any fallback from being attempted for that alias.
4. Gemini's Flash-tier pricing used here is a promotional rate through
   2026-12-31 and doubles on 2027-01-01 -- `app/core/llm_pricing.py`'s
   `gemini-3.6-flash` entry should be revisited around that date.
