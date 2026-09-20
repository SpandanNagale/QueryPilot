"""The LangGraph agent definition.

See ARCHITECTURE in the project README for the full node diagram. Built up
incrementally: analytics path first (Phase 4), manipulation path added on
top (Phase 5) sharing the same intent_classifier -> schema_retriever ->
sql_generator -> validator -> ambiguity_check spine.
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.agent.nodes.ambiguity_check import ambiguity_check_node, route_after_ambiguity_check
from app.agent.nodes.analytics_executor import analytics_executor_node, route_after_analytics_execution
from app.agent.nodes.audit_logger import audit_logger_node
from app.agent.nodes.auto_limit_injector import auto_limit_injector_node
from app.agent.nodes.chart_detector import chart_detector_node
from app.agent.nodes.confirmation_gate import confirmation_gate_node, route_after_confirmation
from app.agent.nodes.dry_run_preview import dry_run_preview_node, route_after_dry_run_preview
from app.agent.nodes.followup_suggester import followup_suggester_node
from app.agent.nodes.guardrail import guardrail_node, route_after_guardrail
from app.agent.nodes.insight_summarizer import insight_summarizer_node
from app.agent.nodes.intent_classifier import intent_classifier_node
from app.agent.nodes.schema_retriever import schema_retriever_node
from app.agent.nodes.self_corrector import self_corrector_node
from app.agent.nodes.sql_generator import sql_generator_node
from app.agent.nodes.terminal_responses import (
    ask_clarifying_question_node,
    error_response_node,
    polite_redirect_node,
    schema_question_node,
)
from app.agent.nodes.transactional_executor import transactional_executor_node
from app.agent.nodes.validator_node import route_after_validation, validator_node
from app.agent.state import AgentState


def route_after_intent(state: AgentState) -> str:
    intent = state.get("intent")
    if intent == "off_topic":
        return "polite_redirect"
    if intent == "schema_question":
        return "schema_question_node"
    return "schema_retriever"


def build_graph(checkpointer=None):
    graph = StateGraph(AgentState)

    graph.add_node("guardrail", guardrail_node)
    graph.add_node("intent_classifier", intent_classifier_node)
    graph.add_node("polite_redirect", polite_redirect_node)
    graph.add_node("schema_question_node", schema_question_node)
    graph.add_node("schema_retriever", schema_retriever_node)
    graph.add_node("sql_generator", sql_generator_node)
    graph.add_node("validator", validator_node)
    graph.add_node("ambiguity_check", ambiguity_check_node)
    graph.add_node("ask_clarifying_question", ask_clarifying_question_node)
    graph.add_node("error_response", error_response_node)

    # analytics path
    graph.add_node("auto_limit_injector", auto_limit_injector_node)
    graph.add_node("analytics_executor", analytics_executor_node)
    graph.add_node("self_corrector", self_corrector_node)
    graph.add_node("insight_summarizer", insight_summarizer_node)
    graph.add_node("chart_detector", chart_detector_node)
    graph.add_node("followup_suggester", followup_suggester_node)

    # manipulation path
    graph.add_node("dry_run_preview", dry_run_preview_node)
    graph.add_node("confirmation_gate", confirmation_gate_node)
    graph.add_node("transactional_executor", transactional_executor_node)
    graph.add_node("audit_logger", audit_logger_node)

    graph.add_edge(START, "guardrail")
    graph.add_conditional_edges(
        "guardrail",
        route_after_guardrail,
        {"blocked": "error_response", "intent_classifier": "intent_classifier"},
    )
    graph.add_conditional_edges(
        "intent_classifier",
        route_after_intent,
        {
            "polite_redirect": "polite_redirect",
            "schema_question_node": "schema_question_node",
            "schema_retriever": "schema_retriever",
        },
    )
    graph.add_edge("polite_redirect", END)
    graph.add_edge("schema_question_node", END)

    graph.add_edge("schema_retriever", "sql_generator")
    graph.add_edge("sql_generator", "validator")
    graph.add_conditional_edges(
        "validator",
        route_after_validation,
        {"sql_generator": "sql_generator", "ambiguity_check": "ambiguity_check", "error_response": "error_response"},
    )
    graph.add_conditional_edges(
        "ambiguity_check",
        route_after_ambiguity_check,
        {
            "ask_clarifying_question": "ask_clarifying_question",
            "dry_run_preview": "dry_run_preview",
            "auto_limit_injector": "auto_limit_injector",
        },
    )
    graph.add_edge("ask_clarifying_question", END)
    graph.add_edge("error_response", END)

    # analytics path
    graph.add_edge("auto_limit_injector", "analytics_executor")
    graph.add_conditional_edges(
        "analytics_executor",
        route_after_analytics_execution,
        {
            "insight_summarizer": "insight_summarizer",
            "self_corrector": "self_corrector",
            "error_response": "error_response",
        },
    )
    graph.add_edge("self_corrector", "analytics_executor")
    graph.add_edge("insight_summarizer", "chart_detector")
    graph.add_edge("chart_detector", "followup_suggester")
    graph.add_edge("followup_suggester", END)

    # manipulation path
    graph.add_conditional_edges(
        "dry_run_preview",
        route_after_dry_run_preview,
        {"confirmation_gate": "confirmation_gate", "error_response": "error_response"},
    )
    graph.add_conditional_edges(
        "confirmation_gate",
        route_after_confirmation,
        {"transactional_executor": "transactional_executor", "cancelled": END},
    )
    graph.add_edge("transactional_executor", "audit_logger")
    graph.add_edge("audit_logger", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
