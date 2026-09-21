"""self_corrector_node's BYOK wiring: it calls the exact same
generate_sql_raw() sql_generator_node calls, on the same request's
execution-error retry loop (executor -> self_corrector -> executor again),
so a BYOK-covered request must keep using the caller's own key here too --
not silently fall back to the shared/budgeted key for this one retry call.

self_corrector_node never touches app.core.llm_cache at all (regeneration
after an error is always fresh, cache or no cache, BYOK or not) -- so
"bypasses the cache consistently" holds trivially, verified below by source
inspection the same way tests/test_llm_cache.py verifies it for
insight_summarizer_node.
"""
from __future__ import annotations

from unittest.mock import patch

from app.agent.nodes.self_corrector import self_corrector_node

_STATE = {
    "user_input": "how many orders are pending?",
    "relevant_schema": "orders(id, status)",
    "query_history": [],
    "generated_sql": "SELECT * FROM ordrs;",
    "error": "no such table: ordrs",
}


def test_self_corrector_works_without_config_like_before():
    """Backward compatible: no caller passes config today, so it must
    remain optional and default to no BYOK."""
    with patch("app.agent.nodes.self_corrector.generate_sql_raw") as mock_generate:
        mock_generate.return_value = "SELECT * FROM orders;"
        result = self_corrector_node(_STATE)

    assert result["generated_sql"].startswith("SELECT * FROM orders")
    assert mock_generate.call_args.kwargs["byok"] is None


def test_self_corrector_threads_byok_into_generate_sql_raw():
    config = {"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}}
    with patch("app.agent.nodes.self_corrector.generate_sql_raw") as mock_generate:
        mock_generate.return_value = "SELECT * FROM orders;"
        self_corrector_node(_STATE, config)

    assert mock_generate.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}


def test_self_corrector_byok_reaches_the_llm_gateway_as_a_bypass_not_a_cache_hit():
    """One level deeper than the mock above: patch chat() (what
    generate_sql_raw actually calls), and assert the byok kwarg reaches it
    unchanged -- proving the wiring holds through generate_sql_raw's own
    body, not just at the self_corrector_node -> generate_sql_raw boundary."""
    config = {"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}}
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT * FROM orders;"
        result = self_corrector_node(_STATE, config)

    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}
    assert result["generated_sql"].startswith("SELECT * FROM orders")


def test_self_corrector_source_has_no_cache_wiring():
    import inspect

    from app.agent.nodes import self_corrector

    source = inspect.getsource(self_corrector)
    assert "llm_cache" not in source
    assert "cache" not in source.lower()


def test_self_corrector_node_is_registered_with_config_injection_working():
    """End-to-end through the real LangGraph node dispatch (not just a
    direct Python call) -- proves the Optional[RunnableConfig] annotation
    is one LangGraph's config-injection check actually recognizes, the same
    footgun documented on intent_classifier_node/sql_generator_node."""
    from app.agent.graph import build_graph

    graph = build_graph()
    node = graph.nodes["self_corrector"].bound
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT * FROM orders;"
        node.invoke(_STATE, config={"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}})

    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}
