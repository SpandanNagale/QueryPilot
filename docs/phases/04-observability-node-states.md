# Phase 4: Observability and node states

Builds on Phase 1 (LLM gateway), Phase 2 (deployment blockers) and Phase 3
(error taxonomy, guardrails, limits, caching), all merged to `main`. This
phase makes a run's internals visible over SSE: which node is doing what,
how long each step took, which provider answered, what the safety layers
individually verified, and a heartbeat so a slow multi-provider fallback
chain doesn't look like a dead connection.

## What changed and why

### `node_state` events (`app/core/node_state.py`, `app/main.py`)

Every graph node now emits `pending -> running -> succeeded | degraded |
failed | not_reached | skipped_cached` over SSE. This rides LangGraph's
`"tasks"` stream mode (added alongside the existing `"updates"`/`"custom"`
modes), which emits one event when a task starts (has an `"input"` key, no
`"result"`/`"error"` yet) and one when it finishes (`"result"`/`"error"`
present — `"result"` is the *exact* dict the node returned, so `running` vs.
`succeeded`/`failed`/`degraded` can be computed from the `"tasks"` stream
alone, with no need to cross-reference the `"updates"` stream for this).
`skipped_cached` is derived from whichever nodes had a `cache_hit` event
during that same dispatch (see below).

**`not_reached`** (`_not_reached_after` in `app/main.py`) is a small,
hand-written table of "what would have run next," keyed by the node that
just failed, rather than a generic graph-reachability walk: the graph
branches (analytics vs. manipulation) and loops (`validator <->
sql_generator`, `self_corrector -> analytics_executor`) in ways a walk can't
resolve without knowing which branch was actually taken and whether a retry
budget is exhausted. `ponytail`-flagged in-code: this mirrors
`app/agent/graph.py`'s edges by hand and needs a matching update if that
graph's shape changes. A `validator`/`analytics_executor` failure only
fires `not_reached` once its own retry budget (`max_generation_retries` /
`max_self_correction_attempts`) is actually exhausted — a mid-loop retry
isn't a terminal failure.

**A real, live-verified bug was caught and fixed here, not by
inspection:** `auto_limit_injector_node` and `dry_run_preview_node` were
made to return a `validator_verdict` key in Task 2 below, but
`app/agent/state.py`'s `AgentState` TypedDict never declared that field.
LangGraph only creates a channel for keys present in the state schema — a
node returning an *undeclared* key has it silently dropped, with no error,
before it ever reaches `graph.stream()`'s output. This was invisible in
unit tests (which construct SSE chunks by hand, bypassing the real schema)
and only surfaced running the actual compiled graph live: the
`validator_verdict` SSE event fired (correctly named, since `NODE_EVENT_MAP`
maps it off the *node* name) but its payload was missing the actual checks
data. Fixed by declaring `validator_verdict: NotRequired[dict]` and
`degraded: NotRequired[bool]` on `AgentState`. Confirmed live afterward (see
verification section) that both now carry real data.

### `validator_verdict` (`app/core/validator.py`, three node files)

`ValidationResult` gained a `checks: list[{name, passed}]` field, additive
to the existing `passed`/`reasons` contract — every existing
`tests/test_guardrails.py` assertion (`.passed`, `.reasons`) is untouched.
`validate_sql` now records `sql_parses`, `statement_type_allowed`,
`where_clause_required` (DELETE/UPDATE only — not emitted for
SELECT/INSERT, verified by test), `tables_exist`, `columns_exist`.
`check_row_impact` records `fk_cascade_clear` (DELETE only) and
`row_impact_within_ceiling`. None of this weakens or reorders any existing
check — it's a parallel bookkeeping list next to the same `fail()` calls
that were already there.

The plan named five example checks (statement type allowed, tables exist,
columns exist, auto-LIMIT injected, row impact within the ceiling); three
live inside `validate_sql` in one pass, so `validator_node` emits one
`validator_verdict` event with all of them together. The other two are
genuinely known at *different* points in the pipeline, so rather than
threading state across nodes to force one combined event, each surfaces its
own `validator_verdict` event at the node where it's actually computed:
`auto_limit_injector_node` (`auto_limit_injected`, always `"passed": true` —
it never blocks, it just records whether a LIMIT was actually added) and
`dry_run_preview_node` (bundled into that node's existing `preview` event,
since `NODE_EVENT_MAP` maps one event name per node and the preview *is*
the moment a row-impact check result matters to the user).

