"""Input guardrail node: the very first node in the graph (START -> guardrail
-> {blocked: error_response, intent_classifier: intent_classifier}).

Heuristic-only (see app/core/guardrail.py) -- no LLM call. Sets state["error"]
on a block so the existing free-text `error_response_node` path produces a
sentence; main.py's error categorization (`classify_reason_to_code`) maps a
"guardrail" reason to ErrorCode.GUARDRAIL_BLOCKED for the client-facing SSE
event.
"""
from __future__ import annotations

from app.agent.state import AgentState
from app.core.guardrail import check_input


def guardrail_node(state: AgentState) -> dict:
    verdict = check_input(state["user_input"])
    update: dict = {"guardrail_verdict": verdict.to_dict()}
    if verdict.blocked:
        update["error"] = f"Guardrail blocked: {verdict.reason}"
    return update


def route_after_guardrail(state: AgentState) -> str:
    if state["guardrail_verdict"]["blocked"]:
        return "blocked"
    return "intent_classifier"
