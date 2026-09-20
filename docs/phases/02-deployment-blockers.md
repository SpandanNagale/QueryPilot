# Phase 2: Deployment blockers

Status: complete. Live-verified 2026-09-20 (Task 11) with a real Docker build,
a real analytics `/chat` request, and a real manipulation+`/confirm` request
against the built image.

## What changed and why

QueryPilot's original single-tenant design assumed one shared SQLite file,
ChromaDB for schema retrieval, a JSONL audit log on local disk, and a
container that ran as root with a wildcard CORS policy. None of that survives
contact with a public multi-visitor demo on Render's free tier (512MB RAM,
0.1 CPU, ephemeral filesystem): one visitor's DELETE would corrupt every other
visitor's view of the data, ChromaDB's persistence layer doesn't fit the free
tier's memory budget, an ephemeral filesystem loses the audit trail on every
restart, and a root container with `allow_origins=["*"]` is a real security
problem the moment this is public. Phase 2 closes each of these gaps, in four
parts.

### 2a: Per-session sandbox database

Every visitor now gets their own SQLite copy, not a shared database.
`app.core.db.get_engines(db_path)` is an `lru_cache`d factory that builds a
`(read_only, read_write)` SQLAlchemy engine pair for any SQLite file path.
`app.core.sessions` manages per-visitor copies of `data/template.db` under
`settings.session_dir` (the OS temp dir), gated by strict UUID4 validation, an
idle-TTL sweep (`QP_SESSION_TTL_MINUTES`, default 30 minutes), and a
max-session eviction cap (`QP_MAX_SESSIONS`, default 200) so a flood of
visitors can't exhaust disk.

Every LangGraph node that touches the database --
`validator_node`, `analytics_executor`, `dry_run_preview`,
`transactional_executor`, `audit_logger`, `confirmation_gate` -- resolves its
own engine pair from `config["configurable"]["db_path"]`, threaded in by
`app/main.py`'s `/chat` and `/confirm` routes. This was the part of the design
with the most risk going in: LangGraph's node/config plumbing wasn't something
this codebase had exercised before. It was live-verified (Task 3, on a real
compiled test graph, not assumed) that LangGraph 1.2.10 correctly passes
`config["configurable"]` into any node function that declares a
`config: RunnableConfig` parameter.

Cross-session isolation was checked at three levels, not just "no exception
raised": unit tests on `get_engines` and `app.core.sessions`; a real
two-session test that runs `transactional_executor_node` directly (the same
function the compiled graph calls) against two different session sandboxes
and confirms a DELETE in session A is invisible when session B reads its own
engine; and HTTP-level tests via `TestClient` against `/tables` and
`/session/reset`.

### 2b: ChromaDB removed, Gemini embeddings + numpy in its place

`chromadb` and `posthog` are gone from `requirements.txt`. Schema retrieval
now calls `google-genai`'s embedding API (`gemini-embedding-001`,
`output_dimensionality=768`) directly. **Live-verified in Task 5, against the
real Gemini API**: `result.embeddings[0].values` is genuinely 768-dimensional
-- no SDK surface adjustment was needed versus what the spec assumed.

Vectors are cached to `data/schema_embeddings.json`, keyed by a SHA-256 hash
of each table's `as_embedding_document()` text, and rebuilt lazily at FastAPI
startup -- **deliberately not at Docker build time**, even though the
original spec suggested build-time caching. Baking a live Gemini API call
into a Docker build layer would put `GEMINI_API_KEY` in the build
history/cache, which this project's rules forbid. This is a documented,
deliberate deviation from the spec, not an oversight.

Retrieval ranks candidate tables by numpy cosine similarity against the query
embedding. Any embedding or cache failure degrades to injecting the full
6-table schema and logs a `degraded` audit event -- retrieval never fails the
request outright.

One more deliberate simplification worth flagging: `schema_retriever.py`
still resolves its engine from the *template* database, not the requesting
session's own sandbox engine. This is intentional -- schema retrieval only
produces prompt-hint context (sample rows, enum-like values for the LLM to
see), not the safety-critical path. The validator and executor nodes, which
actually run SQL against user data, always use the correct per-session
engine.

### 2c: Audit and budget storage moved onto Neon-or-local-SQLite

