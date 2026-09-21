"""FastAPI app: SSE chat endpoint, confirmation resume, schema/audit/health.

Session state is persisted via LangGraph's SQLite checkpointer keyed by
session_id (== thread_id), so a session survives an API process restart.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy import text

from app.agent.graph import build_graph
from app.agent.nodes.intent_classifier import intent_classifier_node
from app.agent.nodes.sql_generator import sql_generator_node
from app.agent.prompts import get_schema_tour
from app.config import settings
from app.core.audit import read_events
from app.core.db import get_engines, read_only_engine
from app.core.errors import AppError, ErrorCode, classify_exception_to_code, classify_reason_to_code
from app.core.llm import check_providers
from app.core.llm_events import register_llm_attempt_listener
from app.core.rate_limit import check_rate_limit
from app.core.request_context import set_session_id
from app.core.schema_store import (
    build_or_refresh_index,
    format_schema_context,
    full_schema_summary,
    introspect_tables,
    retrieve_relevant_tables,
)
from app.core import budget, sessions

logger = logging.getLogger(__name__)


def _resolve_session_engine(session_id: str | None):
    """Session-aware read-only engine for GET endpoints: returns the template
    engine when no session_id is given; otherwise provisions (if needed) and
    returns that session's own sandbox engine."""
    if session_id is None:
        return read_only_engine
    if not sessions.is_valid_session_id(session_id):
        raise HTTPException(400, detail=_error_detail(ErrorCode.SESSION_INVALID))
    db_path = sessions.get_session_db_path(session_id)
    ro, _ = get_engines(str(db_path))
    return ro


def _require_valid_session_id(session_id: str) -> None:
    if not sessions.is_valid_session_id(session_id):
        raise HTTPException(400, detail=_error_detail(ErrorCode.SESSION_INVALID))


