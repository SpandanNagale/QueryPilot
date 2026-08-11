from __future__ import annotations

from app.agent.state import AgentState
from app.core.schema_store import format_schema_context, retrieve_relevant_tables


def schema_retriever_node(state: AgentState) -> dict:
    tables = retrieve_relevant_tables(state["user_input"])
    return {"relevant_schema": format_schema_context(tables)}