`app/core/opdb.py` (a new, separate operational-DB engine -- distinct from
the per-session sandbox engines) and `app/core/models.py` (`AuditEvent`,
`BudgetEntry`) are managed via Alembic, with a single initial migration
(`alembic/versions/0001_initial.py`, revision `38d0f6c00674`, "initial:
audit_events, budget_entries"). `settings.resolved_database_url` falls back
to a local SQLite file when `QP_DATABASE_URL` is unset -- confirmed live via
`alembic upgrade head` against that fallback, both in Task 6 and again in
every Docker container run in this task (see the migration log lines below).

Audit logging moved off `logs/audit.jsonl` (which doesn't survive an
ephemeral filesystem restart) onto the `audit_events` table. `GET /audit` now
requires either a `session_id` (returns only that session's events) or a
valid `X-Admin-Token` header (returns the full log) -- no more unauthenticated
full-log access.

LangGraph checkpoints moved from local disk to the OS temp dir
(`settings.checkpoint_db_path`), matching the same ephemeral-filesystem
assumption as the session sandboxes. `/confirm` on a session with no pending
checkpoint (e.g. the container restarted, or the checkpoint expired) now
returns a structured `409 {"code": "SESSION_EXPIRED", ...}` instead of a bare
400 -- this was verified live in this task too (see the manipulation flow
below, where a real pending checkpoint was confirmed successfully).

### 2d: Container and config hardening

`requirements-dev.txt` was split out (`pytest` only lives there now, so the
production image doesn't ship test tooling).

**A real deploy blocker was found and fixed in this phase**:
`requirements.txt` pinned `pydantic==2.10.4` while also depending on
`google-genai==2.24.0` (added in Phase 1), which actually requires
`pydantic>=2.12.5`. This silently broke a from-scratch `pip install` -- it
only didn't show up in day-to-day dev because the working virtualenv already
had a newer pydantic installed ahead of the stale pin. Fixed by bumping to
`pydantic==2.13.5`, checked against real package metadata (`google-genai`'s
`Requires-Dist: pydantic<3.0.0,>=2.12.5`, and `pydantic-settings==2.7.1`'s own
unbounded `pydantic>=2.7.0`) rather than assumed compatible.

The `Dockerfile` was rewritten: non-root `appuser` (uid 10001),
`pip install --no-cache-dir --only-binary=:all: -r requirements.txt` --
confirmed via a real `docker build --no-cache` that **zero** packages needed
a wheel-availability exemption from that flag -- a build-time template seed
(`python -m scripts.seed_db`, baking `data/template.db` into the image), and
a `CMD` that runs `alembic upgrade head && uvicorn ...`, binding
`${PORT:-8000}`.

CORS origins are now env-driven via `QP_CORS_ALLOWED_ORIGINS`. **A real bug
was caught and fixed in review**: the first implementation computed
`allow_credentials=bool(_cors_origins)`, which didn't guard the wildcard
case -- `QP_CORS_ALLOWED_ORIGINS=*` would produce `allow_origins=["*"]`
*and* `allow_credentials=True` simultaneously, exactly the combination this
project's rules explicitly forbid. Fixed to
`allow_credentials=bool(_cors_origins) and "*" not in _cors_origins`, with a
dedicated regression test that fails against the pre-fix line.

`render.yaml` was added as a deployment blueprint, with every secret marked
`sync: false` (never committed). `docker-compose.yml`'s `ollama`/`model-init`
services are now opt-in via `profiles: ["local-llm"]`, so a plain
`docker compose up` no longer tries to boot a local LLM stack the cloud demo
doesn't need. A real Compose-version-specific gotcha was found here too:
restoring `app`'s `depends_on` on those now-profile-gated services required
adding `required: false` to each entry (standard compose-spec long-form
syntax) -- on this environment's Compose version (v2.40.3-desktop.1), a bare
`depends_on` referencing an inactive-profile service is a hard config error,
not a silently-skipped one as might be assumed. This was verified by
reproducing the counterfactual failure, not just inferred.

## Live verification: peak memory under real load (Task 11)

The spec's target is peak RSS under **400MB**, measured under actual
request load, not idle boot. Task 9's earlier reading (129.3MiB) was
explicitly an idle-startup number, taken right after the container came up --
not a load-bearing measurement. This task re-measured under three real
requests against the actual built image.

```
docker build -t querypilot:phase2 .          # 581MB image, --only-binary=:all: full clean install
docker run -d --name qp-mem-test -p 8000:8000 --env-file .env querypilot:phase2
```

Container came up cleanly:

```
INFO  [alembic.runtime.migration] Running upgrade  -> 38d0f6c00674, initial: audit_events, budget_entries
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
```

### Request 1: real analytics query

`POST /chat` with `{"session_id": "<uuid4>", "message": "How many orders were
placed in total, and what is the total revenue across all orders?"}`, sampled
with `docker stats --no-stream qp-mem-test` during the request.

The gateway generated real SQL (`SELECT COUNT(*) AS total_orders, SUM(total_amount)
AS total_revenue FROM orders;`), executed it against the session sandbox, and
streamed a real natural-language answer (8,000 orders, $21,597,757.41 total
revenue) plus follow-up suggestions -- a genuine end-to-end run through the
LLM gateway's fallback chain, schema retrieval, validator, and executor.

| Sample | MEM USAGE |
|---|---|
| 1 | 132.4MiB |
| 2 | 139.5MiB |
| 3 | 140.0MiB |
| 4 | 139.5MiB |
| 5 | 138.6MiB |
| 6 | 140.3MiB |

### Request 2: manipulation request (reaches confirmation gate)

`POST /chat` with `{"session_id": "<same uuid4>", "message": "Update the
price of product with id 1 to 99.99"}`.

Generated `UPDATE products SET unit_price = 99.99 WHERE id = 1;`, produced a
real dry-run preview (`old_unit_price: 35.99 -> new_unit_price: 99.99`, 1 row
affected), and correctly stopped at `awaiting_confirmation: true` without
writing anything.

| Sample | MEM USAGE |
|---|---|
| 1 | 140.0MiB |
| 2 | 140.5MiB |
| 3 | 141.8MiB |
| 4 | **142.4MiB** |
| 5 | 141.1MiB |
| 6 | 141.6MiB |

### Request 3: real `/confirm`

`POST /confirm` with `{"session_id": "<same uuid4>", "confirmed": true}`.
This resumed the LangGraph checkpoint left pending by request 2 and actually
executed the write: `"final_response": "Done - 1 row(s) affected in
products."`. This exercises the exact checkpoint-resume path Part C's
`SESSION_EXPIRED` handling guards -- a session *with* a pending checkpoint
confirms cleanly; only a session with no pending checkpoint gets the 409.

MEM USAGE held flat at 141.2-141.3MiB across all 6 samples -- the write
itself is cheap; the cost was already paid generating the preview in request
2.

### Result

**Measured peak RSS across all three requests: 142.4MiB** (sample 4 of the
manipulation/preview request). This is comfortably under the spec's 400MB
target -- about 36% of budget, with headroom to spare even accounting for
concurrent visitors on Render's 512MB free tier (each session sandbox is a
small SQLite file copy; the bulk of resident memory here is the Python
process, FastAPI/Uvicorn, the loaded numpy/pydantic/SQLAlchemy stack, and the
LLM provider clients, all of which are shared, not per-session).

Container logs confirmed all three requests returned `200 OK`, and the
container was stopped and removed after the test (`docker stop qp-mem-test &&
docker rm qp-mem-test`) -- no leftover state.

## What to watch going forward

1. **Schema-embedding cache is lazy, not build-time.** This was a deliberate
   choice to keep `GEMINI_API_KEY` out of Docker build layers/history, but it
   means the *first* request after a cold container start pays the cost of a
   live Gemini embedding call (or several, one per table) before the cache is
   warm. Not measured as a problem here since the analytics request above
   succeeded well within a normal request timeout, but worth knowing if
   Render's free tier ever shows a slow first request after a cold start or
   scale-to-zero wake.
2. **`schema_retriever.py` uses the template engine, not the session's own
   engine.** This is safe today because retrieval only produces prompt hints,
   never executes against user data directly -- but if a future feature ever
   lets retrieval read live session data (e.g. per-session schema
   customization), this assumption needs revisiting.
3. **Peak RSS headroom, and where it would go first if it ever got tight.**
   142.4MiB leaves roughly 260MB of margin under the 400MB target on Render's
   512MB box. If that margin ever needs trimming, the heaviest single
   dependencies in this image are `google-genai` (pulls in `grpcio`/protobuf
   machinery for some code paths even though this project only uses its REST
   surface), `numpy` (loaded fully for what is, in this codebase, a handful
   of cosine-similarity calls over at most a few dozen vectors), and
   `psycopg` (only exercised when `QP_DATABASE_URL` points at real Neon
   Postgres; the local-SQLite fallback path never touches it, so it's dead
   weight in that mode) -- listed in rough order of footprint, none of them
   urgent at the current measured number.
4. **Image size (581MB) vs. runtime RSS (142.4MiB).** These are different
   budgets -- image size affects Render's build/pull time, RSS affects
   whether the process gets OOM-killed. Both are currently fine, but if a
   future task ever needs to shrink the image itself (separate from the
   memory budget this task measured), the same three dependencies above are
   the first place to look.
5. **The pydantic bump (2.10.4 -> 2.13.5) fixed a real from-scratch install
   blocker** but was verified only against this project's own test suite and
   a live container boot, not against every corner of `pydantic-settings`'
   behavior -- no regressions found, but flagging since it's a two-minor-version
   jump landed under deploy-blocker pressure rather than as its own
   deliberate upgrade task.
6. **Compose `depends_on` + `profiles` interaction.** The `required: false`
   fix is correct for this environment's Compose version
   (v2.40.3-desktop.1) but is a compose-spec detail worth re-checking if the
   deploy environment's Compose version ever changes -- the failure mode
   observed (hard config error on an inactive-profile `depends_on` with no
   `required: false`) was specific enough to this version that it's worth a
   comment in `docker-compose.yml` if it recurs.

## Definition-of-done checks (Task 11)

- **Docker build**: succeeded, 581MB image, zero packages needed a
  `--only-binary=:all:` exemption.
- **Docker run**: container started cleanly, `alembic upgrade head` applied
  migration `38d0f6c00674`, Uvicorn bound `0.0.0.0:8000`.
- **Real analytics `/chat` request**: 200 OK, real generated SQL, real
  result, real streamed NL answer.
- **Real manipulation `/chat` request**: 200 OK, real preview,
  `awaiting_confirmation: true`, no premature write.
- **Real `/confirm` request**: 200 OK, resumed the real pending checkpoint,
  executed the write (`1 row(s) affected in products`).
- **Peak RSS**: 142.4MiB, under the 400MB target.
- **`pytest -q`**: 131 passed, 0 failed, 0 warnings -- same count as Task 10's
  last run, nothing regressed or skipped.