### `guardrail` event

One line: `guardrail_node`/`validator` weren't in `NODE_EVENT_MAP` at all
before this phase — a block was only visible via the generic `error` event,
never a pass. Added `"guardrail": "guardrail"` and `"validator":
"validator_verdict"` to the map; the existing generic per-node dispatch in
`_run_graph_stream` needed no other changes to pick them up.

### `llm_attempt` / `cache_hit` forwarded into the SSE stream

These events already existed as a process-wide pub/sub
(`app/core/llm_events.py`, wired to `app/core/budget.py` in Phase 3) but
were never forwarded to the client. New per-request buffer
(`app/core/request_context.py`: `start_event_buffer`/`push_event`/
`drain_events`, same `contextvars` pattern as the existing `session_id`
var) plus two new module-level listener functions in `app/main.py`,
registered once at startup alongside the budget listener. `register_
cache_hit_listener` had no idempotency guard before this phase (unlike
`register_llm_attempt_listener`) — harmless while nothing but tests called
it, but would have double-registered (and double-forwarded) on every
`lifespan()` re-entry once production code started calling it; fixed to
match the existing pattern.

### SSE heartbeat + producer-thread rewrite of `_run_graph_stream`

A heartbeat needs to interleave with a *single* blocking node call — a
multi-provider LLM fallback chain took 37.5s in one live-verified run below
— which a plain synchronous generator can't do (`next()` either returns the
next chunk or blocks). `_run_graph_stream` now runs `graph.stream()`
consumption on its own daemon thread, pushing `(kind, payload, buffered)`
tuples onto a `queue.Queue`; the generator polls with `queue.get(timeout=
15.0)` and yields `event: heartbeat` on every miss.

This also *simplified* an existing piece of fragile, heavily-commented
code: the old per-dispatch `set_session_id()` re-arming existed only
because Starlette's `iterate_in_threadpool` copies a fresh `contextvars`
context on every dispatch of the outer generator. Since the producer thread
is a single, persistent OS thread — never re-entered through that
threadpool machinery — `set_session_id()` once at thread start now stays
visible for that thread's entire lifetime, and the old comment/workaround
is gone. Verified safe to call `SqliteSaver` and every SQLAlchemy engine in
this app from a non-main thread: both were already opened with
`check_same_thread=False` (`app/core/db.py`, `langgraph.checkpoint.sqlite`)
specifically because the app was already dispatching across the
threadpool's many worker threads before this change.

The two existing `test_main_errors.py` tests (fake graphs whose `.stream()`
either raises immediately or yields one plain `"updates"` chunk) needed no
changes — both still pass unmodified, confirming the producer-thread
rewrite is behavior-preserving for the paths they cover.

### Per-run summary on `done`

