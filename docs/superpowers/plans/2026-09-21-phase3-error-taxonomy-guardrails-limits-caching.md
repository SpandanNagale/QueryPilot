# Phase 3: Error Taxonomy, Guardrails, Limits and Caching — Implementation Plan

Source: `docs phase spec` (QueryPilot deploy prompt), "Phase 3" section. Builds on
completed Phase 1 (LLM gateway) and Phase 2 (deployment blockers) — both merged
to `main` at `63baec7`.

## Global Constraints

- `app/core/validator.py` stays the safety boundary. Never weaken, bypass, or
  loosen any validator check, the row-impact ceiling, the FK-aware delete
  preview, or the confirmation gate — for caching, guardrails, or anything else.
- `tests/test_guardrails.py` must still pass, unmodified in its existing
  assertions, after every task.
- No raw exception text or traceback ever reaches the client. Every error
  crossing the API boundary is `{code, message, node}` (SSE `error` event) or
  an `HTTPException(status, detail={code, message})`. Log full details
  server-side only (`logger.exception` / `logger.warning`).
- The input guardrail targets prompt attacks (injection, role-override,
  gibberish, PII, length), not SQL. Destructive SQL typed directly by the user
  (`DROP TABLE ...`) must still reach the validator so the UI can show the
  validator blocking it — the guardrail must not intercept SQL-shaped input.
- The cache never caches `insight_summarizer`. Cached SQL always still passes
  through the validator, dry-run preview, and confirmation gate — caching
  skips only the LLM call for `intent_classifier`/`sql_generator`, never the
  safety pipeline downstream of it.
- BYOK (`byok_provider`/`byok_api_key` on `ChatRequest`) routes that one
  request through the caller's key and bypasses the daily budget. Never log
  or persist the key (not in audit events, not in `llm_attempt` events, not in
  exception messages).
- Every new `QP_`-prefixed setting goes in `app/config.py`'s `Settings` class
  and is documented in `.env.example` with a one-line comment.
- Run the full test suite and confirm `tests/test_guardrails.py` passes
  before the branch is considered done for this phase.

## Relevant existing code (read before starting any task)

- `app/main.py` — `_run_graph_stream` (main.py:123-147) has the raw-exception
  leak this phase closes (`str(exc)` at line 145). `/confirm` already raises
  a `409 {"code": "SESSION_EXPIRED", ...}` HTTPException (line 174-177) — a
  precedent for the shape every other error should match.
- `app/core/llm_errors.py` — Phase 1's **provider-fallback** classifier
  (`FallbackAction`), a different concern from this phase's **client-facing**
  `ErrorCode` taxonomy. `AllProvidersFailedError` (raised by
  `app/core/llm.py::_call_with_fallback`) is the main exception this phase's
  taxonomy needs to catch and map to `ALL_PROVIDERS_FAILED`.
- `app/core/llm_events.py` — `emit_llm_attempt(**fields)` /
  `register_llm_attempt_listener(fn)`. Fields currently: `alias, provider,
  model, latency_ms, success, error, prompt_tokens, completion_tokens,
  cost_usd`. No `session_id` field today — `app/core/llm.py`'s `chat()` /
  `structured_chat()` / `stream_chat()` don't know the session, only the
  node's `AgentState`/`config` does.
- `app/core/models.py` — `BudgetEntry` ORM model already exists
  (`session_id, provider, model, cost_usd, timestamp`), created in Phase 2's
  Task 6 anticipating this phase. No code writes to it yet.
- `app/core/opdb.py` — `get_session()` context manager for the operational DB
  (Neon or local SQLite), used by `app/core/audit.py::log_event`.
- `app/agent/graph.py` — `build_graph()`. `START -> intent_classifier` is the
  single entry edge; the guardrail node goes here.
- `app/agent/state.py` — `AgentState` TypedDict. Add fields here for any new
  per-turn state (guardrail verdict, cache hit info).
- `app/agent/nodes/intent_classifier.py`, `app/agent/nodes/sql_generator.py`
  — the two cache targets. `intent_classifier_node` calls
  `structured_chat(alias=LLMAlias.CHAT, ...)`; `sql_generator_node` calls
  `chat(alias=LLMAlias.SQL_GEN, ...)` via `generate_sql_raw()`.
