# Phase 6: Next.js frontend on Vercel

Builds on Phases 1-5, all merged to `main`. A new Next.js (App Router,
TypeScript, Tailwind 4) app in `web/`, talking to the FastAPI backend
directly over SSE — no Next.js API routes, no proxying. `frontend/
index.html` is untouched and still served by FastAPI as the lightweight
fallback UI.

## What changed and why

### Design tokens copied, not reinvented

`app/globals.css`'s teal/amber/coral control-room telemetry palette,
`NODE_STATE_STYLE` mapping, and the fetch-based `parseSseStream` parser are
copied close to verbatim from the sibling Crewai-Research-pipeline repo's
dashboard (read directly from its checked-out source, not from memory) —
the deploy plan asked for the same semantic tokens and dark-first theme,
and that repo's `NodeState` type is already identical to Phase 4's
`app/core/node_state.py`. Adapted, not copied blindly, per the plan's own
instruction: QueryPilot's SSE vocabulary is far richer (a dozen distinct
named events vs. one wrapped `stage_update` envelope), so `lib/pipeline.ts`
is a new reducer built around QueryPilot's actual backend event shapes,
not a port of that repo's `pipeline.ts`.

### One reducer, one unifying rule for attributing events to nodes

`app/main.py`'s SSE stream names events by *what happened*
(`sql_generated`, `preview`, `validator_verdict`, `llm_attempt`,
`cache_hit`, ...), not by *which node* — most of these carry no `node`
field in their payload. `lib/pipeline.ts` resolves this with one rule,
verified against the exact ordering `_run_graph_stream`'s producer thread
guarantees (confirmed by re-reading Phase 4's own implementation): a
`node_state` event with `state: "running"` sets `currentNode`, and it
stays set until the *next* `running` event, because a node's own semantic
event and any `llm_attempt`/`cache_hit` it triggers always arrive between
its `running` and `succeeded`/`failed`/`degraded` node_state events, never
after. Every non-`node_state` event attaches to whichever node is
currently "current." `cache_hit` is the one exception with its own `node`
field (used directly, falling back to `currentNode`).

### Two backend additions this phase needed

- **`app/agent/nodes/confirmation_gate.py`**: the interrupt payload now
  includes `confirmation_timeout_seconds` (from `settings.
  confirmation_timeout_seconds`). The plan asks for "a visible 60-second
  countdown matching `confirmation_timeout_seconds`" — without this field
  the frontend would have to hardcode or guess the configured value rather
  than actually matching it.
- Nothing else — every other piece of data the three-pane UI needs already
  existed on the wire from Phases 1-5.

### Chart and SQL syntax highlighting: hand-rolled, not a new dependency

Per the ladder (already-installed dependency, then native, then shortest
code): a chart or code-highlighting library would be the first new runtime
dependency in this frontend. Neither need justifies one here —
`chart_detector.py` only ever emits one of exactly three simple chart
types against one series (`lib/ChartView.tsx`: ~130 lines of hand-rolled
SVG, bar/line/pie), and SQL is one language, always read-only, a few lines
long (`lib/sqlHighlight.tsx`: a single-pass regex tokenizer, ~35 lines).

### React's stricter render-purity lint rules caught real issues

`eslint-config-next`'s current `react-hooks` rules (`set-state-in-effect`,
`immutability`) flagged four spots, each fixed properly rather than
suppressed:
- `Dashboard`'s session-id read from `localStorage` moved from an effect
  into a `useState` lazy initializer — safe here specifically because the
  three-pane layout that displays it never renders until after the
  cold-start gate's async `waitForBackend()` resolves, well past initial
  hydration, so there's no server/client text mismatch risk.
- `Dashboard`'s "bump a refresh key when a turn settles" and
  `ProviderBudgetPanel`'s "auto-open the BYOK form when the budget is
  exceeded" both moved from `useEffect` + `setState` into React's own
  documented "adjust state during render" pattern (comparing against a
  `prevStatus` ref-like state value, or deriving the displayed value
  directly instead of syncing a second piece of state).
- `ChartView`'s pie-slice angle accumulator changed from a mutated closure
  variable to a pure, index-based computation.
- `sqlHighlight`'s shared module-level `RegExp` (whose `g` flag makes it
  stateful via `lastIndex`) moved to a fresh instance per render.

### Playwright smoke tests: real backend for infra, mocked SSE for the LLM path