def _client_ip(request: Request) -> str:
    """Render's proxy appends exactly one hop to X-Forwarded-For -- the LAST
    entry is the value Render's own edge appended, and the only trustworthy
    one. Earlier entries (including the first) are caller-supplied and
    trivially spoofable by any client wanting a fresh rate-limit bucket per
    request."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


def _require_rate_limit_ok(request: Request) -> None:
    if not check_rate_limit(_client_ip(request)):
        raise HTTPException(429, detail=_error_detail(ErrorCode.RATE_LIMITED))


_state: dict[str, Any] = {"graph": None, "checkpointer_cm": None}
_pending_confirmations: dict[str, float] = {}

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# node name -> SSE event name for its state update. Nodes not listed here
# either have no user-facing update of their own (e.g. validator succeeding
# silently) or are handled specially (insight_summarizer streams tokens;
# interrupts are handled via the "__interrupt__" pseudo-key).
NODE_EVENT_MAP: dict[str, str] = {
    "sql_generator": "sql_generated",
    "self_corrector": "sql_generated",
    "dry_run_preview": "preview",
    "confirmation_gate": "message",  # only carries a payload when cancelled (no final_response on confirm)
    "analytics_executor": "result",
    "transactional_executor": "result",
    "chart_detector": "chart",
    "followup_suggester": "followups",
    "polite_redirect": "message",
    "schema_question_node": "message",
    "ask_clarifying_question": "message",
    "error_response": "message",
}


# frontend/index.html has no static example-prompts list of its own -- its only
# per-turn "examples" are followup_suggester's dynamic suggestion chips, which
# depend on the prior turn and can't seed a cache at startup. These mirror a
# few of evals/eval_set.json's simple, schema-safe analytics questions instead
# (same domain: cold-drinks wholesale distribution, Ratnagiri district), so
# pre-warming has realistic keys real early traffic is likely to actually hit.
_PREWARM_PROMPTS = (
    "Which products are colas?",
    "List all retailers in Dapoli",
    "Show retailers with a credit limit above 50000",
    "Which order ids were cancelled?",
    "What products cost more than 50 rupees per unit?",
)


# Tracks which prompts this process has already run through pre-warm, so a
# second lifespan trigger (e.g. a second `with TestClient(app)` block, or any
# process that restarts its FastAPI app object without restarting the
# process) doesn't repeat real provider/embedding calls for no benefit -- the
# LLM caches themselves are keyed on more than just the prompt (schema hash,
# context hash), so a cache lookup alone can't cheaply tell us "this exact
# prompt was already attempted" without redoing the (real, billable) schema
# retrieval call first.
_prewarmed_prompts: set[str] = set()


def _prewarm_llm_cache() -> None:
    """Populates intent_classifier's and sql_generator's caches directly via
    the node functions (not the full graph -- no checkpointer state is
    touched). Guarded per-prompt so one failing provider call never blocks
    startup, same pattern as build_or_refresh_index() above.

    Called on a background thread (see lifespan below), not inline: a live
    provider call has no bound on how slow "not-yet-failed" can be (observed
    live: one call took 2+ minutes before the fallback chain moved on) --
    try/except alone only guards against a raised exception, not a hang, and
    startup must not wait minutes for a cache warm-up that's a pure
    performance nicety.

    set_session_id("prewarm") makes these real, billable LLM calls count
    toward the daily budget cap too -- app/core/budget.py's listener
    otherwise silently skips any llm_attempt event with no session in the
    request_context contextvar (see budget.record_llm_attempt), and this
    runs outside any request so that contextvar would otherwise stay unset.
    today_spend_usd() sums BudgetEntry rows across ALL sessions for the day
    (no per-session filter), so this sentinel session id just becomes part
    of the same daily total the cap already checks.
    """
    set_session_id("prewarm")
    for prompt in _PREWARM_PROMPTS:
        if prompt in _prewarmed_prompts:
            continue
        try:
            intent_classifier_node({"user_input": prompt, "query_history": []})
            relevant_schema = format_schema_context(retrieve_relevant_tables(prompt))
            sql_generator_node({"user_input": prompt, "query_history": [], "relevant_schema": relevant_schema})
            _prewarmed_prompts.add(prompt)
        except Exception:  # noqa: BLE001 - one bad pre-warm prompt must never block startup
            logger.exception("LLM cache pre-warm failed for prompt %r; continuing", prompt)


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

    # ponytail: fire-and-forget on a daemon thread rather than a bounded-timeout
    # wrapper around each provider call -- simpler, and a startup-time cache
    # warm-up that finishes late (or never, if the process exits first) is
    # harmless; upgrade to an explicit per-call timeout if orphaned real calls
    # ever prove wasteful (cost/log noise) in practice.
    #
    # Gated on both: llm_cache_enabled (QP_LLM_CACHE_ENABLED=false must mean
    # pre-warm never fires, not just "the cache is a no-op once warmed") and
    # PYTEST_CURRENT_TEST (pytest sets this for the duration of every test
    # run, so real/billable provider calls never fire during the test suite
    # -- no fixture or monkeypatch needed anywhere else).
    if settings.llm_cache_enabled and not os.environ.get("PYTEST_CURRENT_TEST"):
        threading.Thread(target=_prewarm_llm_cache, name="llm-cache-prewarm", daemon=True).start()

    register_llm_attempt_listener(budget.record_llm_attempt)

    yield

    checkpointer_cm.__exit__(None, None, None)


app = FastAPI(title="QueryPilot", lifespan=lifespan)
_cors_origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins) and "*" not in _cors_origins,  # never combine "*" with credentials; an empty list keeps this False too
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str
    # BYOK: when both are present, this one request routes through the
    # caller's own provider+key (app.core.llm._byok_deployment) and bypasses
    # the daily budget cap below. Never logged/persisted -- see
    # _byok_override and app/core/llm.py's _call_byok/_record_attempt.
    byok_provider: str | None = None
    byok_api_key: str | None = None


def _byok_override(req: ChatRequest) -> dict | None:
    if req.byok_provider and req.byok_api_key:
        return {"provider": req.byok_provider, "api_key": req.byok_api_key}
    return None


def _require_budget_ok(byok: dict | None) -> None:
    if byok is not None or settings.daily_budget_usd <= 0.0:
        return
    if budget.today_spend_usd() >= settings.daily_budget_usd:
        raise HTTPException(503, detail=_error_detail(ErrorCode.BUDGET_EXCEEDED))


class ConfirmRequest(BaseModel):
    session_id: str
    confirmed: bool


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _error_detail(code: ErrorCode) -> dict:
    """HTTPException `detail` shape: {code, message} -- same two keys
    `/confirm`'s pre-existing 409 used, sourced from the error catalog."""
    return {"code": code.value, "message": AppError(code).message}


def _redact(text: str, secret: str | None) -> str:
    return text.replace(secret, "[REDACTED]") if secret else text


