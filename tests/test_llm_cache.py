"""Tests for app.core.llm_cache and its wiring into intent_classifier_node /
sql_generator_node. The critical property this suite proves: a cache hit is
never a shortcut around the safety pipeline -- cached SQL still runs through
validate_sql exactly like freshly-generated SQL (test below runs the real
node chain, not just the cache module in isolation).
"""
from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from app.agent.nodes.intent_classifier import intent_classifier_node
from app.agent.nodes.sql_generator import sql_generator_node
from app.agent.nodes.validator_node import validator_node
from app.config import settings
from app.core import llm_cache, llm_events


@pytest.fixture(autouse=True)
def _reset_cache_state():
    llm_cache.clear()
    llm_events.clear_cache_hit_listeners()
    yield
    llm_cache.clear()
    llm_events.clear_cache_hit_listeners()


def _validator_config() -> dict:
    return {"configurable": {"db_path": str(settings.template_db_path)}}


# ---------------------------------------------------------------------------
# intent_classifier_node
# ---------------------------------------------------------------------------


def test_intent_classifier_cache_hit_skips_structured_chat():
    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")

        first = intent_classifier_node({"user_input": "How many orders are pending?", "query_history": []})
        second = intent_classifier_node({"user_input": "How many orders are pending?", "query_history": []})

    assert first == {"intent": "analytics"}
    assert second == {"intent": "analytics"}
    mock_chat.assert_called_once()


def test_intent_classifier_cache_miss_on_different_question():
    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")

        intent_classifier_node({"user_input": "How many orders are pending?", "query_history": []})
        intent_classifier_node({"user_input": "How many retailers are in Dapoli?", "query_history": []})

    assert mock_chat.call_count == 2


def test_intent_classifier_normalized_variant_still_hits():
    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")

        intent_classifier_node({"user_input": "How many orders are pending?", "query_history": []})
        intent_classifier_node({"user_input": "  HOW MANY   orders are PENDING?  ", "query_history": []})

    mock_chat.assert_called_once()


def test_intent_classifier_cache_hit_emits_cache_hit_event():
    received = []
    llm_events.register_cache_hit_listener(received.append)

    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")
        intent_classifier_node({"user_input": "How many orders are pending?", "query_history": []})
        intent_classifier_node({"user_input": "How many orders are pending?", "query_history": []})

    assert len(received) == 1  # only the second (hit) call emits
    event = received[0]
    assert event["event_type"] == "cache_hit"
    assert event["node"] == "intent_classifier"
    assert "tokens_saved" in event and "cost_saved" in event and "seconds_saved" in event


def test_intent_classifier_different_query_history_is_a_cache_miss():
    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")

        history_a = [{"nl_request": "x", "sql": "SELECT 1;", "result_summary": "1 row"}]
        history_b = [{"nl_request": "y", "sql": "SELECT 2;", "result_summary": "1 row"}]
        intent_classifier_node({"user_input": "follow up", "query_history": history_a})
        intent_classifier_node({"user_input": "follow up", "query_history": history_b})

    assert mock_chat.call_count == 2


# ---------------------------------------------------------------------------
# sql_generator_node
# ---------------------------------------------------------------------------


def test_sql_generator_cache_hit_skips_chat():
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT name FROM products;"

        state = {"user_input": "Which products are colas?", "relevant_schema": "products(name, category)", "query_history": []}
        first = sql_generator_node(state)
        second = sql_generator_node(state)

    assert first == second == {"generated_sql": "SELECT name FROM products;"}
    mock_chat.assert_called_once()


def test_sql_generator_cache_miss_when_schema_differs():
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT name FROM products;"

        sql_generator_node({"user_input": "Which products are colas?", "relevant_schema": "products(name, category)", "query_history": []})
        sql_generator_node({"user_input": "Which products are colas?", "relevant_schema": "retailers(name, town)", "query_history": []})

    assert mock_chat.call_count == 2


def test_sql_generator_retry_after_validation_failure_never_uses_cache():
    """A validator-failure retry (state["error"] set, routed back to
    sql_generator by route_after_validation) must always call the LLM fresh
    -- the cache key doesn't encode prior_error, so a cache hit here would
    silently replay the exact SQL the validator just rejected."""
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.side_effect = ["SELECT bad FROM products;", "SELECT name FROM products;"]

        state = {
            "user_input": "Which products are colas?",
            "relevant_schema": "products(name, category)",
            "query_history": [],
            "error": "column 'bad' does not exist",
        }
        first = sql_generator_node(state)
        second = sql_generator_node(state)

    assert mock_chat.call_count == 2
    assert first["generated_sql"] != second["generated_sql"]


