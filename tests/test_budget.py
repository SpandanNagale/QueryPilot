"""Daily budget kill-switch: app/core/budget.py's listener + UTC-day sum,
GET /health/budget, /chat's 503 BUDGET_EXCEEDED enforcement, and BYOK's
bypass of that cap. The final section proves the BYOK key never ends up in
a BudgetEntry row, an AuditEvent row, an llm_attempt event, or captured log
output for a BYOK-covered request -- a real negative assertion (grep for the
literal secret string), not just "no exception was raised".
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from functools import partial

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import app.main as main_module
from app.config import settings
from app.core import budget, opdb, request_context
from app.core.models import AuditEvent, BudgetEntry
from app.core.opdb import Base


def _use_tmp_db(tmp_path, monkeypatch):
    """Same pattern as tests/test_audit.py::_use_tmp_db -- points opdb's
    module-level engine/SessionLocal at a fresh tmp-file SQLite DB so this
    suite never touches the real local data/operational.db."""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    test_engine = create_engine(settings.resolved_database_url, json_serializer=partial(json.dumps, default=str))
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(opdb, "engine", test_engine)
    monkeypatch.setattr(opdb, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))


def _make_request(client_host: str = "0.0.0.0") -> Request:
    scope = {"type": "http", "headers": [], "client": (client_host, 12345)}
    return Request(scope)


def _drain_body(response) -> str:
    """StreamingResponse.body_iterator is an async generator (Starlette
    wraps a sync generator via iterate_in_threadpool at construction time),
    so consuming it outside a real ASGI request cycle needs its own event
    loop."""

    async def _collect() -> str:
        return "".join([chunk async for chunk in response.body_iterator])

    return asyncio.run(_collect())


@pytest.fixture(autouse=True)
def _reset_context():
    request_context.clear_session_id()
    yield
    request_context.clear_session_id()


# ---------------------------------------------------------------------------
# app/core/budget.py: record_llm_attempt listener
# ---------------------------------------------------------------------------


def test_record_llm_attempt_writes_budget_entry_when_session_and_cost_present(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    session_id = str(uuid.uuid4())
    request_context.set_session_id(session_id)

    budget.record_llm_attempt({"provider": "gemini", "model": "gemini-3.6-flash", "cost_usd": 0.02})

    with opdb.get_session() as s:
        rows = s.query(BudgetEntry).all()
    assert len(rows) == 1
    assert rows[0].session_id == session_id
    assert rows[0].provider == "gemini"
    assert rows[0].model == "gemini-3.6-flash"
    assert rows[0].cost_usd == 0.02


def test_record_llm_attempt_skips_when_cost_is_none(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    request_context.set_session_id(str(uuid.uuid4()))

    budget.record_llm_attempt({"provider": "gemini", "model": "gemini-3.6-flash", "cost_usd": None})

    with opdb.get_session() as s:
        assert s.query(BudgetEntry).count() == 0


def test_record_llm_attempt_skips_when_no_session_in_context(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    # no set_session_id() -- simulates the startup cache pre-warm thread,
    # which calls node functions directly, outside any request.

    budget.record_llm_attempt({"provider": "gemini", "model": "gemini-3.6-flash", "cost_usd": 0.02})

    with opdb.get_session() as s:
        assert s.query(BudgetEntry).count() == 0


# ---------------------------------------------------------------------------
# app/core/budget.py: today_spend_usd
# ---------------------------------------------------------------------------


def _insert_entry(session_id: str, cost: float, timestamp: datetime) -> None:
    with opdb.get_session() as s:
        s.add(BudgetEntry(session_id=session_id, provider="gemini", model="m", cost_usd=cost, timestamp=timestamp))


def test_today_spend_usd_sums_only_todays_entries(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    _insert_entry(str(uuid.uuid4()), 1.0, now)
    _insert_entry(str(uuid.uuid4()), 2.0, now - timedelta(days=1))  # yesterday -- excluded

    assert budget.today_spend_usd() == 1.0


def test_today_spend_usd_respects_utc_day_boundary(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    start_of_today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    _insert_entry(str(uuid.uuid4()), 5.0, start_of_today - timedelta(seconds=1))  # just before midnight -- excluded
    _insert_entry(str(uuid.uuid4()), 7.0, start_of_today + timedelta(seconds=1))  # just after -- included

    assert budget.today_spend_usd() == 7.0


def test_today_spend_usd_is_zero_with_no_entries(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    assert budget.today_spend_usd() == 0.0


# ---------------------------------------------------------------------------
# GET /health/budget
# ---------------------------------------------------------------------------


def test_health_budget_shape_and_sum(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "daily_budget_usd", 10.0)
    _insert_entry(str(uuid.uuid4()), 3.5, datetime.now(timezone.utc))

    body = main_module.health_budget()

    assert body == {"spent": 3.5, "cap": 10.0, "exceeded": False}


def test_health_budget_exceeded_false_when_cap_zero(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "daily_budget_usd", 0.0)
    _insert_entry(str(uuid.uuid4()), 999.0, datetime.now(timezone.utc))

    assert main_module.health_budget()["exceeded"] is False


def test_health_budget_exceeded_true_when_over_cap(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    _insert_entry(str(uuid.uuid4()), 1.0, datetime.now(timezone.utc))  # exactly at cap

    assert main_module.health_budget()["exceeded"] is True


# ---------------------------------------------------------------------------
# /chat: 503 BUDGET_EXCEEDED enforcement, and BYOK's bypass
# ---------------------------------------------------------------------------


def test_chat_returns_503_budget_exceeded_when_over_cap(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    _insert_entry(str(uuid.uuid4()), 1.0, datetime.now(timezone.utc))

    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")
    with pytest.raises(HTTPException) as exc_info:
        main_module.chat(req, _make_request("10.5.0.1"))

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "BUDGET_EXCEEDED"


def test_chat_allows_request_when_under_cap(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "daily_budget_usd", 100.0)
    _insert_entry(str(uuid.uuid4()), 1.0, datetime.now(timezone.utc))
    monkeypatch.setitem(main_module._state, "graph", _EmptyGraph())

    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")
    response = main_module.chat(req, _make_request("10.5.0.2"))
    assert response.status_code == 200


def test_chat_byok_bypasses_budget_cap_even_when_over_budget(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    _insert_entry(str(uuid.uuid4()), 50.0, datetime.now(timezone.utc))  # way over cap
    monkeypatch.setitem(main_module._state, "graph", _EmptyGraph())

    req = main_module.ChatRequest(
        session_id=str(uuid.uuid4()), message="hi", byok_provider="gemini", byok_api_key="a-key"
    )
    response = main_module.chat(req, _make_request("10.5.0.3"))  # must not raise
    assert response.status_code == 200


def test_chat_byok_requires_both_fields_to_bypass(tmp_path, monkeypatch):
    """Only one of byok_provider/byok_api_key set -- brief says "when both
    are present"; a half-filled BYOK must not silently bypass the cap."""
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "daily_budget_usd", 1.0)
    _insert_entry(str(uuid.uuid4()), 1.0, datetime.now(timezone.utc))

    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi", byok_provider="gemini")
    with pytest.raises(HTTPException) as exc_info:
        main_module.chat(req, _make_request("10.5.0.4"))
    assert exc_info.value.status_code == 503