def _run_graph_stream(session_id: str, config: dict, graph_input) -> Generator[str, None, None]:
    graph = _state["graph"]
    # An ordinary generator-frame local, not the request_context contextvar
    # -- config is stable across every dispatch of this generator regardless
    # of iterate_in_threadpool's per-dispatch context copying (see the
    # set_session_id comment below), since plain locals live in the
    # generator's own frame, untouched by that mechanism. Used only to
    # redact a BYOK key from the exception handler below if the underlying
    # provider call fails and its exception message happens to echo the key
    # back (a common shape for an invalid-key error) -- never logged or
    # emitted itself.
    byok_key = ((config.get("configurable") or {}).get("byok") or {}).get("api_key")
    try:
        stream_iter = graph.stream(graph_input, config, stream_mode=["updates", "custom"])
        while True:
            # Re-armed on every resumption of this generator, not set once
            # up front, because of how Starlette drives a sync
            # StreamingResponse body: iterate_in_threadpool dispatches each
            # next() call through anyio.to_thread.run_sync, which copies a
            # FRESH snapshot of the ambient (never-mutated) request context
            # on every single dispatch -- verified empirically, including
            # that resetting a Token minted in one such copy from inside
            # another raises `ValueError: Token ... was created in a
            # different Context`. A ContextVar.set() before this loop would
            # only be visible for the very first chunk; every later node's
            # LLM call (in a later next(stream_iter), i.e. a later dispatch)
            # would see the var back at its default. Setting it immediately
            # before each next(stream_iter) call keeps it visible in the
            # exact dispatch that runs the node -- which is where
            # app/core/budget.py's llm_attempt listener needs to read it
            # from. Plain set(), no token/reset: each HTTP request already
            # runs in its own isolated asyncio Task, so nothing leaks across
            # requests without one.
            set_session_id(session_id)
            try:
                mode, chunk = next(stream_iter)
            except StopIteration:
                break

            if mode == "custom":
                yield _sse("token", chunk)
                continue

            for node_name, partial in chunk.items():
                if node_name == "__interrupt__":
                    payload = partial[0].value if partial else {}
                    _pending_confirmations[session_id] = time.time()
                    yield _sse("awaiting_confirmation", payload)
                    continue

                if partial and partial.get("error"):
                    reason = partial["error"]
                    if "client_error" in partial:
                        code = ErrorCode.INTERNAL
                        message = partial["client_error"]
                        logger.warning(
                            "Node %s reported an error not shown to the client verbatim: %s", node_name, reason
                        )
                    else:
                        code = classify_reason_to_code(reason)
                        message = reason
                    yield _sse("error", {"code": code.value, "message": message, "node": node_name})

                event_name = NODE_EVENT_MAP.get(node_name)
                if event_name and partial:
                    # "error"/"client_error" are internal-only fields already
                    # surfaced (sanitized) via the "error" SSE event above --
                    # never forward them again raw inside this second,
                    # node-specific event (e.g. analytics_executor's "result"
                    # payload on a DB-exception failure).
                    payload = {k: v for k, v in partial.items() if k not in ("error", "client_error")}
                    if payload:
                        yield _sse(event_name, payload)
    except Exception as exc:  # noqa: BLE001 - surface to the client instead of a bare 500 mid-stream
        # Manually formatted + redacted, not logger.exception(...): that
        # method's own exc_info=True traceback formatting can't be
        # intercepted before it's written, so a BYOK key echoed back in the
        # underlying exception's message (e.g. a provider's invalid-key
        # error) would otherwise reach the server log unredacted --
        # formatting it ourselves first lets us scrub the literal key from
        # every line (message and any chained-exception frames) before it's
        # ever handed to logging.
        sanitized_traceback = _redact(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)), byok_key
        )
        logger.error("Unhandled exception in graph stream (session_id=%s):\n%s", session_id, sanitized_traceback)
        code = classify_exception_to_code(exc)
        yield _sse("error", AppError(code, detail=_redact(str(exc), byok_key), node="server").to_dict())
    finally:
        yield _sse("done", {})