def test_sql_generator_cache_disabled_via_setting(monkeypatch):
    monkeypatch.setattr(settings, "llm_cache_enabled", False)
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT name FROM products;"

        state = {"user_input": "Which products are colas?", "relevant_schema": "products(name, category)", "query_history": []}
        sql_generator_node(state)
        sql_generator_node(state)

    assert mock_chat.call_count == 2


# ---------------------------------------------------------------------------
# The critical safety property: a cache hit is never a shortcut around
# validate_sql. This runs the real node chain (sql_generator_node ->
# validator_node), not just the cache module in isolation.
# ---------------------------------------------------------------------------


def test_cache_hit_sql_still_runs_through_validate_sql():
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT name FROM products;"

        state = {"user_input": "Which products are colas?", "relevant_schema": "products(name, category)", "query_history": []}
        sql_generator_node(state)  # miss: populates the cache
        cached_result = sql_generator_node(state)  # hit: no LLM call

    mock_chat.assert_called_once()
    cached_sql = cached_result["generated_sql"]

    validator_result = validator_node({"generated_sql": cached_sql, "intent": "analytics"}, _validator_config())

    assert validator_result["validation_result"]["passed"] is True
    assert validator_result["error"] is None


def test_cache_hit_of_invalid_sql_is_still_blocked_by_validate_sql():
    """A cached SQL string that references a nonexistent table/column must
    still be blocked on every hit -- caching never weakens the validator."""
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT * FROM customers;"  # "customers" doesn't exist in this schema

        state = {"user_input": "made up query", "relevant_schema": "products(name, category)", "query_history": []}
        sql_generator_node(state)
        cached_result = sql_generator_node(state)

    validator_result = validator_node({"generated_sql": cached_result["generated_sql"], "intent": "analytics"}, _validator_config())

    assert validator_result["validation_result"]["passed"] is False
    assert validator_result["error"]


# ---------------------------------------------------------------------------
# insight_summarizer must never be touched by the cache wiring.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BYOK bypasses the cache entirely (miss AND store) -- see
# app/agent/nodes/intent_classifier.py and sql_generator.py's decision
# comment: the caller's own key must answer a BYOK-covered request, not a
# cached answer the shared budget's key produced (and a BYOK response must
# never get stored where a later non-BYOK request could hit it for free).
# ---------------------------------------------------------------------------


def _byok_config() -> dict:
    return {"configurable": {"byok": {"provider": "gemini", "api_key": "byok-key"}}}


def test_intent_classifier_byok_always_calls_structured_chat_even_when_cached():
    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")

        state = {"user_input": "How many orders are pending?", "query_history": []}
        intent_classifier_node(state)  # populates the (non-BYOK) cache
        intent_classifier_node(state, _byok_config())  # BYOK -- must not hit that cache

    assert mock_chat.call_count == 2
    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}


def test_intent_classifier_byok_response_is_never_stored_in_the_cache():
    with patch("app.agent.nodes.intent_classifier.structured_chat") as mock_chat:
        mock_chat.return_value = Mock(intent="analytics")
        state = {"user_input": "How many orders are pending?", "query_history": []}

        intent_classifier_node(state, _byok_config())  # BYOK -- must not populate the cache
        intent_classifier_node(state)  # non-BYOK -- must still be a cache miss

    assert mock_chat.call_count == 2


def test_sql_generator_byok_always_calls_chat_even_when_cached():
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT name FROM products;"
        state = {"user_input": "Which products are colas?", "relevant_schema": "products(name, category)", "query_history": []}

        sql_generator_node(state)  # populates the (non-BYOK) cache
        sql_generator_node(state, _byok_config())  # BYOK -- must not hit that cache

    assert mock_chat.call_count == 2
    assert mock_chat.call_args.kwargs["byok"] == {"provider": "gemini", "api_key": "byok-key"}


def test_sql_generator_byok_response_is_never_stored_in_the_cache():
    with patch("app.agent.nodes.sql_generator.chat") as mock_chat:
        mock_chat.return_value = "SELECT name FROM products;"
        state = {"user_input": "Which products are colas?", "relevant_schema": "products(name, category)", "query_history": []}

        sql_generator_node(state, _byok_config())  # BYOK -- must not populate the cache
        sql_generator_node(state)  # non-BYOK -- must still be a cache miss

    assert mock_chat.call_count == 2


def test_insight_summarizer_source_has_no_cache_wiring():
    import inspect

    from app.agent.nodes import insight_summarizer

    source = inspect.getsource(insight_summarizer)
    assert "llm_cache" not in source
    assert "cache" not in source.lower()
