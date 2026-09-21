"""I1: pre-warm's LLM cost is invisible to the daily budget unless a session
is set in app.core.request_context before it runs (budget.record_llm_attempt
skips any llm_attempt event with no session in context). I2+I4: pre-warm
must never fire real network calls during the test suite, and must respect
QP_LLM_CACHE_ENABLED=false.
"""
from __future__ import annotations

import json
import uuid
from functools import partial
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.main as main_module
from app.config import settings
from app.core import budget, llm_events, opdb, request_context
from app.core.models import BudgetEntry
from app.core.opdb import Base


def _use_tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    test_engine = create_engine(settings.resolved_database_url, json_serializer=partial(json.dumps, default=str))
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(opdb, "engine", test_engine)
    monkeypatch.setattr(opdb, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))


def _fake_node_that_bills(state):
    """Stands in for intent_classifier_node/sql_generator_node: makes a real
    llm_attempt-shaped event with a cost, the same as a real LLM call would
    on success."""
    llm_events.emit_llm_attempt(
        alias="chat",
        provider="gemini",
        model="gemini-3.6-flash",
        latency_ms=10.0,
        success=True,
        error="",
        prompt_tokens=5,
        completion_tokens=5,
        cost_usd=0.001,
    )
    return {"intent": "analytics", "generated_sql": "SELECT 1;"}


def test_prewarm_sets_session_sentinel_so_budget_entry_is_written(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    request_context.clear_session_id()
    llm_events.register_llm_attempt_listener(budget.record_llm_attempt)
    try:
        with patch("app.main.intent_classifier_node", side_effect=_fake_node_that_bills), patch(
            "app.main.sql_generator_node", side_effect=_fake_node_that_bills
        ), patch("app.main.format_schema_context", return_value=""), patch(
            "app.main.retrieve_relevant_tables", return_value=[]
        ):
            main_module._prewarmed_prompts.clear()
            main_module._prewarm_llm_cache()

        with opdb.get_session() as s:
            rows = s.query(BudgetEntry).all()
        assert len(rows) > 0
        assert all(r.session_id == "prewarm" for r in rows)
    finally:
        llm_events.clear_llm_attempt_listeners()
        request_context.clear_session_id()
        main_module._prewarmed_prompts.clear()


def test_prewarm_thread_not_started_during_pytest_even_when_cache_enabled(monkeypatch, tmp_path):
    """Real lifespan invocation (via TestClient) with llm_cache_enabled=True
    (the default): PYTEST_CURRENT_TEST is set by pytest for the whole test
    run, so the guard must skip starting the thread anyway -- this is why
    pre-warm's real/billable network calls no longer fire while running this
    very suite."""
    import os as os_module

    from fastapi.testclient import TestClient

    from app.main import app

    assert os_module.environ.get("PYTEST_CURRENT_TEST")  # pytest always sets this while a test runs
    monkeypatch.setattr(settings, "llm_cache_enabled", True)
    with patch("app.main.threading.Thread") as mock_thread:
        with TestClient(app):
            pass
    mock_thread.assert_not_called()


def test_prewarm_thread_not_started_when_cache_disabled(monkeypatch, tmp_path):
    """Real lifespan invocation with llm_cache_enabled=False and
    PYTEST_CURRENT_TEST removed (simulating a real, non-pytest process) --
    the thread must still not start, proving the cache-enabled gate on its
    own (independent of the pytest gate) blocks it."""
    import os as os_module

    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    with patch("app.main.threading.Thread") as mock_thread:
        with TestClient(app):
            pass
    mock_thread.assert_not_called()