- `app/agent/nodes/terminal_responses.py::error_response_node` — reads
  `state["error"]` (free text) to build `final_response`. This internal
  free-text mechanism for the generation-retry loop is NOT the client-facing
  raw-exception leak this phase fixes; leave its shape as-is unless a task
  below says otherwise.
- `app/core/validator.py::validate_sql` — returns `ValidationResult` with a
  `.reasons: list[str]`. Categorize by substring: mentions of "row" +
  "ceiling"/"limit" -> `ROW_CEILING_EXCEEDED`; mentions of "cascade" or "FK"
  -> `FK_BLOCKED`; everything else -> `VALIDATION_BLOCKED`. Don't restructure
  `validate_sql` itself to also work.

---

## Task 1: Error taxonomy (`app/core/errors.py`)

Create `app/core/errors.py`:

```python
from enum import Enum

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
```

Give each code a `(message: str, severity: Severity)` entry in a module-level
`_CATALOG: dict[ErrorCode, tuple[str, Severity]]` — a short, human-readable
sentence per code (e.g. `PROVIDER_RATE_LIMIT: "The AI provider is rate-limiting
us right now."`). Add:

```python
class AppError(Exception):
    def __init__(self, code: ErrorCode, *, detail: str | None = None, node: str | None = None): ...
    def to_dict(self) -> dict: ...  # {"code": ..., "message": ..., "node": ...}
```

`to_dict()` uses the catalog's message for `code`; `detail`, if given, is for
server-side logging only (not merged into the client-facing `message`).

Add `classify_exception_to_code(exc: Exception) -> ErrorCode`: maps
`AllProvidersFailedError` -> `ALL_PROVIDERS_FAILED`, and falls back to
`app.core.llm_errors.classify_exception`'s status-code extraction for a
raw provider exception surfacing before all deployments are exhausted
(timeout -> `PROVIDER_TIMEOUT`, 429 -> `PROVIDER_RATE_LIMIT`, 401/403 ->
`PROVIDER_AUTH_FAILED`, else -> `PROVIDER_UNAVAILABLE`); anything
unrecognized -> `INTERNAL`.

**Wire it into `app/main.py`:**
- `_run_graph_stream`'s `except Exception as exc` (main.py:144-145): replace
  `yield _sse("error", {"node": "server", "error": str(exc)})` with an
  `AppError`-shaped payload via `classify_exception_to_code`, and
  `logger.exception(...)` the real exception server-side first.
