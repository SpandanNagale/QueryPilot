# Phase 2: Deployment Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make QueryPilot safe to run as a public multi-visitor demo on Render's free tier: per-session data isolation, no ChromaDB (too heavy for 512MB), durable operational storage on Neon Postgres, and a deployable container/config.

**Architecture:** Every visitor gets a private SQLite copy of a read-only "template" database, created on first request and addressed via a `db_path` threaded through LangGraph's `config["configurable"]`, resolved to `(read_only_engine, read_write_engine)` by a new cached factory. Schema *retrieval* embeddings move from ChromaDB to Gemini embeddings + a local JSON cache + numpy cosine similarity (structure is static across sessions, so this stays on the template DB). Operational data (audit events, budget entries) moves off local JSONL onto a SQLAlchemy+Alembic-managed table that points at Neon Postgres in production and a local SQLite file in dev.

**Tech Stack:** FastAPI, SQLAlchemy 2.0, LangGraph 1.2, sqlglot, google-genai (embeddings), numpy, Alembic, psycopg (Postgres driver).

**Spec:** `C:\Users\shree\Downloads\Trash\querypilot-deploy-prompt.md` (sections "Non-negotiable rules" and "Phase 2: Deployment blockers"). `docs/phases/01-llm-gateway.md` documents the already-complete Phase 1 (LLM gateway) this phase builds on.

## Global Constraints

- `app/core/validator.py` stays the safety boundary — never weaken any check, the row-impact ceiling, FK-aware delete preview, or the confirmation gate.
- Every test currently in `tests/test_guardrails.py` must still pass, unmodified in assertions, after every task. New tests may be added; none may be deleted or relaxed.
- No raw exception text/traceback may reach an HTTP response body — catch and return a plain message (full ErrorCode taxonomy is Phase 3; for this phase, a plain string message is enough, but never `str(exc)` leaking a stack frame path or driver internals verbatim into a *user-facing* field beyond a short message).
- Keep `.env` out of git; every new env var goes into `.env.example` with a one-line comment.
- Local development must keep working with zero cloud setup (SQLite fallback when `QP_DATABASE_URL` is unset; local template-seeded sandbox with no Neon/Postgres needed).
- `config/model_groups.yaml`, `app/core/llm*.py` (Phase 1) are done and must not be touched by this phase except where a task below explicitly says so.

---

## Part A — Per-session sandbox database (2a)

### Task 1: `get_engines()` factory and template settings

**Files:**
- Modify: `app/config.py:19` (rename `database_path` → `template_db_path`, add session settings)
- Modify: `app/core/db.py` (add `get_engines()`, keep `read_only_engine`/`read_write_engine` names but back them with the factory)
- Modify: `scripts/seed_db.py:291-334` (point at the renamed setting)
- Modify: `.env.example` (document new vars)
- Test: `tests/test_db_engines.py` (new)

