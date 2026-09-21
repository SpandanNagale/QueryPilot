# Phase 5: Evals in CI

Builds on Phases 1-4 (LLM gateway, deployment blockers, error taxonomy/
guardrails/limits/caching, observability), all merged to `main`. This phase
updates the eval harness to run on the new cloud stack with real per-session
sandboxing, adds a `guardrail` category and bootstrap-CI statistics, and
wires it into a GitHub Actions workflow that gates merges on regression.

## What changed and why

### `evals/stats.py` (new)

A trimmed port of the sibling Crewai-Research-pipeline repo's `eval/
stats.py` — only `bootstrap_ci` and `percentile`, the two primitives this
harness actually uses. That repo's `paired_bootstrap_ci`/
`spearman_correlation`/`weighted_cohens_kappa` exist there for judge-vs-
human calibration, which has no equivalent here (this harness compares
agent SQL results against a hand-written reference query, not a judge's
rating), so they weren't ported. Stdlib only, no new dependency.

### `evals/eval_set.json`: new `guardrail` category (8 cases)

Four should-block (prompt injection, PII email, gibberish, role-override)
and four should-NOT-block — including the three "Try to break it" preset
phrases from the deploy plan's frontend spec (`DROP TABLE orders`, "delete
all orders", "update every retailer's credit limit to 0") plus one ordinary
analytics question, to directly test the "destructive SQL must reach the
validator, not the guardrail" rule as an eval metric, not just a manual
check.

### `evals/run_evals.py`: per-session sandboxing, real confirm/commit, direct guardrail calls

- **Per-case sandbox** (`_new_case_config`): each case gets a fresh UUID4
  session and its own throwaway copy of `data/template.db` via
  `app.core.sessions.get_session_db_path` — the same mechanism a real
  visitor's browser session gets. Before Phase 2a's per-session sandboxing
  existed, this harness ran every case against the one shared template
  database directly; a manipulation case's real DELETE/UPDATE/INSERT could
  contaminate the template and every later case's reference-query results.
- **Manipulation cases now actually confirm.** The old harness only ever
  checked that a preview/interrupt fired — never resumed with
  `Command(resume="yes")`, so `transactional_executor`/`audit_logger` were
  never exercised by an eval run. Now every non-blocked manipulation case
  confirms for real (safe precisely because it's a throwaway sandbox) and
  verifies the commit: for a DELETE, the reference query's row-existence
  count must hit 0 post-commit; for UPDATE/INSERT, only that the graph
  reports `committed=True` with no error (there's no per-case "expected new
  field value" data in `eval_set.json` to check further).
- **Guardrail cases call `guardrail_node` directly**, not the full graph.
  guardrail runs before any LLM call, so "guardrail correctness" means "did
  this heuristic decide correctly" — routing it through the full graph
  first (as an early draft did) made an `expect_blocked=False` guardrail
  case fail whenever an *unrelated* downstream node crashed, which is
  exactly what happened live during development (see below). Calling the
  node directly is also immediate — the whole 8-case category runs with
  zero network calls.
- **Reports** execution accuracy (all non-guardrail categories) and
  guardrail correctness as separate top-level percentages, a 95% bootstrap
  CI per category, and p50/p95 latency.
- **CI regression gate**: fails (non-zero exit) if guardrail correctness
  drops below 100%, or execution accuracy drops more than 5 points below
  the previously-committed `evals/eval_results.json` (read before this run
  overwrites it).

### Three real bugs found and fixed during live development (not by inspection)

1. **`app/agent/nodes/dry_run_preview.py`: variable shadowing broke every
   DELETE-with-cascade preview.** Phase 4 added `impact = check_row_impact(...)`
   at the top of the node and `"validator_verdict": impact.to_dict()` at
   the bottom — but the DELETE branch in between reused the name `impact`
   for each `CascadeImpact` object in its `for impact in impacts:` loop,
   shadowing the original `ValidationResult`. Any DELETE with a cascading
   FK relationship crashed with `AttributeError: 'CascadeImpact' object has
   no attribute 'to_dict'`. Caught by `evals/run_evals.py`'s `manip_01`
   case ("Delete order 500", which cascades into `order_items`/`payments`)
   the first time this phase's rewritten harness actually ran a DELETE
   case. Fixed by renaming the loop variable to `cascade_impact`.
2. **`app/core/llm_providers.py`: an OpenAI-compatible provider returning
   an empty response aborted the entire fallback chain instead of
   advancing.** `response.choices[0].message.content` raised a raw,
   unclassified `TypeError` ('NoneType' object is not subscriptable') when
   a free-tier fallback model (observed repeatedly against openrouter's
   `nvidia/nemotron-3-super-120b-a12b:free`) returned a well-formed HTTP
   200 with `choices=None` instead of an error the `openai` SDK would
   raise for. This exact shape was first observed and parked as "out of
   scope" in Phase 3 and Phase 4's notes; it recurred a third and fourth
   time during this phase's live eval runs, at a high enough rate (2 of the
   first 2 real cases in one run) to make the eval numbers meaningless
   rather than just occasionally noisy. `classify_exception()`'s default
   for an unrecognized exception shape is `FallbackAction.NO_ADVANCE`
   ("fail loudly rather than silently cycle every deployment for an error
   class this gateway doesn't understand yet") — the right call for a
   genuinely unknown shape, but wrong here once the shape *is* known. Fixed
   by adding `EmptyProviderResponseError` (raised when `response.choices`
   is falsy, and the streaming path now skips a chunk with no choices
   instead of indexing into it) and classifying it to
   `ADVANCE_IMMEDIATELY` — a deployment-specific failure, not a malformed
   request that would fail identically everywhere. Live-verified: the two
   cases that hit this before the fix (`select_01`, `select_02`) passed
   cleanly after it, advancing straight to Gemini/Groq instead of aborting.
3. **`evals/run_evals.py`'s `_write_summary`: `SUMMARY.md`'s em dash
   mangled to `�` on Windows.** `Path.write_text()` with no `encoding=`
   uses the platform's default text encoding, which on this Windows dev
   machine is not UTF-8. Fixed by passing `encoding="utf-8"` explicitly on
   both `SUMMARY.md` and `eval_results.json`'s writes (the JSON write was
   already safe in practice — `json.dumps`'s default `ensure_ascii=True`
   escapes non-ASCII to `\uXXXX` — but made explicit for robustness anyway).

### `.github/workflows/eval.yml` (new)

Triggers on push to `main` and `workflow_dispatch`. Seeds the template DB
(`python -m scripts.seed_db`), runs `alembic upgrade head` **against the
local SQLite fallback, not the production Neon database** (`QP_DATABASE_URL`
is deliberately left unset in CI — this needs to exist only so the graph's
audit-logging nodes don't hit `OperationalError: no such table:
audit_events`, the exact gotcha Phase 3's note flagged for local
verification), runs `pytest -q`, then `python -m evals.run_evals` with
provider keys from repo secrets. The eval step uses `continue-on-error:
true` so the results/summary still get committed back to the repo even on
a detected regression (preserving the historical record instead of
discarding a bad run) — a final step then explicitly fails the job if the
eval step's outcome was a failure. Commit message includes `[skip ci]` so
the harness's own commit doesn't retrigger the workflow.

## Live verification

Full 68-case suite run against the real compiled graph and real cloud
providers (no `uvicorn` process — `evals.run_evals.main()` called directly,
same technique used in every prior phase's live verification) on
2026-09-21, after the `EmptyProviderResponseError` fix above.

```
SCORECARD
  aggregation           10/10   100.0%
  ambiguous              5/5    100.0%
  followup               4/5     80.0%
  guardrail              8/8    100.0%
  join                  10/10   100.0%
  manipulation           8/10    80.0%
  simple_select         10/10   100.0%
  time_based             9/10    90.0%