- Any node's `partial.get("error")` branch (main.py:138-139) that surfaces a
  validator/guardrail reason: categorize per the substring rule above and
  emit `{code, message: reason, node: node_name}` instead of the bare
  `partial["error"]` string — the reason text itself is fine to keep as the
  message (it's already curated, not a raw traceback), just add the code.
- `/confirm`'s existing `HTTPException(409, detail={"code": "SESSION_EXPIRED", ...})`
  (main.py:174-177): keep the shape, source the message from the new catalog
  for consistency.
- `_require_valid_session_id` / `_resolve_session_engine`'s
  `HTTPException(400, "session_id must be a valid UUID4.")`: switch to
  `detail={"code": "SESSION_INVALID", "message": ...}`.
- `/health`: on any internal failure building the response, catch and return
  a `{code: INTERNAL, ...}` shape rather than letting FastAPI's default 500
  handler leak a traceback.

**Tests:** every `ErrorCode` has a catalog entry (no `KeyError` on lookup);
`classify_exception_to_code` on a timeout/429/401/AllProvidersFailedError/
unknown exception returns the right code; `_run_graph_stream`'s exception
path (mock the graph to raise) never puts `str(exc)`'s literal text in the
SSE payload; `/confirm`'s 409 and the session-id 400s carry `code`.

---

## Task 2: Input guardrail (`app/core/guardrail.py` + graph node)

Create `app/core/guardrail.py` with a single entry point:

```python
def check_input(text: str) -> GuardrailVerdict:  # GuardrailVerdict: blocked: bool, reason: str | None
```

Heuristics only, no LLM call:
- **Length**: reject if `len(text) > settings.guardrail_max_input_chars`
  (new setting, default 2000).
- **Gibberish**: reject if the text has no alphabetic content, or an
  extremely low ratio of dictionary-shaped tokens (a cheap heuristic —
  e.g. fraction of characters that are letters/digits/spaces vs symbols,
  below a threshold; don't reach for an NLP dependency).
- **Prompt-injection / role-override patterns**: a compiled regex list
  matching things like "ignore previous instructions", "you are now",
  "system prompt", "act as", "disregard the above", case-insensitive.
- **PII**: a regex for obvious patterns (email, SSN-shaped, credit-card-shaped
  digit runs) — block, don't try to be a full PII detector.
- **Do not** pattern-match SQL keywords (`DROP`, `DELETE`, `UPDATE`) as
  injection — that's the validator's job on the *generated* SQL, and this
  guardrail runs on the *user's raw natural-language input* before any SQL
  exists. A user typing `DROP TABLE orders` as their message must still
  reach `intent_classifier` -> ... -> `validator`, which blocks it and lets
  the UI show *that* block, per Global Constraints.

Add `guardrail_verdict: NotRequired[GuardrailVerdictDict]` to `AgentState`
(`app/agent/state.py`).

New node `app/agent/nodes/guardrail.py`:
```python
def guardrail_node(state: AgentState) -> dict: ...  # calls check_input(state["user_input"])
def route_after_guardrail(state: AgentState) -> str:  # "blocked" | "intent_classifier"
```

Wire into `app/agent/graph.py`: `START -> guardrail -> {blocked:
error_response, intent_classifier: intent_classifier}` (replacing the
current `START -> intent_classifier` edge). On block, set
`state["error"]` to the guardrail reason (so `error_response_node`'s
existing free-text path produces a sentence) — main.py's Task-1 wiring then
tags the resulting SSE `error` event `GUARDRAIL_BLOCKED` via the
"mentions guardrail" categorization, OR (implementer's call, note the
choice in the report) have `guardrail_node` set a code-bearing field
directly. A blocked guardrail verdict must emit zero LLM calls — verify this
with a test using a mock/spy on `app.core.llm.chat`/`structured_chat` that
fails the test if called.

**Tests:** each heuristic blocks its class of input (injection phrase, role
override phrase, one PII pattern, over-length, gibberish); a normal analytics
question passes; `DROP TABLE orders` and "delete all orders" (a preset
"try to break it" attack from the spec) both pass the guardrail and reach
the validator, which is what actually blocks them (assert on `route_after_guardrail`
returning `"intent_classifier"` for these, not `"blocked"`); zero LLM calls
on a blocked verdict.

---

## Task 3: Per-IP rate limiting

New setting: `rate_limit_per_minute: int = 10` (`QP_RATE_LIMIT_PER_MINUTE`).

New module `app/core/rate_limit.py`: an in-memory per-IP sliding window
(a `dict[str, list[float]]` of request timestamps, or a simple fixed-window
counter reset every 60s — sliding window per the spec, keep it simple: a
deque of timestamps per IP, prune entries older than 60s on each check).
No new dependency — stdlib only (`collections.deque`, `time`).

```python
def check_rate_limit(client_ip: str) -> bool:  # True = allowed
```

Wire into `/chat` and `/confirm` in `app/main.py`: extract the client IP
from the `X-Forwarded-For` header (first entry, since Render sits behind a
proxy; fall back to `request.client.host` if the header is absent — this
means both endpoints need a `Request` param added). On a blocked check,
raise `HTTPException(429, detail={"code": "RATE_LIMITED", "message": ...})`
before any graph/LLM work starts.

Memory note: the per-IP timestamp dict grows unboundedly over a long-running
process with many distinct IPs. A single free-tier demo instance with modest
traffic won't hit this in practice within a 15-minute spin-down window;
leave a `ponytail:`-style one-line comment noting the ceiling
(unbounded dict growth) and the upgrade path (evict IPs with no recent
requests) rather than building eviction now.

**Tests:** N+1th request within the window from the same IP is rejected
with 429/RATE_LIMITED; a different IP is unaffected; requests outside the
window succeed again; `X-Forwarded-For` is read correctly (and a request
with no such header still works via `request.client.host`).

---

## Task 4: Daily budget kill-switch + BYOK

New settings: `daily_budget_usd: float = 0.0` (`QP_DAILY_BUDGET_USD`; `0.0`
means "no cap" — don't block anything when unset, since local dev shouldn't
need this configured), `budget_currency` not needed (USD only, per spec).

**Budget tracking — attribute LLM cost to a session without threading
`session_id` through every node's `chat()`/`structured_chat()` call.** Use a
`contextvars.ContextVar[str | None]` (e.g. in `app/core/llm_events.py` or a
new tiny module) set once per request in `app/main.py::_run_graph_stream`
(or `chat`/`confirm`) at the top of the request, read by a budget listener
registered via `register_llm_attempt_listener`. The listener writes a
`BudgetEntry` row (`session_id` from the contextvar, `provider`, `model`,
`cost_usd` from the `llm_attempt` event fields) via `opdb.get_session()`,
skipping rows with `cost_usd is None`. Register this listener once at
startup (in `app/main.py`'s `lifespan`, alongside `build_or_refresh_index()`).

`GET /health/budget`: sums `BudgetEntry.cost_usd` for the current UTC day
(`timestamp >= start of today UTC`), returns
`{spent: float, cap: float, exceeded: bool}` (`exceeded` always `False` when
`cap == 0.0`).

**Enforce the cap in `/chat`:** before starting the graph, if
`daily_budget_usd > 0` and today's spend already exceeds it AND the request
has no BYOK override, return `503 {"code": "BUDGET_EXCEEDED", ...}`.

**BYOK:** add `byok_provider: str | None = None, byok_api_key: str | None =
None` to `ChatRequest`. When both are present: skip the budget check, and
thread the override through `config["configurable"]["byok"] = {"provider":
..., "api_key": ...}`. In `app/core/llm.py::_call_with_fallback`, an
optional way to receive this override is needed — the cleanest path is
passing it down from the node (`intent_classifier_node`,
`sql_generator_node`, `insight_summarizer_node` all call into `chat`/
`structured_chat`, and all have access to `config` already or can gain a
`config: RunnableConfig` param the way `confirmation_gate_node` does). Add
an optional `byok: dict | None = None` kwarg to `chat`/`structured_chat`/
`stream_chat`; when given, bypass `_ordered_deployments`/fallback entirely
and call a single ad-hoc `Deployment` built via
`app.core.llm_providers.build_deployment`, passing the BYOK key through
(check `build_deployment`'s signature — it currently resolves credentials
internally from `provider_credentials`; add a keyword-only `api_key_override:
str | None = None` param threaded into the provider-specific builder
functions, defaulting to today's behavior when `None`). Never write the
BYOK key into an `llm_attempt` event, a log line, or an audit event —
grep the diff for `byok_api_key`/`api_key_override` before finishing to
confirm it's never passed to anything that logs/persists.

**Tests:** a `BudgetEntry` row is written after a real or mocked
`emit_llm_attempt` with the contextvar set; `/health/budget` sums correctly
and respects UTC-day boundaries; `/chat` returns 503/BUDGET_EXCEEDED when
over cap; a BYOK request bypasses the cap even when over budget; the BYOK
key never appears in any `BudgetEntry`/`AuditEvent` row or log record
written during a BYOK-covered test request (assert this negatively).

---

## Task 5: LLM response cache

New setting: `llm_cache_enabled: bool = True` (`QP_LLM_CACHE_ENABLED`, an
escape hatch, not a required toggle for normal operation).

New module `app/core/llm_cache.py`. Cache key: SHA-256 of
`normalized_question + "|" + conversation_context_hash + "|" + schema_hash`,
where:
- `normalized_question` = the user's message, lowercased and whitespace-
  collapsed.
- `conversation_context_hash` = a hash of `state["query_history"]` (or
  empty for the first turn) — enough that a follow-up in a different
  conversation context doesn't hit a stale cache entry from an unrelated one.
- `schema_hash` = hash of `state.get("relevant_schema", "")` for
  `sql_generator`'s cache (schema retrieval already ran by then); for
  `intent_classifier` (which runs before schema retrieval) omit this term.

Storage: an in-memory `dict` (module-level, process-lifetime) is sufficient
— Render's free tier is a single instance with no shared cache need across
processes, and the spec doesn't ask for persistence across restarts.
`{cache_key: (cached_value, provider, model)}` — enough to still emit a
faithful `llm_attempt`-shaped `cache_hit` event.

Wire into `intent_classifier_node` and `sql_generator_node`: on a cache hit,
skip the `structured_chat`/`chat` call entirely, return the cached result,
and emit a `cache_hit` event via a new `emit_cache_hit` in
`app/core/llm_events.py` (mirroring `emit_llm_attempt`'s isolated-listener
pattern) carrying `node`, `tokens_saved` (best-effort, may be `None`),
`cost_saved` (best-effort), `seconds_saved` (best-effort — track average
latency for that key's original miss, or omit if not cheaply available; note
the choice in the report rather than over-building this). On a miss, call
through as today and store the result.

**The validator, dry-run preview, and confirmation gate always run on cached
SQL** — this is automatic as long as caching only replaces the
`sql_generator_node`'s LLM call and still returns `{"generated_sql": sql}`
into the same state shape; the downstream graph edges are unchanged. Don't
special-case cached SQL anywhere in `validator_node`/`dry_run_preview_node`/
`confirmation_gate_node`.

`insight_summarizer` is never cached — don't touch
`app/agent/nodes/insight_summarizer.py`'s call path at all.

**Pre-warm at startup**: in `app/main.py`'s `lifespan`, after
`build_or_refresh_index()`, run the example prompts already shown in
`frontend/index.html` (grep it for the example-prompts list; reuse that
exact list rather than inventing a new one) through
`intent_classifier`+`sql_generator`'s cache-populate path directly (call the
node functions or the underlying `chat`/`structured_chat`, not the full
graph — avoid mutating any checkpointer state during pre-warm). Guard with
`try/except` per prompt so one failing pre-warm call never blocks startup
(same pattern as the existing `build_or_refresh_index()` try/except at
main.py:88-91).

**Tests:** identical question -> second call hits cache, `chat`/
`structured_chat` not invoked again (spy/mock assertion); a different
question -> cache miss; a cache hit still runs the SQL through
`validate_sql` (assert the full node chain, not just the cache lookup, by
running validator_node after a cached sql_generator_node result); a normalized
variant (different casing/whitespace) of an already-cached question still
hits; `insight_summarizer` is unaffected (no cache wiring touches it —
a grep-based check in the report is fine here, not necessarily a runtime test).

---

## Task 6: Phase 3 design note

Write `docs/phases/03-error-taxonomy-guardrails-limits-caching.md` following
the style of `docs/phases/01-llm-gateway.md` and
`docs/phases/02-deployment-blockers.md` (read both first): what changed and
why, the full `ErrorCode` list and what triggers each one, the guardrail's
heuristics and what they deliberately don't catch (SQL keywords — that's the
validator's job), the budget/BYOK design (contextvar-based session
attribution — explain why, not just what), the cache key composition and
what's deliberately excluded from caching (`insight_summarizer`), and
anything live-verified vs assumed (e.g. the guardrail's gibberish threshold
was tuned against which example inputs). Run the "try to break it" presets
from the original spec (`DROP TABLE orders`, "delete all orders", a
prompt-injection attempt, "update every retailer's credit limit to 0")
against the real running app and record which layer blocks each one —
this is the live-verification the non-negotiable rules require, not a
description of what should theoretically happen.

---

## Task ordering

Tasks 1-5 touch mostly-disjoint files (`app/core/errors.py`,
`app/core/guardrail.py`+node+graph edge, `app/core/rate_limit.py`,
budget/BYOK across `llm.py`/`llm_providers.py`/`main.py`, `llm_cache.py`+two
node files) but Task 1's `main.py` changes and Task 3/4's `main.py` changes
(new `Request` param on `/chat`/`/confirm`, new budget check, new BYOK
field) land in the same file's same functions — run Task 1 before 3 and 4,
and have 3/4's dispatch briefs show the post-Task-1 state of those
functions, not the plan's pre-Task-1 snippets above. Task 2's graph.py edge
change (`START -> guardrail -> intent_classifier`) doesn't conflict with
anything else. Task 5 touches `intent_classifier_node`/`sql_generator_node`
bodies — independent of 1/2/3/4's files. Task 6 last, depends on everything
being live-testable.
