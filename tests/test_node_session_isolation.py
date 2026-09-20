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