------------------------------------------------------------
  Execution accuracy: 93.3%
  Guardrail correctness: 100.0%
  Latency: mean 35.86s p50 20.62s p95 131.13s
```

Committed to `evals/eval_results.json` / `evals/SUMMARY.md` as the new
baseline future CI runs regress against. The old local-Ollama baseline (60
cases, 100% pass, mean latency 49.4s, max 835.9s) is preserved at
`evals/eval_results.local_ollama_baseline.json` rather than overwritten —
p50 latency on the cloud stack (20.6s) is faster than local Ollama's mean,
though the comparison isn't apples-to-apples: this run's latencies are
inflated by the Gemini free-tier embedding quota being fully exhausted for
the day (see below), and the two result sets score different case sets (60
vs. 68, the new 8 being the free/instant guardrail category).

All 4 individual case failures were investigated to a specific, understood
cause rather than left as an unexplained number:

- **`time_04`** ("Show monthly order counts for 2024"): `agent raised: All
  deployments for alias='chat' failed` — every one of the 4 configured
  providers failed on this specific call, including Groq hitting its own
  per-minute token rate limit (`Rate limit reached... tokens per minute
  (TPM): Limit 8000, Used 7067`). Genuine, correctly-handled infrastructure
  exhaustion (`AllProvidersFailedError`), not a code defect.
- **`followup_01`** ("What are the top 3 products by revenue?" -> "Now show
  the same for juice products only"): the agent generated `SELECT name,
  unit_price, bottle_size_ml FROM products WHERE category = 'juice' LIMIT
  200` — a plain product listing that dropped the first turn's "top 3 by
  revenue" ranking/aggregation entirely, instead of carrying that intent
  forward with the new category filter. A genuine model-quality miss on
  multi-turn context carryover, not a harness or gateway bug — the kind of
  case an eval suite exists to surface, not something Phase 5 should paper
  over by loosening the check.
- **`manip_01`** ("Delete order 500") and **`manip_10`** ("Delete order
  15") both failed on their first run with `interrupted=False,
  preview_row_count=None` and no `error` set — the harness's original
  detail string didn't capture enough to diagnose this, so a diagnostic
  improvement was added (`is_ambiguous`, `final_response` alongside
  `error`) and both cases were re-run directly. Root cause confirmed live:
  `ambiguity_check` non-deterministically classified these two cascading
  DELETEs as ambiguous on that specific run (`final_response`: "Do you want
  to delete all related order_items and payments for order 15?"), routing
  to `ask_clarifying_question` instead of `dry_run_preview`. Both cases
  passed cleanly on an immediate re-run with the exact same input, same
  code, same eval_set.json entry — confirming this is LLM
  temperature/sampling non-determinism in a genuinely defensible judgment
  call (asking about cascade behavior before a destructive delete is
  reasonable agent behavior), not a bug. `eval_set.json` doesn't mark these
  cases `expect_ambiguous`, so they're scored as failures when this
  happens; left as-is rather than "fixed" by loosening the check, since
  suppressing it would hide genuine non-determinism the CI gate's 5-point
  tolerance already exists to absorb.

### Gemini embedding quota was fully exhausted for the entire run

Every single non-guardrail case hit `429 RESOURCE_EXHAUSTED
...EmbedContentRequestsPerDayPerProjectPerModel-FreeTier...quotaValue:
1000` on its `schema_retriever` call and fell back to the full schema
(Phase 2b's graceful-degradation path, working exactly as designed) — this
is a **daily**, not per-minute, quota, so it stayed exhausted for the
entire ~100-minute run. This inflates every case's latency by the retry
backoff before the fallback kicks in (observed 15-55s per case just for
this), and is the dominant reason several individual cases show latencies
of 130-430s. Not a code defect and not something this phase should "fix" —
it's an accurate reflection of what a demo running past its free-tier quota
looks like, and Phase 2b's fallback already keeps it correct, just slow.

## What to watch going forward

- Today's run's latency numbers are not a clean baseline for future
  latency-regression comparisons — they're dominated by Gemini quota
  exhaustion, not representative steady-state performance. A future run on
  a fresh quota day would be a fairer number to trend against.
- `ambiguity_check` non-determinism on cascading DELETEs (see above) will
  periodically fail `manip_01`/`manip_07`/`manip_10` in CI even with no
  code change — expected noise the 5-point regression tolerance is
  designed to absorb, not a flake to chase.
- The `EmptyProviderResponseError` fix only covers the OpenAI-compatible
  path (OpenRouter/Groq) since that's the exact shape observed live three
  times across three phases; Ollama Cloud and Gemini have their own
  response shapes and haven't shown the equivalent failure mode.
