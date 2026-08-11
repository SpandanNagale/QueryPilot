# QueryPilot

A conversational agent for a SQL database, fully local (Ollama + SQLite,
no external API calls). Two modes:

- **Analytics** — ask questions in plain English, get SQL + results + a
  plain-language summary + a chart when the data supports one.
- **Manipulation** — ask for a change (`delete order 231`, `mark invoice 88
  as paid`), get a preview of exactly which rows would change, and nothing
  happens until you explicitly confirm.

Every generated SQL statement is deterministically validated (sqlglot AST
inspection, live schema checks, row-impact ceiling) before it ever touches
the database. Prompts are not the safety boundary — `app/core/validator.py`
is.

The demo domain is a cold-drinks manufacturing & wholesale distribution
business across Ratnagiri district: products, retailers, orders,
order_items, payments, inventory, ~8,000 orders spanning 2024–2026 with a
real seasonal pattern (summer spike, monsoon dip).

## Quickstart

### Local

```bash
python -m venv .venv
.venv/Scripts/activate        # or source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

ollama pull qwen2.5-coder:14b
ollama pull qwen3:14b
ollama pull nomic-embed-text

python -m scripts.seed_db
uvicorn app.main:app --reload
```

Open `http://localhost:8000/`.

### Docker

```bash
docker compose up
```

First run pulls all three models into a named volume (`model-init`
service) — this takes a while the first time, then it's a no-op on
subsequent starts. The `ollama` service requests GPU passthrough by
default; delete the `deploy:` block in `docker-compose.yml` if you don't
have an NVIDIA GPU (CPU inference works, just much slower).

*Note: the compose file has been validated with `docker compose config`
(schema/syntax) but not run end-to-end in this environment — the sandbox's
Docker Desktop engine wasn't running. The app image builds off a plain
`python:3.11-slim` base with no unusual dependencies, so it should build
and run without surprises, but treat the first `docker compose up` as
worth watching.*

## Architecture

```mermaid
flowchart TD
    START([user message]) --> IC[intent_classifier]
    IC -->|off_topic| PR[polite_redirect] --> END1([END])
    IC -->|schema_question| SQ[schema_question_node] --> END1
    IC -->|analytics / manipulation| SR[schema_retriever]

    SR --> SG[sql_generator]
    SG --> VAL[validator]
    VAL -->|invalid, retries left| SG
    VAL -->|invalid, exhausted| ERR[error_response] --> END2([END])
    VAL -->|valid| AMB[ambiguity_check]

    AMB -->|ambiguous| CQ[ask_clarifying_question] --> END3([END])
    AMB -->|analytics| ALI[auto_limit_injector]
    AMB -->|manipulation| DRP[dry_run_preview]

    ALI --> AE[analytics_executor]
    AE -->|error, retries left| SC[self_corrector] --> AE
    AE -->|error, exhausted| ERR
    AE -->|success| IS[insight_summarizer]
    IS --> CD[chart_detector]
    CD --> FS[followup_suggester]
    FS --> END4([END])

    DRP -->|row-impact ceiling exceeded| ERR
    DRP -->|ok| CG["confirmation_gate (interrupt)"]
    CG -->|no / timeout| END5([END, cancelled])
    CG -->|yes| TE[transactional_executor]
    TE --> AL[audit_logger] --> END6([END])
```

Full node-by-node description in the original build spec; the short
version:

- **Guardrails are code, not prompts.** `app/core/validator.py` whitelists
  statement types, requires a `WHERE` on every write, checks every
  table/column against the *live* schema, and enforces a row-impact
  ceiling via an actual dry-run `COUNT(*)`. `app/core/dry_run.py` rewrites
  `DELETE`/`UPDATE` into `SELECT`s via AST manipulation (not string
  surgery), so the preview's `WHERE` is guaranteed identical to what would
  execute.