class _EmptyGraph:
    """A minimal graph whose .stream() yields nothing -- proves chat()
    reaches graph construction (no exception raised before it) without
    needing a real LangGraph instance or LLM calls."""

    def stream(self, *args, **kwargs):
        return iter(())


# ---------------------------------------------------------------------------
# BYOK key never logged/persisted, for a request that goes through /chat.
# ---------------------------------------------------------------------------

_SECRET_BYOK_KEY = "sk-super-secret-byok-key-should-never-leak-98765"


class _BudgetWritingGraph:
    """Simulates a graph whose nodes make a real (accounted) LLM attempt and
    write a real audit event during the stream -- close enough to a real
    request's side effects to make the negative assertion meaningful,
    without needing a live LLM provider call."""

    def __init__(self, session_id: str):
        self._session_id = session_id

    def stream(self, *args, **kwargs):
        from app.core import audit
        from app.core.llm_events import emit_llm_attempt

        emit_llm_attempt(
            alias="chat",
            provider="gemini",
            model="gemini-3.6-flash",
            latency_ms=12.0,
            success=True,
            error="",
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.01,
        )
        audit.log_event(session_id=self._session_id, event="manipulation_executed", row_count=1)
        yield "updates", {"sql_generator": {"generated_sql": "SELECT 1;"}}