@app.post("/chat")
def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    _require_rate_limit_ok(request)
    _require_valid_session_id(req.session_id)
    byok = _byok_override(req)
    _require_budget_ok(byok)
    db_path = sessions.get_session_db_path(req.session_id)
    config = {"configurable": {"thread_id": req.session_id, "db_path": str(db_path)}}
    if byok is not None:
        config["configurable"]["byok"] = byok
    # Deliberately NOT sending "query_history": [] here: LangGraph's default
    # LastValue channel semantics mean an explicit value in input always
    # overwrites the checkpointed one, even an empty list - that was silently
    # wiping conversation history on every single turn. Every node already
    # reads it via state.get("query_history", []), so omitting the key lets
    # the checkpointer's accumulated value carry forward across turns, and
    # still defaults to [] cleanly on a brand-new thread.
    #
    # "error" is the opposite case: it's genuinely per-turn state, not
    # accumulated history, so explicitly resetting it here is correct (and
    # does not reintroduce the query_history-wiping bug above -- different
    # key, different semantics). Without this, a guardrail block on turn N
    # leaves state["error"] set for every later turn in the same
    # checkpointed session, permanently defeating sql_generator_node's cache
    # guard (its "prior_error is None" check) and injecting a nonsense
    # "Previous error: Guardrail blocked..." into every subsequent
    # SQL-generation prompt for that session.
    graph_input = {"user_input": req.message, "error": None}
    return StreamingResponse(_run_graph_stream(req.session_id, config, graph_input), media_type="text/event-stream")


@app.post("/confirm")
def confirm(req: ConfirmRequest, request: Request) -> StreamingResponse:
    _require_rate_limit_ok(request)
    _require_valid_session_id(req.session_id)
    graph = _state["graph"]
    db_path = sessions.get_session_db_path(req.session_id)
    config = {"configurable": {"thread_id": req.session_id, "db_path": str(db_path)}}
    current = graph.get_state(config)
    if not current.next:
        raise HTTPException(409, detail=_error_detail(ErrorCode.SESSION_EXPIRED))

    requested_at = _pending_confirmations.pop(req.session_id, None)
    timed_out = requested_at is not None and (time.time() - requested_at) > settings.confirmation_timeout_seconds
    resume_value = "timeout" if timed_out else ("yes" if req.confirmed else "no")

    return StreamingResponse(
        _run_graph_stream(req.session_id, config, Command(resume=resume_value)), media_type="text/event-stream"
    )


class SessionResetRequest(BaseModel):
    session_id: str


@app.post("/session/reset")
def session_reset(req: SessionResetRequest) -> dict:
    _require_valid_session_id(req.session_id)
    sessions.reset_session(req.session_id)
    return {"status": "reset", "session_id": req.session_id}


@app.get("/schema")
def schema() -> dict:
    return {"schema": full_schema_summary(), "tour": get_schema_tour()}


@app.get("/health/providers")
def health_providers() -> dict:
    return check_providers()


@app.get("/health/budget")
def health_budget() -> dict:
    spent = budget.today_spend_usd()
    cap = settings.daily_budget_usd
    exceeded = cap > 0.0 and spent >= cap
    return {"spent": spent, "cap": cap, "exceeded": exceeded}


_MAX_TABLE_PREVIEW_LIMIT = 200


@app.get("/tables")
def list_tables(session_id: str | None = None) -> dict:
    """One row per table for the frontend's data browser: name, purpose, columns,
    and a live row count, so a non-technical user can see what's actually in the
    database instead of just column names and types."""
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
    """Paginated raw rows for one table, read-only. This is what the frontend's
    data browser modal renders — plain data, not an LLM-generated query."""
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


@app.get("/audit")
def audit(limit: int = 50, offset: int = 0, session_id: str | None = None, x_admin_token: str | None = Header(None)) -> dict:
    if x_admin_token is not None and settings.admin_token and secrets.compare_digest(x_admin_token, settings.admin_token):
        return {"events": read_events(limit=limit, offset=offset)}  # full log, every session
    if session_id is None:
        raise HTTPException(400, "session_id is required (or provide a valid X-Admin-Token for the full log).")
    _require_valid_session_id(session_id)
    return {"events": read_events(limit=limit, offset=offset, session_id=session_id)}


@app.get("/health")
def health() -> dict:
    try:
        try:
            with read_only_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            db_ok, db_msg = True, "ok"
        except Exception as exc:  # noqa: BLE001 - a down DB is a reported, not raised, health condition
            db_ok, db_msg = False, str(exc)

        return {
            "healthy": db_ok,
            "database": {"available": db_ok, "detail": db_msg},
            "read_only_mode": settings.read_only_mode,
        }
    except Exception as exc:  # noqa: BLE001 - /health must never leak a traceback via FastAPI's default 500 handler
        logger.exception("Unhandled failure building /health response")
        return {"healthy": False, **_error_detail(ErrorCode.INTERNAL)}


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