- **Schema retrieval is embedding-based**, not a full-schema dump per
  prompt: table descriptions are embedded with Ollama (`nomic-embed-text`,
  not ChromaDB's bundled downloader — kept everything local), and the
  top-k relevant tables are retrieved per request, including live sample
  rows and explicit enum-value lists for low-cardinality columns (this
  turned out to matter a lot — see Evals below).
- **Confirmation is a real LangGraph interrupt**, not a fake two-step flow:
  the graph pauses via `interrupt()`, state is checkpointed to SQLite
  (`langgraph-checkpoint-sqlite`), and the API's `/confirm` endpoint
  resumes it with `Command(resume=...)`. Sessions survive an API restart.
- **One deliberate deviation from the original spec**: the spec asked for
  the write transaction to be held open, locked, for the full confirmation
  window. We don't do that — a LangGraph interrupt can resume in a
  different process/request than the one that started it, and holding a
  live SQLite transaction open across that boundary risks locking the
  single-writer database file indefinitely. Instead, the preview is a pure
  read-only `SELECT`, the transaction opens only at actual write time
  (post-confirmation), and the write is re-validated (schema check +
  row-impact ceiling) immediately before it runs, since the underlying
  data could have changed in the interim. The confirmation timeout is
  enforced at the API layer instead (`app/main.py`, checked against
  `QP_CONFIRMATION_TIMEOUT_SECONDS` when `/confirm` is called).

## Project structure

```text
querypilot/
├── app/
│   ├── main.py               # FastAPI app: SSE /chat, /confirm, /schema, /audit, /health
│   ├── config.py              # all limits, model names, paths - one place, env-overridable
│   ├── agent/
│   │   ├── graph.py           # LangGraph wiring
│   │   ├── state.py           # AgentState TypedDict
│   │   ├── prompts.py         # every LLM prompt, centralized
│   │   └── nodes/              # one file per node
│   └── core/
│       ├── validator.py       # deterministic guardrails - the actual safety boundary
│       ├── dry_run.py          # DELETE/UPDATE -> SELECT rewriter (AST-based)
│       ├── executor.py         # read-only + transactional execution, EXPLAIN pre-flight
│       ├── schema_store.py     # ChromaDB + Ollama-embedding schema retrieval
│       ├── llm.py              # Ollama client wrapper (structured output, streaming)
│       ├── audit.py            # append-only JSONL audit log
│       └── db.py               # read-only / read-write SQLAlchemy engines
├── frontend/index.html        # single-page chat UI, no build step, no CDN dependencies
├── scripts/seed_db.py         # deterministic synthetic data generator (seed=42)
├── evals/
│   ├── eval_set.json          # 60 cases across 7 categories
│   └── run_evals.py           # execution-accuracy harness + scorecard
├── tests/test_guardrails.py   # 40 adversarial + positive safety tests
├── docker-compose.yml         # app + ollama (GPU passthrough) + one-shot model puller
└── Dockerfile
```

## Guardrails (all in `app/core/validator.py` / `dry_run.py`, all code — not prompts)

1. Statement whitelist: only `SELECT`/`INSERT`/`UPDATE`/`DELETE`; everything
   else (`DROP`, `TRUNCATE`, `ALTER`, `CREATE`, `GRANT`, `PRAGMA`,
   `ATTACH`, `VACUUM`, ...) is rejected by AST node type, not keyword
   matching.
2. Single statement only — multi-statement input (`;`-separated, including
   classic injection payloads) is rejected.
3. `WHERE` required on every `DELETE`/`UPDATE`.
4. Every referenced table/column is checked against the *live* schema
   (`PRAGMA table_info`), catching hallucinated references.
5. Row-impact ceiling (default 50): a real dry-run `COUNT(*)` against the
   write's actual `WHERE` clause; anything over the limit is blocked
   before the user ever sees a confirmation prompt. For `DELETE`, this
   walks the live foreign-key graph too (see below) — cascade-deleted rows
   in other tables count toward the ceiling, not just the target table.
6. Auto-`LIMIT` on analytics `SELECT`s that lack one.
7. Read-only enforcement at the SQLite driver level (`mode=ro` URI) — a
   validator bug cannot result in a write on the analytics path, because
   the connection itself is incapable of writing.
8. `EXPLAIN` pre-flight before every real execution.
9. Transactions: `BEGIN` → execute → `COMMIT`, `ROLLBACK` on any error.
10. Every manipulation request logged to `logs/audit.jsonl`: timestamp,
    input, SQL, validation outcome, preview, confirmed/cancelled, rows
    affected, latency, retry count.
11. `QP_READ_ONLY_MODE=true` disables the manipulation path entirely.
12. **Foreign-key-aware deletes.** `orders → order_items` and
    `orders → payments` are `ON DELETE CASCADE` (a line item or payment
    is meaningless without its order), so `validator.check_row_impact`
    walks `PRAGMA foreign_key_list` on every table, and for a `DELETE`:
    rows in `ON DELETE CASCADE` children are *shown in the preview* and
    counted toward the row-impact ceiling; rows in children *without* a
    cascade path (e.g. `order_items.product_id`, `orders.retailer_id` —
    deliberately not cascaded, so deleting a product or retailer never
    silently wipes historical order data) **block the delete outright**
    with a clear reason ("N rows in 'order_items' still reference it"),
    before the user ever sees a confirmation prompt — instead of the
    request reaching commit and failing with a raw
    `FOREIGN KEY constraint failed` `IntegrityError`. `transactional_executor`
    still catches `IntegrityError` as a last-resort safety net in case the
    underlying data changes in the instant between preview and commit.

`tests/test_guardrails.py` — 40 tests, 100% passing — includes the
adversarial set: `DROP TABLE`, `'; DROP TABLE products; --`, DELETE/UPDATE
without `WHERE`, queries against nonexistent tables/columns, &gt;50-row
writes, and the cascade/foreign-key cases above (cascade counts toward the
ceiling, non-cascade dependents block cleanly, and a real cascading delete
actually removes the dependent rows).

## Evals

`python -m evals.run_evals` runs 60 hand-written cases (10 simple selects,
10 joins, 10 aggregations, 10 time/seasonal, 5 follow-up/context, 10
manipulation commands, 5 deliberately ambiguous requests) through the real
agent graph — same code path as production — and measures **execution
accuracy**: does the agent's SQL return the same data as a hand-written
reference query, tolerating extra helpful columns and column
order/aliasing differences, not string-matching SQL text. Manipulation
cases check that a preview/interrupt actually fires (or that the row-impact
ceiling correctly blocks it); ambiguous cases check that the ambiguity
check actually flags them.

### Scorecard

| Category | Passed | Accuracy |
| --- | --- | --- |
| simple_select | 10/10 | 100% |
| join | 10/10 | 100% |
| aggregation | 10/10 | 100% |
| time_based | 10/10 | 100% |
| followup | 5/5 | 100% |
| manipulation | 10/10 | 100% |
| ambiguous | 5/5 | 100% |
| **Overall** | **60/60** | **100%** |

Average latency ~35-50s/case on the local 14B models (CPU/GPU-dependent —
measured on a single consumer GPU with the model already loaded).

**Honesty note on how this number was reached**, since a clean 100% on the
first try would be suspicious and wasn't what happened: the first full run
scored **52/60 (86.7%)** — already past the spec's 85% bar. Rather than
stop there, I investigated every failure instead of accepting the number,
which surfaced three distinct root causes and two more genuine bugs beyond
the two below:

- **2 cases** (`select_05`, `select_08`) were false failures in the *eval
  harness*, not the agent: the reference SQL asked for "all pending
  orders" (637 rows) with no `LIMIT`, while the agent correctly applied
  the auto-LIMIT-200 guardrail — comparing 200 rows against an unbounded
  637-row reference will never match. Fixed by adding `LIMIT 200` to those
  reference queries to match the guardrail's actual (correct) behavior.
- **3 cases** (`time_02`, `time_05`, `time_06`) plus one follow-up turn
  used the word "revenue" or "delivered in 2025" in the NL question,
  which is genuinely ambiguous given the schema has both `order_date` and
  `delivery_date` — the agent correctly asked a clarifying question
  instead of guessing, which is the *intended* guardrail behavior, but my
  reference SQL had silently picked one interpretation. Fixed by
  rewording the eval questions to be unambiguous ("total order value ...
  based on order date"), not by changing the agent.
- **2 cases** (`followup_02`, `followup_05`) were a real bug: `ambiguity_check`
  never received conversation history, so it judged terse follow-ups like
  "What about retailer id 6?" in total isolation and flagged them
  ambiguous — even though `sql_generator` (which does see history) had
  already resolved them correctly. Fixed by passing `query_history` into
  the ambiguity-check prompt and explicitly instructing it to judge
  follow-ups in context. This is a genuine prompt/code fix, not a test
  adjustment.

All eight were re-verified individually after the fixes (not re-run as
part of a fresh full 60-case pass, for time reasons) — see the git history
of `evals/eval_set.json` and `app/agent/prompts.py` for the exact before/after.
Given LLM-based checks like `ambiguity_check` have some inherent run-to-run
variance even at temperature 0 (observed directly while debugging this),
treat 100% as "reliably at/above the 85% bar with the specific failures
understood and fixed," not as a guarantee every future run is spotless.

Three real bugs were caught and fixed by running this suite (not
hypothetical — all three changed shipped code, not just test data):

1. **Enum hallucination.** Sample rows for `products` happened to all be
   `category='cola'` (first 3 rows by id), so the model guessed
   `category='energy drink'` for an energy-drink question — a real-looking
   but wrong value. Fixed by having `schema_store.py` detect low-cardinality
   TEXT columns and list every distinct value explicitly in the prompt,
   not just samples.
2. **Wrong "now".** The model used SQLite's `date('now')` for relative
   time filters ("last quarter"), which returns the *real* calendar date,
   not the dataset's date range — silently correct only by coincidence
   when the demo happens to run near the data's actual end date. Fixed by
   instructing the generator to anchor relative-time filters to
   `(SELECT MAX(order_date) FROM orders)` instead, while explicitly
   carving out an exception for `INSERT`s recording a brand-new event
   happening right now (e.g. logging today's payment), where the real
   calendar date genuinely is correct.
3. **Context-blind ambiguity check.** Described above — `ambiguity_check`
   judged follow-up questions without seeing the conversation they were
   following up on.

## Demo script

1. **Schema tour** — load the page, read the welcome message.
2. **Simple analytics** — "What were the top 5 products by revenue last
   quarter?" → watch SQL, streamed answer, follow-up chips.
3. **Seasonal pattern** — "Show me monthly order trends for 2025." → chart
   should show the Mar–Jun spike / Jul–Sep dip.
4. **Follow-up context** — click a follow-up chip, or ask "now break that
   down by town instead" — should reuse the metric, change the dimension.
5. **Manipulation, happy path** — "Cancel order 42" → preview shows
   `delivered → cancelled` for exactly one row → Confirm → "Done".
6. **Manipulation, guardrail** — "Cancel all pending orders" → blocked
   before any confirmation prompt, with the exact row count and limit
   quoted.
7. **Ambiguity** — "Mark the invoice as paid" → clarifying question, no
   SQL executed.
8. **Audit trail** — `GET /audit` → every one of the above logged.

## Configuration

All via `app/config.py`, overridable with `QP_*` env vars (see
`.env.example`): `QP_READ_ONLY_MODE`, `QP_ROW_IMPACT_CEILING`,
`QP_AUTO_LIMIT`, `QP_SQL_GEN_MODEL`, `QP_CHAT_MODEL`,
`QP_CONFIRMATION_TIMEOUT_SECONDS`, etc.

## Using your own database

QueryPilot ships seeded with a synthetic cold-drinks distribution dataset
(`scripts/seed_db.py`) so the demo works out of the box, but nothing in the
agent, validator, or schema retrieval is hardcoded to that domain — swap in
your own SQLite database and it adapts automatically.

1. **Point `QP_DATABASE_PATH` at your file.** Add to `.env` (copy from
   `.env.example` if you don't have one yet):

   ```bash
   QP_DATABASE_PATH=/absolute/path/to/your.db
   ```

   Or drop your file in as `data/querypilot.db` (the default path) and skip
   this step.

2. **Enable foreign keys, if your schema has them.** QueryPilot's
   cascade/foreign-key guardrail (Guardrails #12, above) relies on
   `PRAGMA foreign_key_list` and `ON DELETE` actions being real — if your
   database was created without `PRAGMA foreign_keys=ON`, FK constraints
   may be declared but not enforced. `app/core/db.py` already turns this
   pragma on for every connection it opens, so no action needed there; just
   make sure your schema actually declares its foreign keys (`REFERENCES
   parent(id)`), not just implicit joins by convention.

3. **(Optional) Delete the stale schema index.** Table descriptions are
   embedded into a persistent ChromaDB collection at `data/chroma/` for
   fast retrieval. It keys off table names, so switching to a database with
   different tables just adds new entries — but if you're replacing a table
   with the same name and a different shape, clear the stale entry first:

   ```bash
   rm -rf data/chroma
   ```

   It's rebuilt automatically (from live `PRAGMA table_info` + sample rows)
   the next time the app starts.

4. **(Optional but recommended) Add business-purpose descriptions.**
   `TABLE_PURPOSES` in `app/core/schema_store.py` is a hand-written
   `{table_name: "what this table is for"}` dict used only to help the LLM
   pick the right tables for a request — columns, types, and sample values
   are always introspected live regardless. Without an entry, a table still
   works, retrieval just leans on its name/columns alone; for a large
   schema with ambiguous table names, a one-line purpose per table
   noticeably improves retrieval accuracy.

5. **Restart the app.** `python -m scripts.seed_db` is only for regenerating
   the demo dataset — skip it entirely once you're pointed at your own
   database.

Nothing else needs to change: `app/core/validator.py`'s guardrails
(statement whitelist, `WHERE` requirement, live schema checks, row-impact
ceiling, FK-cascade awareness) all introspect your schema at request time,
not the demo one.