`_run_graph_stream`'s `finally` block now builds `{run_id, total_latency_ms,
node_latencies_ms, llm_attempts: [...], cache_hits, cache_seconds_saved,
trace_url}` from the same locals accumulated while draining the queue
(node start/end timestamps from `"tasks"` events, buffered `llm_attempt`/
`cache_hit` events) and merges it into the existing `done` event payload,
rather than a separate event — the plan says "at done, emit," not "emit a
new event."

### Optional Langfuse tracing (`app/core/langfuse_sink.py`)

`LangfuseSink` is constructed unconditionally per run (cheap — the real
`Langfuse` client is a lazy, process-wide singleton built only on first use)
and every public method internally checks `is_configured()`
(`LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` both set), so call sites in
`_run_graph_stream` never need their own `if configured:` branch. One trace
per `_run_graph_stream` call (i.e. per `/chat` or `/confirm` HTTP request,
not per logical multi-turn conversation — `ponytail`: a `/chat` that
interrupts and a later `/confirm` that resumes it become two separate
traces rather than one spanning both; acceptable for per-call
observability, would need a shared run-id keyed off the pending-confirmation
state to unify). One nested span per node (`start_observation` called
directly on the parent span object — simpler than the sibling
Crewai-Research-pipeline repo's manual OTel `use_span` context-propagation
helper, which the currently-pinned `langfuse==4.15.2` no longer needs), one
nested generation per `llm_attempt`, one event per `cache_hit`/`guardrail`.
Every method is wrapped in `try/except`, logging a warning and returning —
verified live with both an unconfigured sink (skips everything, confirmed
no `_get_client()` call via a monkeypatch that raises if it's ever touched)
and a configured-but-invalid-credentials sink (real span/generation calls
succeed locally; only the SDK's own background batch export logs a 401 —
never raised into the request).

## Live verification

Run against the real compiled graph and real cloud providers (no `uvicorn`
process — `_run_graph_stream` called directly against a real
`build_graph()`, same technique `test_node_session_isolation.py` already
uses) on 2026-09-21.

### Analytics turn ("Which products are colas?")

Full `node_state` sequence observed running -> succeeded for every node on
the path (`guardrail` through `followup_suggester`), `validator_verdict`
carrying the real `checks` list, `token` events streaming
`insight_summarizer`'s real output, and a `done` summary with real
`node_latencies_ms` per node. `schema_retriever` reported `degraded` (Gemini
free-tier embedding quota was exhausted during this session, confirmed by
the logged traceback) and the run still completed correctly on the
full-schema fallback — Phase 2b's graceful-degradation contract, now
visible over SSE for the first time.

### Manipulation turn with confirm ("Update order id 5's status to
cancelled" -> confirm)

Two separate `_run_graph_stream` calls (mirroring `/chat` then `/confirm`).
Turn 1: `dry_run_preview`'s `preview` event correctly bundled a
`validator_verdict` with `row_impact_within_ceiling: true`;
`confirmation_gate`'s task-end event carried `interrupts: [...]` with
`error: None`, correctly reported as `node_state: succeeded` (an interrupt
is an intentional pause, not a failure) rather than leaving the node stuck
at `running`. Turn 2 (`Command(resume="yes")`): `confirmation_gate` resumed
and completed, `transactional_executor` and `audit_logger` both ran and
reported `succeeded`, `result` carried `"Done - 1 row(s) affected in
orders."` The interrupt/resume mechanism and the new producer-thread design
compose correctly — this was the highest-risk untested path (interrupt
handling had never been exercised against `stream_mode=["updates",
"custom", "tasks"]` before).

### Guardrail block -> `not_reached` fan-out (unit-level, fake graph)

`tests/test_node_state_events.py::test_guardrail_block_marks_downstream_
nodes_not_reached` — confirms `guardrail: failed` is followed by
`not_reached` for `intent_classifier`, `schema_retriever`, `sql_generator`,
`validator`, `ambiguity_check` (every node on the shared spine that never
ran).

### Heartbeat under real provider latency

Not staged — observed for free on the manipulation-turn run above:
`intent_classifier`'s Ollama Cloud attempt alone took 37.5s (real
structured-output validation failure against `gpt-oss:20b`, then a
successful OpenRouter fallback), during which two real `event: heartbeat`
frames were emitted at the real 15s interval before the `llm_attempt`
events and the node's `succeeded` state arrived.

### Observed again, still out of scope (first noted in Phase 3)

The same free-tier-OpenRouter `TypeError: 'NoneType' object is not
subscriptable` Phase 3's note already documented recurred during this
phase's live verification (a different free model,
`nvidia/nemotron-3-super-120b-a12b:free`, but the same `response.choices is
None` shape). `classify_exception`'s default for an unrecognized exception
is `FallbackAction.NO_ADVANCE` ("fail loudly rather than silently cycling
every deployment for an error class this gateway doesn't understand yet")
— a deliberate Phase 1 design decision, not a Phase 4 regression, and it
still degrades correctly to a client-facing `INTERNAL` error with no
traceback leaked (confirmed again this phase, now also correctly recorded
as `node_state: failed` with the exception's `str()` in Langfuse's span
detail when tracing is on). Left parked, per Phase 3's note — worth
revisiting if free-tier fallback flakiness becomes a demo-reliability
problem rather than a rare event correctly absorbed by the fallback chain
on retry.

## What to watch going forward

- `_not_reached_after`'s hardcoded path table needs a matching edit if
  `app/agent/graph.py`'s edges change shape — flagged in-code.
- Langfuse traces one HTTP call, not one logical conversation turn that
  spans a confirmation interrupt/resume — see the "Optional Langfuse
  tracing" section above.
- `cache_seconds_saved`'s per-run total in the `done` summary is
  best-effort (mirrors Phase 3's own `seconds_saved` caveat on
  `emit_cache_hit` — `tokens_saved`/`cost_saved` are `None`, not cheaply
  available without widening `app/core/llm.py`'s public return shape).
- The recurring free-tier-provider `TypeError` (see above) is still parked
  from Phase 3; two independent live sightings across two phases now.