def test_byok_key_never_appears_in_budget_entry_audit_or_log_for_chat_request(tmp_path, monkeypatch, caplog):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.core import llm_events

    llm_events.register_llm_attempt_listener(budget.record_llm_attempt)
    try:
        session_id = str(uuid.uuid4())
        monkeypatch.setitem(main_module._state, "graph", _BudgetWritingGraph(session_id))

        req = main_module.ChatRequest(
            session_id=session_id, message="hi", byok_provider="gemini", byok_api_key=_SECRET_BYOK_KEY
        )
        with caplog.at_level("DEBUG"):
            response = main_module.chat(req, _make_request("10.5.0.5"))
            body = _drain_body(response)

        assert _SECRET_BYOK_KEY not in body

        with opdb.get_session() as s:
            budget_rows = s.query(BudgetEntry).filter(BudgetEntry.session_id == session_id).all()
            audit_rows = s.query(AuditEvent).filter(AuditEvent.session_id == session_id).all()
        assert len(budget_rows) == 1  # the simulated llm_attempt WAS recorded
        assert _SECRET_BYOK_KEY not in json.dumps([r.__dict__ for r in budget_rows], default=str)
        assert len(audit_rows) == 1
        assert _SECRET_BYOK_KEY not in json.dumps([r.payload for r in audit_rows], default=str)

        assert _SECRET_BYOK_KEY not in caplog.text
    finally:
        llm_events.clear_llm_attempt_listeners()


class _KeyLeakingExceptionGraph:
    """Simulates a provider SDK exception whose message echoes the caller's
    own (invalid) key back -- a plausible real shape for a 401 invalid-key
    error, and exactly the scenario _run_graph_stream's redaction guards
    against. Raises when .stream() is CALLED (mirrors test_main_errors.py's
    _ExplodingGraph), so it's caught by the same outer except Exception
    block as a mid-stream failure would be."""

    def stream(self, *args, **kwargs):
        raise RuntimeError(f"Invalid API key: {_SECRET_BYOK_KEY}")


def test_byok_key_never_appears_in_logs_when_the_underlying_call_fails(tmp_path, monkeypatch, caplog):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = str(uuid.uuid4())
    monkeypatch.setitem(main_module._state, "graph", _KeyLeakingExceptionGraph())

    req = main_module.ChatRequest(
        session_id=session_id, message="hi", byok_provider="gemini", byok_api_key=_SECRET_BYOK_KEY
    )
    with caplog.at_level("DEBUG"):
        response = main_module.chat(req, _make_request("10.5.0.6"))
        body = _drain_body(response)

    # the client-facing SSE body never carried the raw exception text to
    # begin with (that's test_main_errors.py's job) -- the new behavior
    # under test is the *log* output.
    assert _SECRET_BYOK_KEY not in body
    assert '"code": "INTERNAL"' in body

    assert _SECRET_BYOK_KEY not in caplog.text
    # ... and something reasonable IS still logged -- the exception wasn't
    # silently swallowed by the redaction, just sanitized.
    assert "Unhandled exception in graph stream" in caplog.text
    assert session_id in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "RuntimeError" in caplog.text


def test_non_byok_request_exception_logging_is_unaffected_by_redaction(tmp_path, monkeypatch, caplog):
    """No byok on this request -- _redact must be a no-op (nothing to
    redact), so the real exception text still reaches the log same as
    before this fix, for the ordinary (non-BYOK) failure path."""
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = str(uuid.uuid4())

    class _PlainExplodingGraph:
        def stream(self, *args, **kwargs):
            raise RuntimeError("plain failure, no key involved")

    monkeypatch.setitem(main_module._state, "graph", _PlainExplodingGraph())
    req = main_module.ChatRequest(session_id=session_id, message="hi")
    with caplog.at_level("DEBUG"):
        response = main_module.chat(req, _make_request("10.5.0.7"))
        _drain_body(response)

    assert "plain failure, no key involved" in caplog.text
    assert "[REDACTED]" not in caplog.text
