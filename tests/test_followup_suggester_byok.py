"""C2: followup_suggester_node was missing BYOK threading -- a BYOK-covered
request was silently billing this LLM call to the shared keys. Mirrors
tests/test_self_corrector.py's test shapes.
"""
from __future__ import annotations

from unittest.mock import patch

from app.agent.nodes.followup_suggester import followup_suggester_node

_STATE = {
    "user_input": "how many orders are pending?",
    "execution_result": {"columns": ["count"], "rows": [{"count": 3}], "row_count": 1, "truncated": False},
}


def test_followup_suggester_works_without_config_like_before():
    with patch("app.agent.nodes.followup_suggester.chat") as mock_chat:
        mock_chat.return_value = "- one\n- two"
        followup_suggester_node(_STATE)

    assert mock_chat.call_args.kwargs["byok"] is None


def test_followup_suggester_threads_byok_into_chat():
    config = {"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}}
    with patch("app.agent.nodes.followup_suggester.chat") as mock_chat:
        mock_chat.return_value = "- one\n- two"
        followup_suggester_node(_STATE, config)

    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}


def test_followup_suggester_node_is_registered_with_config_injection_working():
    """End-to-end through the real LangGraph node dispatch -- proves the
    Optional[RunnableConfig] annotation is one LangGraph's config-injection
    check actually recognizes."""
    from app.agent.graph import build_graph

    graph = build_graph()
    node = graph.nodes["followup_suggester"].bound
    with patch("app.agent.nodes.followup_suggester.chat") as mock_chat:
        mock_chat.return_value = "- one\n- two"
        node.invoke(_STATE, config={"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}})

    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}