Live-verified through extensive manual testing (see below) that a single
real turn can legitimately take 30-250+ seconds end to end under today's
free-tier provider conditions, with occasional flakiness already
documented in Phases 3-5's notes. Re-running that live, non-deterministic
path per Playwright test would make CI slow, costly, and flaky — and
would blur two different testing concerns: whether the *agent* is correct
(`evals/run_evals.py`'s job, already covering this exhaustively) versus
whether the *frontend* correctly renders a given, known event sequence
(this phase's job). `playwright.config.ts` starts both the real Next.js
dev server and the real FastAPI backend (`webServer`, satisfying "run
against a locally started backend"); `/health`, `/tables`, `/session/
reset`, `/health/providers`, `/health/budget` are hit for real (fast, no
LLM calls) — only `/chat` and `/confirm`'s SSE bodies are intercepted
with canned, deterministic fixtures (`tests/fixtures/sse.ts`) built in
`_sse()`'s exact wire format. All 7 tests (cold-start x2, one analytics
query, one manipulation confirm, one cancel, one blocked attack, one
sandbox reset) run in under 2 seconds total.

### `.github/workflows/frontend-tests.yml` (new)

Separate from `eval.yml` (backend evals): seeds the template DB, runs
migrations against the local SQLite fallback (same reasoning as
`eval.yml` — CI never touches the production Neon DB), installs Node
dependencies and Playwright's Chromium, builds the frontend, then runs the
Playwright suite. No provider API keys needed or requested as secrets —
confirmed live that the mocked-SSE tests never reach a real provider, and
the backend's own startup-time cache pre-warm/schema-embedding calls are
already wrapped in try/except that can't crash the server on missing
credentials (verified by reading `app/main.py`'s `lifespan`, not assumed).

## Live verification

Full manual run through the real app (real backend, real cloud providers,
`uvicorn` + `next dev` both running locally) on 2026-09-21, via browser
automation, not just static review:

- **Analytics turn** ("Which products are colas?"): every node transitioned
  running → succeeded live in the trace pane; SQL rendered with syntax
  highlighting and four passing validator badges; a 10-row results table;
  a rendering SVG bar chart; streamed insight text; three clickable
  follow-up chips; a run summary reading "96.4s total · via gemini,
  ollama_cloud · 1754 tok · $0.0001 · 1 cache hit (28.0s saved) · view
  trace" with a working Langfuse trace link. The SSE heartbeat's "still
  working…" indicator correctly appeared during a 57.6s `intent_classifier`
  call — direct visual confirmation that Phase 4's heartbeat keeps the
  connection alive and the UI honest during real worst-case provider
  latency, not just passing in a unit test.
- **Manipulation with confirm** ("Record a cash payment of 100 for order
  3"): reached the confirmation card with a live countdown, real preview
  rows, and syntax-highlighted SQL; confirming ran `transactional_executor`
  and `audit_logger` for real — `payments`' row count in the table browser
  changed 6003 → 6004 live, then back to 6003 after a sandbox reset in a
  later step, confirming the write, the display, and the reset all
  genuinely round-trip through the real per-session sandbox.
- **A real self-correction + not_reached sequence** ("DROP TABLE orders"):
  live-verified something no smoke test could stage — the guardrail
  correctly let it through (not blocked, matching the "guardrail never
  touches SQL content" rule), the validator blocked the literal `DROP`,
  the model then self-corrected to a `DELETE FROM orders WHERE id IS NOT
  NULL`, which the validator accepted but `dry_run_preview` blocked on
  the row-impact ceiling (42,140 rows including cascades) — and
  `confirmation_gate`/`transactional_executor`/`audit_logger` all
  rendered with the "not_reached" grey styling live, exactly matching
  Phase 4's `_not_reached_after` design and its own unit tests, now
  confirmed against a real multi-attempt run rather than a fake graph.
- **Sandbox reset**: real `POST /session/reset` call, new UUID4 displayed,
  table browser re-fetched against the new session automatically.

### A real bug found only by watching the real UI, not by review

`ResultsPane` was hiding a completed write's `final_response` ("Done - 1
row(s) affected in payments.") whenever `execution_result` was also
present — which is *always* true for `transactional_executor` (it sets
both together). The condition read cleanly in isolation but was wrong in
practice: confirming a real INSERT showed an empty results table and no
success message at all. Fixed by always showing `final_response` when
present, and only rendering the results table when the execution result
actually has columns (a write's `execution_result.columns` is `[]` by
construction — confirmed by reading `app/core/executor.py::execute_write`
rather than assumed).

## What to watch going forward

- `lib/pipeline.ts`'s `currentNode`-attribution rule depends on the exact
  event ordering `_run_graph_stream`'s producer thread guarantees today;
  a future change to that ordering (e.g., buffering llm_attempt/cache_hit
  differently) would need a matching frontend update.
- The Playwright suite's mocked `/chat`/`/confirm` fixtures
  (`tests/fixtures/sse.ts`) are hand-maintained against the backend's
  event shapes — they don't automatically stay in sync with a future
  backend change the way a shared schema would. A drift here would show
  up as the frontend failing to render a real response the same way it
  renders the fixture, without Playwright itself catching it.
- Mobile-width visual verification: claude-in-chrome's `resize_window`
  didn't actually change the rendered viewport in this session (the
  screenshot stayed desktop-sized despite a reported-successful resize) —
  switched to the chrome-devtools MCP's `emulate` (390×844, mobile+touch)
  instead, which did render at the real mobile viewport. Confirmed live:
  the three panes stack vertically in the documented order (sandbox →
  trace → results), no horizontal overflow, composer and prompt chips
  full-width with correct side gutters, session card and table list
  readable. Worth remembering which tool actually honors a viewport
  resize if this comes up again.
