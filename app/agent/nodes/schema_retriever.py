from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from app.agent.state import AgentState
from app.core.audit import log_event
from app.core.schema_store import format_schema_context, retrieve_relevant_tables

_FULL_SCHEMA_TABLE_COUNT = 6  # from TABLE_PURPOSES in schema_store.py; a retrieval result at or above
# this count means retrieve_relevant_tables() fell back to the full schema rather than really ranking


def schema_retriever_node(state: AgentState, config: RunnableConfig) -> dict:
    tables = retrieve_relevant_tables(state["user_input"])
    update: dict = {"relevant_schema": format_schema_context(tables)}
    if len(tables) >= _FULL_SCHEMA_TABLE_COUNT:
        log_event(event="degraded", session_id=config["configurable"]["thread_id"], reason="schema_embedding_fallback")
        update["degraded"] = True  # surfaced as this node's node_state (app/main.py::_run_graph_stream)
    return update
