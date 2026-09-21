"""C2: ambiguity_check_node was missing BYOK threading -- a BYOK-covered
request was silently billing this LLM call to the shared keys. Mirrors
tests/test_self_corrector.py's test shapes.
"""
from __future__ import annotations

from unittest.mock import patch

from app.agent.nodes.ambiguity_check import ambiguity_check_node

_STATE = {
    "user_input": "how many orders are pending?",
    "generated_sql": "SELECT COUNT(*) FROM orders WHERE status = 'pending';",
    "relevant_schema": "orders(id, status)",
    "query_history": [],
}


class _Result:
    is_ambiguous = False
    clarifying_question = None
    assumptions = []


def test_ambiguity_check_works_without_config_like_before():
    with patch("app.agent.nodes.ambiguity_check.structured_chat") as mock_chat:
        mock_chat.return_value = _Result()
        ambiguity_check_node(_STATE)

    assert mock_chat.call_args.kwargs["byok"] is None


def test_ambiguity_check_threads_byok_into_structured_chat():
    config = {"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}}
    with patch("app.agent.nodes.ambiguity_check.structured_chat") as mock_chat:
        mock_chat.return_value = _Result()
        ambiguity_check_node(_STATE, config)

    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}


def test_ambiguity_check_node_is_registered_with_config_injection_working():
    """End-to-end through the real LangGraph node dispatch -- proves the
    Optional[RunnableConfig] annotation is one LangGraph's config-injection
    check actually recognizes."""
    from app.agent.graph import build_graph

    graph = build_graph()
    node = graph.nodes["ambiguity_check"].bound
    with patch("app.agent.nodes.ambiguity_check.structured_chat") as mock_chat:
        mock_chat.return_value = _Result()
        node.invoke(_STATE, config={"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}})

    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}
