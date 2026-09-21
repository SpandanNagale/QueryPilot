"""C1: raw DB-exception text must never reach the client. analytics_executor
and transactional_executor's generic `except Exception` branches now set a
curated `client_error` alongside the existing (internal-only) detailed
`error`. error_response_node prefers `client_error` when present, and
_run_graph_stream's node-error SSE branch does the same -- while a node
without `client_error` (the validator/guardrail path, already curated) keeps
forwarding `error` verbatim as before.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import app.main as main_module
from app.agent.nodes.terminal_responses import error_response_node
from app.config import settings
from app.core import sessions
from app.core.db import get_engines

_RAW_DB_ERROR = "no such table: totally_bogus_table_xyz"


def _config_for_new_session(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    session_id = str(uuid.uuid4())
    db_path = sessions.get_session_db_path(session_id)
    get_engines(str(db_path))  # provisions the sandbox DB file
    return {"configurable": {"thread_id": session_id, "db_path": str(db_path)}}


def test_analytics_executor_sets_client_error_on_db_exception(tmp_path, monkeypatch):
    from app.agent.nodes.analytics_executor import analytics_executor_node

    config = _config_for_new_session(tmp_path, monkeypatch)
    state = {"generated_sql": "SELECT * FROM orders WHERE id = 1", "retry_count": 0}

    with patch("app.agent.nodes.analytics_executor.sql_executor.execute_select") as mock_exec:
        mock_exec.side_effect = RuntimeError(_RAW_DB_ERROR)
        result = analytics_executor_node(state, config)

    assert result["client_error"] == "An unexpected database error occurred while running this query."
    assert _RAW_DB_ERROR in result["error"]


def test_transactional_executor_sets_client_error_on_generic_exception(tmp_path, monkeypatch):
    from app.agent.nodes.transactional_executor import transactional_executor_node

    config = _config_for_new_session(tmp_path, monkeypatch)
    state = {"generated_sql": "DELETE FROM orders WHERE id = 1", "preview": {"table": "orders"}, "user_input": "x"}

    with patch("app.agent.nodes.transactional_executor.sql_executor.execute_write") as mock_exec:
        mock_exec.side_effect = RuntimeError(_RAW_DB_ERROR)
        result = transactional_executor_node(state, config)

    assert result["client_error"] == "an unexpected database error occurred"
    assert _RAW_DB_ERROR in result["error"]
    # final_response is user-facing too -- must not embed the raw exception.
    assert _RAW_DB_ERROR not in result["final_response"]


def test_transactional_executor_integrity_error_path_is_unchanged(tmp_path, monkeypatch):
    """The IntegrityError branch above the generic one is already curated --
    must keep behaving exactly as before (no client_error field at all)."""
    from sqlalchemy.exc import IntegrityError

    from app.agent.nodes.transactional_executor import transactional_executor_node

    config = _config_for_new_session(tmp_path, monkeypatch)
    state = {"generated_sql": "DELETE FROM orders WHERE id = 1", "preview": {"table": "orders"}, "user_input": "x"}

    with patch("app.agent.nodes.transactional_executor.sql_executor.execute_write") as mock_exec:
        mock_exec.side_effect = IntegrityError("stmt", {}, Exception("fk"))
        result = transactional_executor_node(state, config)

    assert "client_error" not in result
    assert "foreign key constraint failed" in result["error"]


def test_error_response_node_prefers_client_error_when_both_set():
    state = {"error": "execution failed: no such table: bogus", "client_error": "a friendly message"}
    result = error_response_node(state)
    assert "no such table" not in result["final_response"]
    assert "a friendly message" in result["final_response"]


def test_error_response_node_falls_back_to_error_when_no_client_error():
    """Backward compat: the validator/guardrail path never sets client_error
    -- must still surface `error` exactly as before."""
    state = {"error": "This would affect 500 rows, which exceeds the safety limit of 50."}
    result = error_response_node(state)
    assert "exceeds the safety limit of 50" in result["final_response"]


# ---------------------------------------------------------------------------
# app/main.py::_run_graph_stream -- client_error preferred in the SSE payload
# ---------------------------------------------------------------------------


class _NodeWithClientErrorGraph:
    def stream(self, *args, **kwargs):
        yield "updates", {
            "analytics_executor": {
                "error": f"execution failed: {_RAW_DB_ERROR}",
                "client_error": "An unexpected database error occurred while running this query.",
            }
        }


class _NodeWithoutClientErrorGraph:
    def stream(self, *args, **kwargs):
        yield "updates", {"validator": {"error": "This would affect 500 rows, which exceeds the safety limit of 50."}}


def test_run_graph_stream_hides_raw_text_when_client_error_present(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _NodeWithClientErrorGraph())
    events = list(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))
    body = "".join(events)

    assert _RAW_DB_ERROR not in body
    assert "An unexpected database error occurred while running this query." in body
    assert '"code": "INTERNAL"' in body
    assert '"node": "analytics_executor"' in body


def test_run_graph_stream_still_forwards_reason_verbatim_without_client_error(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _NodeWithoutClientErrorGraph())
    events = list(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))
    body = "".join(events)

    assert '"code": "ROW_CEILING_EXCEEDED"' in body
    assert "exceeds the safety limit of 50" in body
    assert '"node": "validator"' in body
