"""FastAPI app: SSE chat endpoint, confirmation resume, schema/audit/health.

Session state is persisted via LangGraph's SQLite checkpointer keyed by
session_id (== thread_id), so a session survives an API process restart.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel
from sqlalchemy import text

from app.agent.graph import build_graph
from app.agent.prompts import get_schema_tour
from app.config import settings
from app.core.audit import read_events
from app.core.db import get_engines, read_only_engine
from app.core.llm import check_providers
from app.core.schema_store import build_or_refresh_index, full_schema_summary, introspect_tables
from app.core import sessions

logger = logging.getLogger(__name__)


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


app = FastAPI(title="QueryPilot", lifespan=lifespan)
_cors_origins = [o.strip() for o in settings.cors_allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=bool(_cors_origins),  # never combine "*" with credentials; an empty list keeps this False too
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ConfirmRequest(BaseModel):
    session_id: str
    confirmed: bool


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _run_graph_stream(session_id: str, config: dict, graph_input) -> Generator[str, None, None]:
    graph = _state["graph"]
    try:
        for mode, chunk in graph.stream(graph_input, config, stream_mode=["updates", "custom"]):
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
                    yield _sse("error", {"node": node_name, "error": partial["error"]})

                event_name = NODE_EVENT_MAP.get(node_name)
                if event_name and partial:
                    yield _sse(event_name, partial)
    except Exception as exc:  # noqa: BLE001 - surface to the client instead of a bare 500 mid-stream
        yield _sse("error", {"node": "server", "error": str(exc)})
    finally:
        yield _sse("done", {})


@app.post("/chat")
def chat(req: ChatRequest) -> StreamingResponse:
    _require_valid_session_id(req.session_id)
    db_path = sessions.get_session_db_path(req.session_id)
    config = {"configurable": {"thread_id": req.session_id, "db_path": str(db_path)}}
    # Deliberately NOT sending "query_history": [] here: LangGraph's default
    # LastValue channel semantics mean an explicit value in input always
    # overwrites the checkpointed one, even an empty list - that was silently
    # wiping conversation history on every single turn. Every node already
    # reads it via state.get("query_history", []), so omitting the key lets
    # the checkpointer's accumulated value carry forward across turns, and
    # still defaults to [] cleanly on a brand-new thread.
    graph_input = {"user_input": req.message}
    return StreamingResponse(_run_graph_stream(req.session_id, config, graph_input), media_type="text/event-stream")


@app.post("/confirm")
def confirm(req: ConfirmRequest) -> StreamingResponse:
    _require_valid_session_id(req.session_id)
    graph = _state["graph"]
    db_path = sessions.get_session_db_path(req.session_id)
    config = {"configurable": {"thread_id": req.session_id, "db_path": str(db_path)}}
    current = graph.get_state(config)
    if not current.next:
        raise HTTPException(
            409,
            detail={"code": "SESSION_EXPIRED", "message": "No pending confirmation for this session. It may have expired or already been resolved."},
        )

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
    if x_admin_token is not None and settings.admin_token and x_admin_token == settings.admin_token:
        return {"events": read_events(limit=limit, offset=offset)}  # full log, every session
    if session_id is None:
        raise HTTPException(400, "session_id is required (or provide a valid X-Admin-Token for the full log).")
    _require_valid_session_id(session_id)
    return {"events": read_events(limit=limit, offset=offset, session_id=session_id)}


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


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