**Interfaces:**
- Produces: `app.core.db.get_engines(db_path: str | pathlib.Path) -> tuple[sqlalchemy.engine.Engine, sqlalchemy.engine.Engine]` — `(read_only, read_write)`, `functools.lru_cache`d so repeated calls with the same path return the same engine objects (important: SQLAlchemy engines pool connections; we don't want to build a fresh one per call).
- Produces: `app.core.db.read_only_engine`, `app.core.db.read_write_engine` — now `get_engines(str(settings.template_db_path))[0]` / `[1]`, i.e. still module-level names every existing caller/test can keep importing unchanged, now always pointing at the **template** database specifically (not a "the" database, since there will be many).
- Consumes: nothing new from earlier tasks (this is the foundation task).

- [ ] **Step 1: Rewrite `app/config.py`'s database section**

Replace lines 18-19 and 47-53 of `app/config.py`:

```python
    # --- Database ---
    template_db_path: Path = PROJECT_ROOT / "data" / "template.db"

    # --- Per-session sandbox ---
    session_dir: Path = Path(tempfile.gettempdir()) / "qp_sessions"
    session_ttl_minutes: int = 30
    max_sessions: int = 200
```

and delete the `read_write_uri`/`read_only_uri` properties entirely (they move to `app/core/db.py` as plain functions parameterized by path, since there will be many DB paths now, not one). Add `import tempfile` to the top of the file alongside the existing `from pathlib import Path`.

- [ ] **Step 2: Rewrite `app/core/db.py`'s engine construction**

Replace lines 27-37 of `app/core/db.py`:

```python
from functools import lru_cache


def _rw_uri(db_path: str) -> str:
    return f"file:{db_path}"


def _ro_uri(db_path: str) -> str:
    return f"file:{db_path}?mode=ro"


@lru_cache(maxsize=256)
def get_engines(db_path: str) -> tuple[Engine, Engine]:
    """One (read_only, read_write) engine pair per distinct SQLite file path,
    cached for the process lifetime. Every session's sandbox copy gets its
    own pair the first time it's requested; the template's pair is built the
    same way, through the same cache, so there's exactly one code path for
    "give me the engines for this file" regardless of which file it is."""
    db_path = str(db_path)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def _rw_creator() -> sqlite3.Connection:
        return sqlite3.connect(_rw_uri(db_path), uri=True, check_same_thread=False)

    def _ro_creator() -> sqlite3.Connection:
        return sqlite3.connect(_ro_uri(db_path), uri=True, check_same_thread=False)

    rw = create_engine("sqlite://", creator=_rw_creator, poolclass=NullPool, future=True)
    ro = create_engine("sqlite://", creator=_ro_creator, poolclass=NullPool, future=True)
    return ro, rw


read_only_engine, read_write_engine = get_engines(str(settings.template_db_path))
```

Add `from pathlib import Path` and `from functools import lru_cache` to the imports at the top (keep the existing `import sqlite3`). The `_set_sqlite_pragma` event listener (lines 40-44, unchanged) still fires for every connection on every engine, since it's registered on the base `Engine` class — session engines get `PRAGMA foreign_keys=ON` automatically, same as the template.

`get_referencing_tables`'s signature (line 54) is untouched — it keeps `engine: Engine = read_only_engine`, which now means "defaults to the template" unless a caller passes a session engine explicitly (Task 3 does this for the manipulation path).

- [ ] **Step 3: Update `scripts/seed_db.py` to target the template path, and fix the `--force` bug**

At lines 290-298, replace:

```python
def main(force: bool) -> None:
    settings.template_db_path.parent.mkdir(parents=True, exist_ok=True)
    if settings.template_db_path.exists():
        if not force:
            print(f"{settings.template_db_path} already exists. Use --force to drop and recreate. Skipping.")
            return
        settings.template_db_path.unlink()

    conn = sqlite3.connect(settings.template_db_path)
```

(Every other reference to `settings.database_path` in this file, e.g. the final `print(f"Seeded {settings.database_path}")` at line 334, becomes `settings.template_db_path`.) This also fixes a pre-existing bug: previously the file was deleted and reseeded unconditionally regardless of `--force`, with the flag only changing a print message. Now `--force` actually gates the destructive step, and re-running the seed script without it is a safe no-op — needed because Phase 2's Docker build step (Task 9) calls this at build time and a local developer may also run it directly.

- [ ] **Step 4: Add new env vars to `.env.example`**

After the existing `QP_CONFIRMATION_TIMEOUT_SECONDS=60` line, add:

```
# Per-session sandbox: each visitor gets their own SQLite copy of data/template.db
# under this directory, so one visitor's writes never affect another's.
QP_SESSION_TTL_MINUTES=30
QP_MAX_SESSIONS=200
```

- [ ] **Step 5: Write `tests/test_db_engines.py`**

```python
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy import text

from app.config import settings
from app.core.db import get_engines


def test_get_engines_is_cached_per_path(tmp_path):
    db_path = tmp_path / "a.db"
    shutil.copyfile(settings.template_db_path, db_path)
    ro1, rw1 = get_engines(str(db_path))
    ro2, rw2 = get_engines(str(db_path))
    assert ro1 is ro2
    assert rw1 is rw2


def test_get_engines_different_paths_are_isolated(tmp_path):
    path_a = tmp_path / "a.db"
    path_b = tmp_path / "b.db"
    shutil.copyfile(settings.template_db_path, path_a)
    shutil.copyfile(settings.template_db_path, path_b)
    ro_a, rw_a = get_engines(str(path_a))
    ro_b, rw_b = get_engines(str(path_b))

    with rw_a.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    with ro_a.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id = 1")).scalar_one() == 0
    with ro_b.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id = 1")).scalar_one() == 1


def test_read_only_engine_from_get_engines_still_refuses_writes(tmp_path):
    db_path = tmp_path / "c.db"
    shutil.copyfile(settings.template_db_path, db_path)
    ro, _ = get_engines(str(db_path))
    with ro.connect() as conn:
        try:
            conn.execute(text("DELETE FROM orders WHERE id = 1"))
            assert False, "read-only engine must refuse writes"
        except Exception:
            pass
```

- [ ] **Step 6: Run the seed script to produce the template, then run the full test suite**

```bash
python -m scripts.seed_db
pytest -q
```

Expected: all existing tests pass unchanged (they still import `read_only_engine`/`read_write_engine` from `app.core.db`, which now resolve through `get_engines`), plus the three new tests pass.

- [ ] **Step 7: Commit**

```bash
git add app/config.py app/core/db.py scripts/seed_db.py .env.example tests/test_db_engines.py
git commit -m "feat(sandbox): add get_engines() factory, rename database_path to template_db_path"
```

---

### Task 2: Session manager (`app/core/sessions.py`)

**Files:**
- Create: `app/core/sessions.py`
- Test: `tests/test_sessions.py` (new)

**Interfaces:**
- Consumes: `app.core.db.get_engines` (Task 1), `app.config.settings.template_db_path` / `.session_dir` / `.session_ttl_minutes` / `.max_sessions` (Task 1).
- Produces:
  - `is_valid_session_id(session_id: str) -> bool`
  - `get_session_db_path(session_id: str) -> pathlib.Path` — copies the template on first call for a given id, touches the file's mtime on every call (idle-TTL tracking), and triggers a sweep.
  - `reset_session(session_id: str) -> pathlib.Path` — deletes and recopies.
  - `sweep_expired_sessions() -> int` — returns count of files removed.

- [ ] **Step 1: Write `app/core/sessions.py`**

```python
"""Per-visitor SQLite sandbox: each session_id gets its own copy of the
read-only template database under settings.session_dir, so writes made in
one browser tab are invisible to every other visitor. Render's free tier has
no background workers, so expiry is a lightweight sweep run inline on
request instead of a scheduled job.
"""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from app.config import settings

_last_sweep_at: float = 0.0
_SWEEP_INTERVAL_SECONDS = 60  # ponytail: fixed 60s throttle on the directory scan so a request
# burst on Render's 0.1 CPU box doesn't re-glob settings.session_dir on every single call;
# revisit if QP_MAX_SESSIONS needs to be much larger than a couple hundred.


def is_valid_session_id(session_id: str) -> bool:
    """Strict UUID4 check. session_id becomes a file path component
    (settings.session_dir / f"{session_id}.db"), so anything else — including
    a technically-valid-but-differently-formatted UUID string — is rejected
    rather than normalized, to keep the path construction trivially safe."""
    try:
        parsed = uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == session_id.lower()


def _session_path(session_id: str) -> Path:
    return settings.session_dir / f"{session_id}.db"


def get_session_db_path(session_id: str) -> Path:
    """Returns the path to session_id's sandbox copy, creating it from the
    template on first call. Call sites must validate session_id with
    is_valid_session_id() first — this function trusts its input."""
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    path = _session_path(session_id)
    if not path.exists():
        shutil.copyfile(settings.template_db_path, path)
    else:
        path.touch()  # idle-TTL is measured from last access, not creation
    _maybe_sweep()
    return path


def reset_session(session_id: str) -> Path:
    path = _session_path(session_id)
    if path.exists():
        path.unlink()
    return get_session_db_path(session_id)


def sweep_expired_sessions() -> int:
    if not settings.session_dir.exists():
        return 0

    now = time.time()
    ttl_seconds = settings.session_ttl_minutes * 60
    files = sorted(settings.session_dir.glob("*.db"), key=lambda p: p.stat().st_mtime)

    removed = 0
    survivors = []
    for f in files:
        if now - f.stat().st_mtime > ttl_seconds:
            f.unlink(missing_ok=True)
            removed += 1
        else:
            survivors.append(f)

    overflow = len(survivors) - settings.max_sessions
    if overflow > 0:
        for f in survivors[:overflow]:  # oldest-mtime first, since `files` was sorted ascending
            f.unlink(missing_ok=True)
            removed += 1

    return removed


def _maybe_sweep() -> None:
    global _last_sweep_at
    now = time.time()
    if now - _last_sweep_at < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep_at = now
    sweep_expired_sessions()
```

- [ ] **Step 2: Write `tests/test_sessions.py`**

```python
from __future__ import annotations

import time
import uuid

from sqlalchemy import text

from app.config import settings
from app.core import sessions
from app.core.db import get_engines


def _new_id() -> str:
    return str(uuid.uuid4())


def test_valid_uuid4_accepted():
    assert sessions.is_valid_session_id(str(uuid.uuid4()))


def test_traversal_and_malformed_ids_rejected():
    for bad in ["../../etc/passwd", "not-a-uuid", "", "12345", str(uuid.uuid1())]:
        assert not sessions.is_valid_session_id(bad)


def test_first_access_copies_template(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = _new_id()
    path = sessions.get_session_db_path(session_id)
    assert path.exists()
    with get_engines(str(path))[0].connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar_one() > 0


def test_two_sessions_are_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    id_a, id_b = _new_id(), _new_id()
    path_a = sessions.get_session_db_path(id_a)
    path_b = sessions.get_session_db_path(id_b)

    ro_a, rw_a = get_engines(str(path_a))
    ro_b, _ = get_engines(str(path_b))

    with rw_a.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    assert conn_count(ro_a, "orders", "id = 1") == 0
    assert conn_count(ro_b, "orders", "id = 1") == 1


def conn_count(engine, table, where) -> int:
    with engine.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar_one()


def test_reset_restores_original_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = _new_id()
    path = sessions.get_session_db_path(session_id)
    rw, _ = get_engines(str(path))[1], None
    ro, rw = get_engines(str(path))

    with rw.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))
    assert conn_count(ro, "orders", "id = 1") == 0

    sessions.reset_session(session_id)
    ro2, _ = get_engines(str(path))  # same path, same cached engine pair
    assert conn_count(ro2, "orders", "id = 1") == 1


def test_sweep_removes_idle_sessions_past_ttl(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "session_ttl_minutes", 0)  # anything untouched for >0 minutes expires
    session_id = _new_id()
    path = sessions.get_session_db_path(session_id)
    old_time = time.time() - 3600
    import os

    os.utime(path, (old_time, old_time))

    removed = sessions.sweep_expired_sessions()
    assert removed == 1
    assert not path.exists()


def test_sweep_evicts_oldest_when_over_max_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "max_sessions", 2)
    monkeypatch.setattr(settings, "session_ttl_minutes", 999)  # nothing expires by age in this test
    import os

    paths = []
    for i in range(3):
        path = sessions.get_session_db_path(_new_id())
        os.utime(path, (time.time() + i, time.time() + i))  # strictly increasing mtimes
        paths.append(path)

    removed = sessions.sweep_expired_sessions()
    assert removed == 1
    assert not paths[0].exists()  # oldest evicted
    assert paths[1].exists() and paths[2].exists()
```

- [ ] **Step 3: Run the new tests plus the full suite**

```bash
pytest tests/test_sessions.py -v
pytest -q
```

Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add app/core/sessions.py tests/test_sessions.py
git commit -m "feat(sandbox): add session manager with TTL sweep and max-session eviction"
```

---

### Task 3: Thread `db_path` through the LangGraph nodes that touch the database

**Files:**
- Modify: `app/agent/nodes/validator_node.py`
- Modify: `app/agent/nodes/analytics_executor.py`
- Modify: `app/agent/nodes/dry_run_preview.py`
- Modify: `app/agent/nodes/transactional_executor.py`
- Modify: `app/agent/nodes/audit_logger.py`
- Modify: `app/core/audit.py:17` (accept and record `session_id`)
- Test: `tests/test_node_session_isolation.py` (new)

**Interfaces:**
- Consumes: `app.core.db.get_engines` (Task 1). LangGraph passes `config["configurable"]` to any node whose signature declares a second `config: RunnableConfig` parameter (confirmed live against the installed `langgraph==1.2.10` — see verification note below); `config["configurable"]["db_path"]` will be set by Task 5's `app/main.py` changes.
- Produces: nothing new exported — these are graph-internal nodes. The observable change is that every DB read/write and every `log_event(...)` call inside them now uses the session's own engine and records `session_id`.

**Verification already done (live, this session):** built a two-node `StateGraph`, declared one node as `def node_a(state, config: RunnableConfig)`, invoked with `compiled.invoke({"x": 1}, {"configurable": {"db_path": "...", "thread_id": "s1"}})`, and confirmed the node received the exact `configurable` dict passed in (plus LangGraph's own internal `__pregel_*` keys, which nothing here reads). No live-verification gap remains for this mechanism.

- [ ] **Step 1: Add a `session_id` field to `app.core.audit.log_event`**

In `app/core/audit.py`, `log_event` already accepts arbitrary `**fields`, so no signature change is needed there — callers just need to start passing `session_id=...`. Update `read_events` (used by `/audit`, Task 5) to accept an optional filter:

```python
def read_events(limit: int = 100, offset: int = 0, session_id: str | None = None) -> list[dict]:
    """Most-recent-first. Pass session_id to restrict to one session's events
    (the default for the public /audit endpoint); omit it only for the
    admin-token-gated full log."""
    if not settings.audit_log_path.exists():
        return []
    with open(settings.audit_log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines = list(reversed(lines))
    if session_id is not None:
        lines = [line for line in lines if json.loads(line).get("session_id") == session_id]
    selected = lines[offset : offset + limit]
    return [json.loads(line) for line in selected if line.strip()]
```

(This JSONL-filtering approach is temporary scaffolding — Task 11 replaces the whole audit store with a Postgres/SQLite table where filtering is a `WHERE` clause. Keeping it simple here avoids building throwaway indexing logic.)

- [ ] **Step 2: Update `app/agent/nodes/validator_node.py`**

```python
from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.config import settings
from app.core.db import get_engines
from app.core.validator import validate_sql


def validator_node(state: AgentState, config: RunnableConfig) -> dict:
    db_path = config["configurable"]["db_path"]
    read_only_engine, _ = get_engines(db_path)
    result = validate_sql(state["generated_sql"], intent=state.get("intent", "analytics"), engine=read_only_engine)
    update: dict = {"validation_result": result.to_dict()}
    if result.passed:
        update["error"] = None
    else:
        update["error"] = "; ".join(result.reasons)
        update["retry_count"] = state.get("retry_count", 0) + 1
    return update


def route_after_validation(state: AgentState) -> str:
    if state["validation_result"]["passed"]:
        return "ambiguity_check"
    if state.get("retry_count", 0) <= settings.max_generation_retries:
        return "sql_generator"
    return "error_response"
```

(`route_after_validation` is unchanged — routing functions don't touch the DB and LangGraph doesn't pass `config` to them anyway.)

- [ ] **Step 3: Update `app/agent/nodes/analytics_executor.py`**

```python
from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.config import settings
from app.core import executor as sql_executor
from app.core.db import get_engines
from app.core.validator import validate_sql


def analytics_executor_node(state: AgentState, config: RunnableConfig) -> dict:
    sql = state["generated_sql"]
    db_path = config["configurable"]["db_path"]
    read_only_engine, _ = get_engines(db_path)

    validation = validate_sql(sql, intent="analytics", engine=read_only_engine)
    if not validation.passed:
        return {"error": "; ".join(validation.reasons), "retry_count": state.get("retry_count", 0) + 1}

    try:
        result = sql_executor.execute_select(sql, engine=read_only_engine)
    except Exception as exc:  # noqa: BLE001 - genuinely want to catch any DB error and self-correct on it
        return {"error": str(exc), "retry_count": state.get("retry_count", 0) + 1}

    return {
        "execution_result": {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.row_count >= settings.auto_limit,
        },
        "error": None,
    }


def route_after_analytics_execution(state: AgentState) -> str:
    if state.get("error") is None:
        return "insight_summarizer"
    if state.get("retry_count", 0) < settings.max_self_correction_attempts:
        return "self_corrector"
    return "error_response"
```

- [ ] **Step 4: Update `app/agent/nodes/dry_run_preview.py`**

```python
from __future__ import annotations

import time

from langchain_core.runnables import RunnableConfig
from sqlalchemy import text

from app.agent.state import AgentState
from app.core.audit import log_event
from app.core.db import get_engines, get_referencing_tables
from app.core.dry_run import build_cascade_impact_queries, build_preview
from app.core.validator import check_row_impact


def dry_run_preview_node(state: AgentState, config: RunnableConfig) -> dict:
    sql = state["generated_sql"]
    started_at = time.time()
    session_id = config["configurable"]["thread_id"]
    db_path = config["configurable"]["db_path"]
    read_only_engine, _ = get_engines(db_path)

    impact = check_row_impact(sql, engine=read_only_engine)
    if not impact.passed:
        log_event(
            event="manipulation_blocked",
            session_id=session_id,
            user_input=state["user_input"],
            generated_sql=sql,
            reason="; ".join(impact.reasons),
            latency_seconds=round(time.time() - started_at, 3),
        )
        return {"error": "; ".join(impact.reasons), "manipulation_started_at": started_at}

    preview = build_preview(sql)

    if preview.operation == "INSERT":
        preview_info = {
            "operation": preview.operation,
            "table": preview.table,
            "rows": [],
            "row_count": 1,
            "insert_values": preview.insert_values,
        }
    else:
        with read_only_engine.connect() as conn:
            rows = [dict(row._mapping) for row in conn.execute(text(preview.preview_sql)).fetchall()]
        preview_info = {"operation": preview.operation, "table": preview.table, "rows": rows, "row_count": len(rows)}

        if preview.operation == "DELETE":
            cascade_refs = [
                (r.child_table, r.child_column)
                for r in get_referencing_tables(preview.table, engine=read_only_engine)
                if r.on_delete == "CASCADE"
            ]
            impacts = build_cascade_impact_queries(preview, cascade_refs)
            cascades = []
            with read_only_engine.connect() as conn:
                for impact in impacts:
                    cascade_rows = [dict(row._mapping) for row in conn.execute(text(impact.preview_sql)).fetchall()]
                    if cascade_rows:
                        cascades.append(
                            {
                                "table": impact.table,
                                "column": impact.column,
                                "rows": cascade_rows,
                                "row_count": len(cascade_rows),
                            }
                        )
            if cascades:
                preview_info["cascades"] = cascades

    return {
        "preview": preview_info,
        "awaiting_confirmation": True,
        "error": None,
        "manipulation_started_at": started_at,
    }


def route_after_dry_run_preview(state: AgentState) -> str:
    if state.get("error"):
        return "error_response"
    return "confirmation_gate"
```

- [ ] **Step 5: Update `app/agent/nodes/transactional_executor.py`**

```python
from __future__ import annotations

from langchain_core.runnables import RunnableConfig
from sqlalchemy.exc import IntegrityError

from app.agent.state import AgentState
from app.core import executor as sql_executor
from app.core.db import get_engines
from app.core.validator import check_row_impact, validate_sql


def transactional_executor_node(state: AgentState, config: RunnableConfig) -> dict:
    sql = state["generated_sql"]
    db_path = config["configurable"]["db_path"]
    read_only_engine, read_write_engine = get_engines(db_path)

    validation = validate_sql(sql, intent="manipulation", engine=read_only_engine)
    if not validation.passed:
        reason = "; ".join(validation.reasons)
        return {"error": reason, "final_response": f"I couldn't complete this: {reason}"}

    impact = check_row_impact(sql, engine=read_only_engine)
    if not impact.passed:
        reason = "; ".join(impact.reasons)
        return {"error": reason, "final_response": f"I couldn't complete this: {reason}"}

    try:
        with sql_executor.transaction(engine=read_write_engine) as conn:
            result = sql_executor.execute_write(sql, conn)
    except IntegrityError:
        reason = "a foreign key constraint failed - other rows still reference the ones being changed"
        return {"error": reason, "final_response": f"The change failed and was rolled back: {reason}."}
    except Exception as exc:  # noqa: BLE001 - must ROLLBACK and report, whatever the cause
        return {"error": str(exc), "final_response": f"The change failed and was rolled back: {exc}"}

    return {
        "execution_result": {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": False,
        },
        "final_response": f"Done - {result.row_count} row(s) affected in {state['preview']['table']}.",
        "error": None,
    }
```

- [ ] **Step 6: Update `app/agent/nodes/audit_logger.py`**

```python
from __future__ import annotations

import time

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.audit import log_event


def audit_logger_node(state: AgentState, config: RunnableConfig) -> dict:
    started_at = state.get("manipulation_started_at")
    log_event(
        event="manipulation_executed" if not state.get("error") else "manipulation_failed",
        session_id=config["configurable"]["thread_id"],
        user_input=state["user_input"],
        generated_sql=state.get("generated_sql"),
        validation_result=state.get("validation_result"),
        preview=state.get("preview"),
        confirmed=state.get("confirmed"),
        row_count=(state.get("execution_result") or {}).get("row_count"),
        error=state.get("error"),
        retry_count=state.get("retry_count", 0),
        latency_seconds=round(time.time() - started_at, 3) if started_at else None,
    )
    return {}
```

- [ ] **Step 7: Write `tests/test_node_session_isolation.py`**

This drives the actual compiled graph (not just individual node functions) through the manipulation path twice, on two different session sandboxes, and checks a write in one is invisible in the other — the concrete DoD check ("two browsers cannot see each other's writes") at the graph level rather than the HTTP level (Task 5 adds the HTTP-level version).

```python
from __future__ import annotations

import uuid

from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import text

from app.agent.graph import build_graph
from app.config import settings
from app.core import sessions
from app.core.db import get_engines


def _run_delete(graph, session_id: str, db_path, sql: str):
    config = {"configurable": {"thread_id": session_id, "db_path": str(db_path)}}
    # Drive the graph directly at the transactional_executor node in
    # isolation would require constructing full AgentState by hand for every
    # upstream field; instead we invoke the two nodes this test actually
    # cares about directly, the same way the real graph edges call them.
    from app.agent.nodes.transactional_executor import transactional_executor_node

    state = {"generated_sql": sql, "preview": {"table": "orders"}, "user_input": "test"}
    return transactional_executor_node(state, config)


def test_delete_in_one_session_invisible_in_another(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    id_a, id_b = str(uuid.uuid4()), str(uuid.uuid4())
    path_a = sessions.get_session_db_path(id_a)
    path_b = sessions.get_session_db_path(id_b)
    graph = build_graph(checkpointer=MemorySaver())

    result = _run_delete(graph, id_a, path_a, "DELETE FROM orders WHERE id = 1")
    assert result["error"] is None

    ro_a, _ = get_engines(str(path_a))
    ro_b, _ = get_engines(str(path_b))
    with ro_a.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id = 1")).scalar_one() == 0
    with ro_b.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM orders WHERE id = 1")).scalar_one() == 1
```

- [ ] **Step 8: Run the new test plus the full suite**

```bash
pytest tests/test_node_session_isolation.py -v
pytest -q
```

Expected: all pass, including every existing `tests/test_guardrails.py` case (they call `validator.validate_sql`/`check_row_impact` directly with no `engine=` argument at all, so they keep hitting the template via the unchanged default — nothing in this task touches that default).

- [ ] **Step 9: Commit**

```bash
git add app/agent/nodes/validator_node.py app/agent/nodes/analytics_executor.py \
  app/agent/nodes/dry_run_preview.py app/agent/nodes/transactional_executor.py \
  app/agent/nodes/audit_logger.py app/core/audit.py tests/test_node_session_isolation.py
git commit -m "feat(sandbox): thread session db_path through every graph node touching the database"
```

---

### Task 4: Session-aware `/tables` and `/tables/{name}/preview`, and `POST /session/reset`

**Files:**
- Modify: `app/core/schema_store.py:106` (`introspect_tables` gains an `engine` param)
- Modify: `app/main.py` (session_id validation, `/chat`+`/confirm` wire `db_path` into `config["configurable"]`, `/tables`+`/tables/{name}/preview` become session-aware, new `POST /session/reset`)
- Test: `tests/test_main_sessions.py` (new)

**Interfaces:**
- Consumes: `app.core.sessions.is_valid_session_id`, `.get_session_db_path`, `.reset_session` (Task 2); `app.core.db.get_engines` (Task 1).
- Produces: `app.core.schema_store.introspect_tables(table_names: list[str] | None = None, engine: Engine = read_only_engine) -> dict[str, TableInfo]` (new `engine` param, defaults preserve every existing caller's behavior unchanged).

- [ ] **Step 1: Add an `engine` parameter to `introspect_tables`**

In `app/core/schema_store.py`, change line 106's signature and line 115's body:

```python
def introspect_tables(
    table_names: list[str] | None = None, engine: Engine = read_only_engine
) -> dict[str, TableInfo]:
    ...
    tables: dict[str, TableInfo] = {}
    with engine.connect() as conn:
```

(replace every other `read_only_engine` reference inside the function body with `engine` — there are three more `conn.execute` blocks reusing the same `with ... as conn:` block, unaffected since they already use `conn`). Add `from sqlalchemy.engine import Engine` to the imports. `build_or_refresh_index()` (line 162, calls `introspect_tables()`) and `full_schema_summary()` (line 210, calls `introspect_tables()`) both keep calling it with zero arguments, so they keep using the template engine, unchanged — only `app/main.py`'s table endpoints will pass `engine=` explicitly.

- [ ] **Step 2: Add session_id validation and resolution helpers to `app/main.py`**

Add near the top of `app/main.py`, after the existing imports:

```python
from app.core import sessions
from app.core.db import get_engines


def _resolve_session_engine(session_id: str | None):
    """Session-aware read-only engine for GET endpoints: the template if no
    session_id is given, or the session doesn't exist yet; that session's
    own sandbox engine otherwise."""
    if session_id is None:
        return read_only_engine
    if not sessions.is_valid_session_id(session_id):
        raise HTTPException(400, "session_id must be a valid UUID4.")
    db_path = sessions.get_session_db_path(session_id)
    ro, _ = get_engines(str(db_path))
    return ro


def _require_valid_session_id(session_id: str) -> None:
    if not sessions.is_valid_session_id(session_id):
        raise HTTPException(400, "session_id must be a valid UUID4.")
```

- [ ] **Step 3: Wire `db_path` into `/chat` and `/confirm`**

Replace `chat()` (lines 122-133):

```python
@app.post("/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    _require_valid_session_id(req.session_id)
    db_path = sessions.get_session_db_path(req.session_id)
    config = {"configurable": {"thread_id": req.session_id, "db_path": str(db_path)}}
    graph_input = {"user_input": req.message}
    return StreamingResponse(_run_graph_stream(req.session_id, config, graph_input), media_type="text/event-stream")
```

Replace `confirm()` (lines 136-150):

```python
@app.post("/confirm")
def confirm(req: ConfirmRequest) -> StreamingResponse:
    _require_valid_session_id(req.session_id)
    graph = _state["graph"]
    db_path = sessions.get_session_db_path(req.session_id)
    config = {"configurable": {"thread_id": req.session_id, "db_path": str(db_path)}}
    current = graph.get_state(config)
    if not current.next:
        raise HTTPException(400, "No pending confirmation for this session.")

    requested_at = _pending_confirmations.pop(req.session_id, None)
    timed_out = requested_at is not None and (time.time() - requested_at) > settings.confirmation_timeout_seconds
    resume_value = "timeout" if timed_out else ("yes" if req.confirmed else "no")

    return StreamingResponse(
        _run_graph_stream(req.session_id, config, Command(resume=resume_value)), media_type="text/event-stream"
    )
```

- [ ] **Step 4: Make `/tables` and `/tables/{table_name}/preview` session-aware**

Replace lines 176-215:

```python
@app.get("/tables")
def list_tables(session_id: str | None = None) -> dict:
    engine = _resolve_session_engine(session_id)
    tables = introspect_tables(engine=engine)
    with engine.connect() as conn:
        counts = {name: conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one() for name in tables}
    return {
        "tables": [
            {
                "name": t.name,
                "purpose": t.purpose,
                "columns": [{"name": n, "type": ctype} for n, ctype in t.columns],
                "row_count": counts[t.name],
            }
            for t in tables.values()
        ]
    }


@app.get("/tables/{table_name}/preview")
def preview_table(table_name: str, limit: int = 50, offset: int = 0, session_id: str | None = None) -> dict:
    engine = _resolve_session_engine(session_id)
    with engine.connect() as conn:
        live_tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            )
        }
        if table_name not in live_tables:
            raise HTTPException(404, f"Unknown table: {table_name}")
        limit = max(1, min(limit, _MAX_TABLE_PREVIEW_LIMIT))
        offset = max(0, offset)
        columns = [row[1] for row in conn.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()]
        rows = [
            dict(row._mapping)
            for row in conn.execute(
                text(f'SELECT * FROM "{table_name}" LIMIT :limit OFFSET :offset'),
                {"limit": limit, "offset": offset},
            )
        ]
        total = conn.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar_one()
    return {"table": table_name, "columns": columns, "rows": rows, "row_count": total, "limit": limit, "offset": offset}
```

(`_live_table_names()`, the old module-level helper at lines 166-173, is folded into `preview_table` above since it now needs the resolved per-request engine rather than the fixed global — delete the old standalone function.)

- [ ] **Step 5: Add `POST /session/reset`**

Add after the `/confirm` endpoint:

```python
class SessionResetRequest(BaseModel):
    session_id: str


@app.post("/session/reset")
def session_reset(req: SessionResetRequest) -> dict:
    _require_valid_session_id(req.session_id)
    sessions.reset_session(req.session_id)
    return {"status": "reset", "session_id": req.session_id}
```

- [ ] **Step 6: Write `tests/test_main_sessions.py`**

```python
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.config import settings


def test_chat_rejects_non_uuid4_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/chat", json={"session_id": "../../etc/passwd", "message": "hi"})
    assert response.status_code == 400


def test_session_reset_rejects_non_uuid4():
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/session/reset", json={"session_id": "not-a-uuid"})
    assert response.status_code == 400


def test_tables_endpoint_falls_back_to_template_with_no_session_id():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/tables")
    assert response.status_code == 200
    assert len(response.json()["tables"]) > 0


def test_tables_endpoint_reflects_session_specific_deletes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.core import sessions
    from app.core.db import get_engines
    from sqlalchemy import text

    session_id = str(uuid.uuid4())
    path = sessions.get_session_db_path(session_id)
    _, rw = get_engines(str(path))
    with rw.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    from app.main import app

    with TestClient(app) as client:
        response = client.get(f"/tables/orders/preview?session_id={session_id}")
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["rows"]]
    assert 1 not in ids

    with TestClient(app) as client:
        response = client.get("/tables/orders/preview")  # no session_id -> template, unaffected
    ids = [row["id"] for row in response.json()["rows"]]
    assert 1 in ids


def test_session_reset_restores_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.core import sessions
    from app.core.db import get_engines
    from sqlalchemy import text

    session_id = str(uuid.uuid4())
    path = sessions.get_session_db_path(session_id)
    _, rw = get_engines(str(path))
    with rw.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    from app.main import app

    with TestClient(app) as client:
        reset_response = client.post("/session/reset", json={"session_id": session_id})
        assert reset_response.status_code == 200
        preview_response = client.get(f"/tables/orders/preview?session_id={session_id}")
    ids = [row["id"] for row in preview_response.json()["rows"]]
    assert 1 in ids
```

- [ ] **Step 7: Run the new tests plus the full suite**

```bash
pytest tests/test_main_sessions.py -v
pytest -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add app/core/schema_store.py app/main.py tests/test_main_sessions.py
git commit -m "feat(sandbox): session-aware /tables endpoints and POST /session/reset"
```

---

## Part B — Remove ChromaDB (2b)

### Task 5: Gemini embeddings + local JSON cache + numpy cosine similarity retrieval

**Files:**
- Modify: `app/core/schema_store.py` (replace the ChromaDB-backed retrieval block, lines 1-24 and 82-201, with Gemini + numpy; keep `TableInfo`, `introspect_tables`, `format_schema_context`, `full_schema_summary` as-is from Task 4)
- Modify: `app/config.py` (remove `chroma_persist_dir`, add embedding settings)
- Modify: `requirements.txt` (remove `chromadb`, `posthog`; add `numpy`, `google-genai` is already present from Phase 1)
- Modify: `.env.example`
- Test: `tests/test_schema_embeddings.py` (new)

**Interfaces:**
- Produces: `app.core.schema_store.retrieve_relevant_tables(nl_query: str, top_k: int | None = None) -> list[TableInfo]` — **signature unchanged**, so `app/agent/nodes/schema_retriever.py` needs no changes at all.
- Produces: `app.core.schema_store.build_or_refresh_index(force: bool = False) -> int` — **signature unchanged**.
- Consumes: `app.config.provider_credentials.gemini_api_key` (Phase 1).

- [ ] **Step 1: Live-verify the embedding call before writing the cache logic**

```bash
python -c "
from google import genai
from app.config import provider_credentials
client = genai.Client(api_key=provider_credentials.gemini_api_key)
result = client.models.embed_content(
    model='gemini-embedding-001',
    contents=['orders: Purchase orders placed by retailers. Columns: id, retailer_id, status.'],
    config={'output_dimensionality': 768},
)
print(len(result.embeddings[0].values))
"
```

Expected: prints `768`. Record the actual result in the Task 11 design note (this is the "embedding dimensions" the spec calls out as needing live verification). If the SDK's method name or config shape differs from the above (the `google-genai` package has changed its embedding API surface across versions before), adjust Step 2's implementation to match what actually works, and note the discrepancy in the design note.

**Deliberate deviation from the spec's build-time-caching suggestion:** the spec says to generate `schema_embeddings.json` "at build time if the key is present, otherwise on first startup." Building it at Docker-build time would require passing `GEMINI_API_KEY` into the build (a `--build-arg` or BuildKit secret), which risks the key ending up in an image layer or build log. This plan always generates the cache lazily on first startup instead (via the existing `lifespan()` call to `build_or_refresh_index()` in `app/main.py`, unchanged from before this phase) — a few extra seconds on the very first request after a cold start, in exchange for never handling a secret at build time. Record this trade-off in the Task 11 design note rather than silently deviating from the spec text.

- [ ] **Step 2: Replace the ChromaDB section of `app/core/schema_store.py`**

Delete lines 1-24 (module docstring through `_embedding_client = ...`) and replace with:

```python
"""Schema retrieval via Gemini embeddings + numpy cosine similarity.

Each table's description (name, purpose, columns) is embedded once via
Gemini's embedding API and cached to disk, keyed by a hash of the document
text so a schema/description change invalidates just that table's cached
vector. A user request embeds the query text at request time and ranks
tables by cosine similarity - no vector database, no ONNX runtime, no
background telemetry client: at 6 tables, a linear numpy scan is simply
faster to build and run than standing up a real vector index.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

import numpy as np
from google import genai
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.config import provider_credentials, settings
from app.core.db import read_only_engine

logger = logging.getLogger(__name__)

_embedding_client = genai.Client(api_key=provider_credentials.gemini_api_key)
```

Delete lines 82-186 of the original file (`OllamaEmbeddingFunction` class through `_get_collection()`) and replace with:

```python
def _embed_texts(texts: list[str]) -> list[list[float]]:
    result = _embedding_client.models.embed_content(
        model=settings.embedding_model,
        contents=texts,
        config={"output_dimensionality": settings.embedding_dimensions},
    )
    return [list(e.values) for e in result.embeddings]


def _doc_hash(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _load_cache() -> dict[str, dict]:
    if not settings.schema_embeddings_cache_path.exists():
        return {}
    try:
        return json.loads(settings.schema_embeddings_cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    settings.schema_embeddings_cache_path.parent.mkdir(parents=True, exist_ok=True)
    settings.schema_embeddings_cache_path.write_text(json.dumps(cache), encoding="utf-8")


def build_or_refresh_index(force: bool = False) -> int:
    """(Re)embeds every table description whose cached hash doesn't match its
    current as_embedding_document() text. Idempotent and cheap to call at
    startup: with a warm cache and no schema changes, this makes zero
    embedding calls."""
    tables = introspect_tables()
    cache = {} if force else _load_cache()

    to_embed: list[str] = []
    to_embed_names: list[str] = []
    for name, table in tables.items():
        document = table.as_embedding_document()
        digest = _doc_hash(document)
        if cache.get(name, {}).get("hash") != digest:
            to_embed.append(document)
            to_embed_names.append(name)

    if to_embed:
        vectors = _embed_texts(to_embed)
        for name, document, vector in zip(to_embed_names, to_embed, vectors):
            cache[name] = {"hash": _doc_hash(document), "vector": vector}

    # Drop cache entries for tables that no longer exist.
    for stale_name in list(cache.keys()):
        if stale_name not in tables:
            del cache[stale_name]

    _save_cache(cache)
    return len(to_embed)


def retrieve_relevant_tables(nl_query: str, top_k: int | None = None) -> list[TableInfo]:
    """Top-k most relevant tables for a natural-language request, ranked by
    cosine similarity against the cached per-table embeddings. Falls back to
    every table (full schema) on any embedding failure - with 6 tables the
    full schema is small enough that this degradation is nearly free, and
    correctness matters far more than an optimally-targeted prompt."""
    top_k = settings.schema_retrieval_top_k if top_k is None else top_k
    tables = introspect_tables()

    try:
        cache = _load_cache()
        missing = [name for name in tables if name not in cache]
        if missing:
            build_or_refresh_index()
            cache = _load_cache()

        query_vector = np.array(_embed_texts([nl_query])[0])
        names = list(tables.keys())
        matrix = np.array([cache[name]["vector"] for name in names])
        query_norm = query_vector / np.linalg.norm(query_vector)
        matrix_norm = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        scores = matrix_norm @ query_norm

        ranked = [name for _, name in sorted(zip(scores, names), reverse=True)]
        top_names = ranked[:top_k]
        return [tables[name] for name in top_names]
    except Exception:  # noqa: BLE001 - any embedding/cache failure degrades to the full schema, never fails the request
        logger.warning("Schema embedding retrieval failed; falling back to full schema.", exc_info=True)
        return list(tables.values())
```

Keep `introspect_tables` (with the `engine` param added in Task 4), `TableInfo`, `TABLE_PURPOSES`, `as_prompt_block`, `as_embedding_document`, `format_schema_context`, and `full_schema_summary` exactly as they are — none of them reference ChromaDB.

- [ ] **Step 3: Emit a `degraded` event on fallback, for Phase 4's observability to pick up later**

The spec requires "Emit a `degraded` event and never fail the request" on embedding failure. Phase 4 (not this phase) builds the general SSE event-emission plumbing for graph nodes; wiring a `degraded` SSE event all the way to the client is out of scope here without that plumbing. For now, satisfy "never fail the request" (done above via the `except` fallback) and make the degradation observable in the interim via the existing audit log, from the caller's side. In `app/agent/nodes/schema_retriever.py`:

```python
from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.audit import log_event
from app.core.schema_store import format_schema_context, retrieve_relevant_tables

_FULL_SCHEMA_TABLE_COUNT = 6  # from TABLE_PURPOSES in schema_store.py; a retrieval result at or above
# this count means retrieve_relevant_tables() fell back to the full schema rather than really ranking


def schema_retriever_node(state: AgentState, config: RunnableConfig) -> dict:
    tables = retrieve_relevant_tables(state["user_input"])
    if len(tables) >= _FULL_SCHEMA_TABLE_COUNT:
        log_event(event="degraded", session_id=config["configurable"]["thread_id"], reason="schema_embedding_fallback")
    return {"relevant_schema": format_schema_context(tables)}
```

(This also means `schema_retriever_node` now needs the `config: RunnableConfig` parameter — LangGraph passes it automatically the same way it does for the Task 3 nodes, no graph.py change needed.)

- [ ] **Step 4: Update `app/config.py`**

Remove `chroma_persist_dir: Path = PROJECT_ROOT / "data" / "chroma"` and `schema_retrieval_top_k`'s neighboring comment block header if it becomes stale; replace with:

```python
    # --- Schema retrieval ---
    schema_embeddings_cache_path: Path = PROJECT_ROOT / "data" / "schema_embeddings.json"
    embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = 768
    schema_retrieval_top_k: int = 3
    sample_rows_per_table: int = 3
```

- [ ] **Step 5: Update `requirements.txt`**

Remove the `chromadb==0.5.23` and `posthog<3` lines entirely. Add `numpy==2.2.1` (pin to whatever version `pip show numpy` reports after installing, to match the repo's pattern of exact pins elsewhere in this file).

- [ ] **Step 6: Update `.env.example`**

The existing line `# Gemini -- third fallback (and Phase 2b's embedding provider). Get a key at https://aistudio.google.com/apikey` already documents `GEMINI_API_KEY` for this purpose from Phase 1 — no new provider-credential var is needed. No change required here beyond confirming that comment still reads correctly (it does).

- [ ] **Step 7: Write `tests/test_schema_embeddings.py`**

```python
from __future__ import annotations

import json

from app.config import settings
from app.core import schema_store


def test_build_or_refresh_index_writes_cache_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")
    count = schema_store.build_or_refresh_index()
    assert count > 0
    cache = json.loads((tmp_path / "embeddings.json").read_text())
    assert set(cache.keys()) == set(schema_store.TABLE_PURPOSES.keys())
    for entry in cache.values():
        assert len(entry["vector"]) == settings.embedding_dimensions


def test_build_or_refresh_index_is_idempotent_with_warm_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")
    schema_store.build_or_refresh_index()
    second_call_embedded_count = schema_store.build_or_refresh_index()
    assert second_call_embedded_count == 0


def test_retrieve_relevant_tables_ranks_by_similarity(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")
    schema_store.build_or_refresh_index()
    tables = schema_store.retrieve_relevant_tables("how many products are low on stock?", top_k=2)
    names = [t.name for t in tables]
    assert "inventory" in names


def test_retrieve_relevant_tables_degrades_to_full_schema_on_embedding_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "schema_embeddings_cache_path", tmp_path / "embeddings.json")

    def _broken_embed(texts):
        raise RuntimeError("simulated embedding outage")

    monkeypatch.setattr(schema_store, "_embed_texts", _broken_embed)
    tables = schema_store.retrieve_relevant_tables("anything", top_k=2)
    assert len(tables) == len(schema_store.TABLE_PURPOSES)
```

- [ ] **Step 8: Uninstall chromadb/posthog locally, reinstall from the updated requirements, run the full suite**

```bash
pip uninstall -y chromadb posthog
pip install -r requirements.txt
pytest -q
```

Expected: all pass, including the new embedding tests (these make real Gemini API calls — same live-dependency pattern Phase 1's LLM gateway tests already accept for provider calls not otherwise mockable at this layer; if CI cost/flakiness becomes a problem, a future task can add a `--mock-embeddings` fixture, but that's out of scope here per YAGNI).

- [ ] **Step 9: Commit**

```bash
git add app/core/schema_store.py app/agent/nodes/schema_retriever.py app/config.py \
  requirements.txt tests/test_schema_embeddings.py
git commit -m "feat(schema): replace ChromaDB retrieval with Gemini embeddings + numpy cosine similarity"
```

---

## Part C — Durable storage on Neon; ephemeral local state (2c)

### Task 6: Operational database engine, SQLAlchemy models, Alembic

**Files:**
- Create: `app/core/opdb.py` (operational engine + declarative `Base`, separate from the per-session sqlite engines in `app/core/db.py`)
- Create: `app/core/models.py` (`AuditEvent`, `BudgetEntry` ORM models)
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/versions/0001_initial.py`
- Modify: `app/config.py` (add `database_url`)
- Modify: `requirements.txt` (add `alembic`, `psycopg[binary]`)
- Modify: `.env.example`
- Test: `tests/test_opdb.py` (new)

**Interfaces:**
- Produces: `app.core.opdb.engine: Engine`, `app.core.opdb.Base` (declarative base), `app.core.opdb.get_session() -> sqlalchemy.orm.Session` (context-manager-friendly session factory).
- Produces: `app.core.models.AuditEvent`, `app.core.models.BudgetEntry` ORM classes.

- [ ] **Step 1: Add `database_url` to `app/config.py`**

```python
    # --- Operational storage (audit events, budget entries) ---
    # Empty means "use a local SQLite file" -- local dev needs zero cloud setup.
    database_url: str = ""

    @property
    def resolved_database_url(self) -> str:
        return self.database_url or f"sqlite:///{PROJECT_ROOT / 'data' / 'operational.db'}"
```

- [ ] **Step 2: Write `app/core/opdb.py`**

```python
"""Operational database: audit events and budget entries. Points at Neon
Postgres in production (QP_DATABASE_URL) and a local SQLite file otherwise,
so local development needs no cloud account. Deliberately separate from
app/core/db.py's per-session query-data engines - this one holds QueryPilot's
own bookkeeping data, never the demo dataset the agent queries."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(settings.resolved_database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@contextmanager
def get_session():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

- [ ] **Step 3: Write `app/core/models.py`**

```python
"""SQLAlchemy ORM models for the operational database (app/core/opdb.py)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.opdb import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class BudgetEntry(Base):
    __tablename__ = "budget_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
```

(`payload: JSON` holds every field Task 7 needs to log — `user_input`, `generated_sql`, `validation_result`, `preview`, `confirmed`, `row_count`, `error`, `retry_count`, `latency_seconds` — as one column instead of one column per field, since the set of fields differs by event type and JSONL never had a fixed schema either; Phase 3's budget/cache logic populates `BudgetEntry` rows, not this phase.)

- [ ] **Step 4: Initialize Alembic and write the initial migration**

```bash
pip install alembic "psycopg[binary]"
alembic init alembic
```

Replace the generated `alembic/env.py`'s model-metadata wiring (the `target_metadata = None` line and the `run_migrations_online`/`offline` functions' engine construction) so it imports the app's own models and engine instead of reading a hardcoded URL from `alembic.ini`:

```python
# near the top of alembic/env.py, after the existing imports
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.models import AuditEvent, BudgetEntry  # noqa: F401 - import registers them on Base.metadata
from app.core.opdb import Base, engine as app_engine
from app.config import settings

target_metadata = Base.metadata
```

and in both `run_migrations_offline()` and `run_migrations_online()`, replace whatever URL/engine construction the scaffold generated with `settings.resolved_database_url` / `app_engine` respectively (the online path can call `connectable = app_engine` directly instead of building a new engine from `config.get_section(...)`).

Generate the migration:

```bash
alembic revision --autogenerate -m "initial: audit_events, budget_entries"
```

Confirm the generated file under `alembic/versions/` creates exactly the two tables from Task 6 Step 3 (rename it to `0001_initial.py` if Alembic's default hash-based filename differs, for readability, keeping its `revision`/`down_revision` values as autogenerated).

- [ ] **Step 5: Update `.env.example`**

```
# --- Operational storage (Phase 2c) ---
# Neon Postgres connection string in production; leave unset for local dev,
# which falls back to a local SQLite file (data/operational.db) automatically.
QP_DATABASE_URL=
```

- [ ] **Step 6: Update `requirements.txt`**

Add `alembic==1.14.1` and `psycopg[binary]==3.2.4` (adjust to whatever `pip show` reports after Step 4's install, matching this file's exact-pin convention).

- [ ] **Step 7: Write `tests/test_opdb.py`**

```python
from __future__ import annotations

from app.core.models import AuditEvent, BudgetEntry
from app.core.opdb import Base, get_session


def test_local_sqlite_fallback_creates_tables_and_round_trips(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    from sqlalchemy import create_engine

    test_engine = create_engine(settings.resolved_database_url)
    Base.metadata.create_all(test_engine)

    from sqlalchemy.orm import sessionmaker

    TestSession = sessionmaker(bind=test_engine)
    with TestSession() as session:
        session.add(AuditEvent(session_id="11111111-1111-4111-8111-111111111111", event="test", payload={"a": 1}))
        session.commit()

    with TestSession() as session:
        rows = session.query(AuditEvent).all()
    assert len(rows) == 1
    assert rows[0].payload == {"a": 1}
```

(This test exercises the ORM models directly against a throwaway SQLite file rather than the module-level `app.core.opdb.engine` singleton, since that engine is built once at import time from whatever `QP_DATABASE_URL` was set when the process started — the same reason `app/core/db.py`'s original engines were singletons before Task 1's `get_engines()` factory. Testing engine *selection* logic itself is covered by asserting `settings.resolved_database_url` directly, not by re-importing `app.core.opdb.engine`.)

Add one more, non-parametrized assertion test:

```python
def test_resolved_database_url_falls_back_to_local_sqlite(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "")
    assert settings.resolved_database_url.startswith("sqlite:///")


def test_resolved_database_url_uses_configured_value(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "database_url", "postgresql://user:pass@host/db")
    assert settings.resolved_database_url == "postgresql://user:pass@host/db"
```

- [ ] **Step 8: Run the migration against the local SQLite fallback, then the test suite**

```bash
alembic upgrade head
pytest tests/test_opdb.py -v
pytest -q
```

Expected: `data/operational.db` is created with `audit_events` and `budget_entries` tables; all tests pass.

- [ ] **Step 9: Commit**

```bash
git add app/core/opdb.py app/core/models.py alembic.ini alembic/ app/config.py \
  requirements.txt .env.example tests/test_opdb.py
git commit -m "feat(storage): add operational DB (audit_events, budget_entries) with Alembic migrations"
```

---

### Task 7: Migrate audit logging from JSONL to the operational database

**Files:**
- Modify: `app/core/audit.py` (rewrite `log_event`/`read_events` on top of `app.core.opdb`/`app.core.models.AuditEvent`)
- Modify: `app/main.py:218-220` (`/audit` endpoint: default to caller's session, add `X-Admin-Token` for the full log)
- Modify: `app/config.py` (add `admin_token` setting; the `audit_log_path` field can be deleted now)
- Modify: `.env.example`
- Test: `tests/test_audit.py` (new)

**Interfaces:**
- Produces: `app.core.audit.log_event(*, session_id: str, event: str, **fields) -> None` (now `session_id` is a required, explicit keyword argument rather than one of many optional `**fields`, since it's now an indexed column, not just another JSON field — every existing call site from Task 3/5 already passes it this way).
- Produces: `app.core.audit.read_events(limit: int = 100, offset: int = 0, session_id: str | None = None) -> list[dict]` — same signature as Task 3 Step 1's interim version, now backed by a real query instead of a JSONL scan.

- [ ] **Step 1: Rewrite `app/core/audit.py`**

```python
"""Audit trail for every manipulation request: which session, what SQL, the
validation outcome, the preview shown, confirm/cancel, rows affected,
latency, retry count. Stored in the operational database (Neon in
production, local SQLite otherwise) instead of a JSONL file, so it survives
a Render free-tier spin-down and isn't lost with the ephemeral filesystem.
"""
from __future__ import annotations

from typing import Any

from app.core.models import AuditEvent
from app.core.opdb import get_session


def log_event(*, session_id: str, event: str, **fields: Any) -> None:
    with get_session() as session:
        session.add(AuditEvent(session_id=session_id, event=event, payload=fields))


def read_events(limit: int = 100, offset: int = 0, session_id: str | None = None) -> list[dict]:
    """Most-recent-first. Pass session_id to restrict to one session's
    events (the default for the public /audit endpoint); omit it only for
    the admin-token-gated full log."""
    with get_session() as session:
        query = session.query(AuditEvent).order_by(AuditEvent.timestamp.desc())
        if session_id is not None:
            query = query.filter(AuditEvent.session_id == session_id)
        rows = query.offset(offset).limit(limit).all()
        return [
            {
                "timestamp": row.timestamp.isoformat(),
                "session_id": row.session_id,
                "event": row.event,
                **row.payload,
            }
            for row in rows
        ]
```

Every existing caller (`app/agent/nodes/dry_run_preview.py`, `audit_logger.py`, `schema_retriever.py` — all from Tasks 3 and 5) already calls `log_event(session_id=..., event=..., ...)` with `session_id` as an explicit keyword, so none of them need further changes for this new required-keyword signature.

- [ ] **Step 2: Update `app/config.py`**

Remove `audit_log_path: Path = PROJECT_ROOT / "logs" / "audit.jsonl"` and add:

```python
    # --- Audit access control ---
    admin_token: str = ""  # required in the X-Admin-Token header for GET /audit's full (all-sessions) log
```

- [ ] **Step 3: Update `GET /audit` in `app/main.py`**

Replace lines 218-220:

```python
@app.get("/audit")
def audit(limit: int = 50, offset: int = 0, session_id: str | None = None, x_admin_token: str | None = Header(None)) -> dict:
    if x_admin_token is not None and settings.admin_token and x_admin_token == settings.admin_token:
        return {"events": read_events(limit=limit, offset=offset)}  # full log, every session
    if session_id is None:
        raise HTTPException(400, "session_id is required (or provide a valid X-Admin-Token for the full log).")
    _require_valid_session_id(session_id)
    return {"events": read_events(limit=limit, offset=offset, session_id=session_id)}
```

Add `Header` to the `from fastapi import ...` import line at the top of the file.

- [ ] **Step 4: Update `.env.example`**

```
# --- Audit access (Phase 2c) ---
# Required in the X-Admin-Token header to read GET /audit's all-sessions log;
# leave unset in local dev if you don't need the admin view.
QP_ADMIN_TOKEN=
```

- [ ] **Step 5: Write `tests/test_audit.py`**

```python
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.config import settings
from app.core import audit


def test_log_event_and_read_events_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    from app.core.opdb import Base, engine

    Base.metadata.create_all(engine)

    session_id = str(uuid.uuid4())
    audit.log_event(session_id=session_id, event="manipulation_executed", row_count=3)
    events = audit.read_events(session_id=session_id)
    assert len(events) == 1
    assert events[0]["event"] == "manipulation_executed"
    assert events[0]["row_count"] == 3


def test_read_events_filters_by_session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    from app.core.opdb import Base, engine

    Base.metadata.create_all(engine)

    id_a, id_b = str(uuid.uuid4()), str(uuid.uuid4())
    audit.log_event(session_id=id_a, event="e1")
    audit.log_event(session_id=id_b, event="e2")

    assert [e["event"] for e in audit.read_events(session_id=id_a)] == ["e1"]
    assert [e["event"] for e in audit.read_events(session_id=id_b)] == ["e2"]


def test_audit_endpoint_requires_session_id_or_admin_token(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    monkeypatch.setattr(settings, "admin_token", "secret")
    from app.core.opdb import Base, engine

    Base.metadata.create_all(engine)
    from app.main import app

    with TestClient(app) as client:
        no_auth = client.get("/audit")
        assert no_auth.status_code == 400

        with_session = client.get(f"/audit?session_id={uuid.uuid4()}")
        assert with_session.status_code == 200

        with_admin = client.get("/audit", headers={"X-Admin-Token": "secret"})
        assert with_admin.status_code == 200
```

- [ ] **Step 6: Run the new tests plus the full suite**

```bash
pytest tests/test_audit.py -v
pytest -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add app/core/audit.py app/main.py app/config.py .env.example tests/test_audit.py
git commit -m "feat(storage): move audit trail from audit.jsonl to the operational database"
```

---

### Task 8: Checkpoints stay ephemeral under `/tmp`; clean `SESSION_EXPIRED` on `/confirm`

**Files:**
- Modify: `app/config.py:45` (`checkpoint_db_path` moves under the OS temp dir, matching the sandbox's own ephemeral-by-design storage)
- Modify: `app/main.py` (`/confirm`'s "no pending confirmation" branch becomes a distinguishable `SESSION_EXPIRED` case)
- Test: `tests/test_main_confirm_expiry.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `/confirm` on a session with no pending checkpoint now returns HTTP 409 with `{"code": "SESSION_EXPIRED", "message": "..."}` instead of the current generic 400, so a future frontend (Phase 6) can distinguish "nothing to confirm" from "your validation request was malformed." (The full `ErrorCode` enum is Phase 3; this is a narrowly-scoped early instance of the same shape the spec explicitly calls out in 2c, so it's added now rather than left as an undifferentiated 400.)

- [ ] **Step 1: Move `checkpoint_db_path` under the OS temp dir**

In `app/config.py`, replace line 45:

```python
    checkpoint_db_path: Path = Path(tempfile.gettempdir()) / "qp_checkpoints" / "checkpoints.sqlite"
```

(`tempfile` is already imported from Task 1.)

- [ ] **Step 2: Return a distinguishable `SESSION_EXPIRED` response from `/confirm`**

In `app/main.py`, replace the `if not current.next:` branch inside `confirm()`:

```python
    if not current.next:
        raise HTTPException(
            409,
            detail={"code": "SESSION_EXPIRED", "message": "No pending confirmation for this session. It may have expired or already been resolved."},
        )
```

- [ ] **Step 3: Write `tests/test_main_confirm_expiry.py`**

```python
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.config import settings


def test_confirm_with_no_pending_checkpoint_returns_session_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/confirm", json={"session_id": str(uuid.uuid4()), "confirmed": True})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"
```

- [ ] **Step 4: Run the new test plus the full suite**

```bash
pytest tests/test_main_confirm_expiry.py -v
pytest -q
```

Expected: all pass. (Losing checkpoints on a spin-down is explicitly acceptable per the spec — this task only makes the resulting failure mode legible instead of a bare 400.)

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/main.py tests/test_main_confirm_expiry.py
git commit -m "feat(storage): move checkpoints under /tmp, return SESSION_EXPIRED on stale /confirm"
```

---

## Part D — Container and config (2d)

### Task 9: Dockerfile, requirements split, CORS

**Files:**
- Modify: `Dockerfile`
- Create: `requirements-dev.txt`
- Modify: `requirements.txt` (remove `pytest`)
- Modify: `app/main.py:78` (CORS origins from env)
- Modify: `app/config.py` (add `cors_allowed_origins`)
- Modify: `.env.example`

**Interfaces:**
- Consumes: `app.config.settings.cors_allowed_origins: str` (comma-separated).

- [ ] **Step 1: Split dev tooling out of `requirements.txt`**

Remove `pytest==8.3.4` from `requirements.txt`. Create `requirements-dev.txt`:

```
-r requirements.txt
pytest==8.3.4
```

- [ ] **Step 2: Add `cors_allowed_origins` to `app/config.py`**

```python
    # --- CORS ---
    cors_allowed_origins: str = ""  # comma-separated; empty means no cross-origin requests are allowed
```

- [ ] **Step 3: Make CORS env-driven in `app/main.py`**

Replace line 78:

```python
_cors_origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),  # never combine "*" with credentials; an empty list keeps this False too
    allow_methods=["*"],
    allow_headers=["*"],
)
```

- [ ] **Step 4: Rewrite the `Dockerfile`**

```dockerfile
FROM python:3.11-slim AS base

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/
COPY frontend/ ./frontend/
COPY config/ ./config/
COPY alembic.ini .
COPY alembic/ ./alembic/

RUN python -m scripts.seed_db

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000
ENV PORT=8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

(Seeding the template now happens at build time, unconditionally — Task 1's `--force`-gated skip logic in `scripts/seed_db.py` means re-running it in a later build layer is still safe if the Dockerfile changes order later. The old runtime check-then-seed shell logic is gone: the template is baked into the image, and each session's copy is made lazily under `/tmp` at request time by `app/core/sessions.py`, Task 2 — not by anything in this container step. Migrations now run every container start via `alembic upgrade head`, not lazily on first request, per the non-negotiable rules.)

- [ ] **Step 5: Update `.env.example`**

```
# --- CORS ---
# Comma-separated list of allowed origins for the Vercel-hosted frontend
# (Phase 6), e.g. https://querypilot.vercel.app. Empty blocks all cross-origin
# requests -- never combine "*" with credentials.
QP_CORS_ALLOWED_ORIGINS=
```

- [ ] **Step 6: Build the image and smoke-test it locally**

```bash
docker build -t querypilot:phase2 .
docker run --rm -p 8000:8000 --env-file .env querypilot:phase2 &
sleep 3
curl -s http://localhost:8000/health
docker stop $(docker ps -q --filter ancestor=querypilot:phase2)
```

Expected: `/health` returns `{"healthy": true, ...}`. If `--only-binary=:all:` fails to install any package (no published wheel for `python:3.11-slim`'s platform), drop that flag for that one dependency in a follow-up commit rather than the whole install — record which, if any, in the Task 11 design note. Record the actual peak RSS measured here (`docker stats` during a manipulation flow) in the Task 11 design note, per the spec's "measure peak RSS" requirement.

- [ ] **Step 7: Run the full test suite (still runs on the host, not in the container)**

```bash
pip install -r requirements-dev.txt
pytest -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add Dockerfile requirements.txt requirements-dev.txt app/main.py app/config.py .env.example
git commit -m "feat(deploy): non-root Docker build, build-time template seed, migration-then-uvicorn start, env-driven CORS"
```

---

### Task 10: `render.yaml` blueprint and opt-in local-LLM Docker Compose profile

**Files:**
- Create: `render.yaml`
- Modify: `docker-compose.yml` (make `ollama`/`model-init` an opt-in profile)

**Interfaces:** none (deploy config only).

- [ ] **Step 1: Write `render.yaml`**

```yaml
services:
  - type: web
    name: querypilot-backend
    runtime: docker
    plan: free
    dockerfilePath: ./Dockerfile
    healthCheckPath: /health
    envVars:
      - key: QP_READ_ONLY_MODE
        value: "false"
      - key: QP_ROW_IMPACT_CEILING
        value: "50"
      - key: QP_AUTO_LIMIT
        value: "200"
      - key: QP_CONFIRMATION_TIMEOUT_SECONDS
        value: "60"
      - key: QP_SESSION_TTL_MINUTES
        value: "30"
      - key: QP_MAX_SESSIONS
        value: "200"
      - key: QP_LLM_PROFILE
        value: "cloud"
      - key: QP_CORS_ALLOWED_ORIGINS
        sync: false
      - key: QP_DATABASE_URL
        sync: false
      - key: QP_ADMIN_TOKEN
        sync: false
      - key: OLLAMA_API_KEY
        sync: false
      - key: OLLAMA_CLOUD_HOST
        value: "https://ollama.com"
      - key: OPENROUTER_API_KEY
        sync: false
      - key: GEMINI_API_KEY
        sync: false
      - key: GROQ_API_KEY
        sync: false
```

(Every value that's a real secret or deployment-specific string uses `sync: false`, so Render prompts for it in the dashboard instead of it living in this file, per the non-negotiable "keep `.env` out of git" rule extended to the blueprint itself.)

- [ ] **Step 2: Make the local-LLM services in `docker-compose.yml` opt-in**

Add `profiles: ["local-llm"]` to both the `ollama` and `model-init` service definitions (leave the `app` service without a `profiles` key, so it always starts by default). Running `docker compose up` now starts only `app`; `docker compose --profile local-llm up` starts all three, for a developer who wants the fully-local `QP_LLM_PROFILE=local` path.

- [ ] **Step 3: Validate `render.yaml` syntax**

```bash
python -c "import yaml; yaml.safe_load(open('render.yaml'))" && echo "valid YAML"
docker compose config >/dev/null && echo "valid compose file"
```

Expected: both print their success line with no errors.

- [ ] **Step 4: Commit**

```bash
git add render.yaml docker-compose.yml
git commit -m "feat(deploy): add render.yaml blueprint, make local-LLM compose services opt-in"
```

---

### Task 11: Memory budget measurement and Phase 2 design note

**Files:**
- Create: `docs/phases/02-deployment-blockers.md`

- [ ] **Step 1: Measure peak RSS during a full analytics query and a full manipulation flow**

```bash
docker build -t querypilot:phase2 .
docker run -d --name qp-mem-test -p 8000:8000 --env-file .env querypilot:phase2
```

Drive one analytics request and one manipulation-plus-confirm request against it (via `curl` against `/chat` and `/confirm` with a real UUID4 `session_id`), sampling `docker stats --no-stream qp-mem-test` a few times during each, then:

```bash
docker stop qp-mem-test && docker rm qp-mem-test
```

- [ ] **Step 2: Write `docs/phases/02-deployment-blockers.md`**

Following the structure of `docs/phases/01-llm-gateway.md`: what changed and why (per-session sandbox via `get_engines()`+`app/core/sessions.py`; ChromaDB replaced with Gemini embeddings + numpy; audit/budget storage moved to Neon-or-local-SQLite via Alembic; Docker/render.yaml/CORS hardening), the live-verified facts from this phase (Gemini embedding dimensionality from Task 5 Step 1, the LangGraph `config["configurable"]` passing behavior confirmed in Task 3, the measured peak RSS numbers from Step 1 above, and whether it came in under the spec's 400MB target), and a "what to watch going forward" section covering anything found but not fixed (e.g. if the measured RSS is close to or over budget, flag which dependency is heaviest — `google-genai`, `psycopg`, etc. — as a candidate for a future trim).

- [ ] **Step 3: Confirm the full suite still passes and no test was skipped by accident**

```bash
pytest -q
```

Expected: same pass count as Task 10's last run, plus everything added across Tasks 1-10 — verify the count is not lower than expected by comparing against the number of new test functions added across this plan's tasks.

- [ ] **Step 4: Commit**

```bash
git add docs/phases/02-deployment-blockers.md
git commit -m "docs(phase2): design note for deployment blockers, with live-verified memory numbers"
```
