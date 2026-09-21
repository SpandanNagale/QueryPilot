"""Small terminal/short-circuit nodes that end a turn without touching SQL:
off-topic redirects, schema questions, clarifying questions, and honest
failure explanations."""
from __future__ import annotations

from app.agent.state import AgentState
from app.core.schema_store import full_schema_summary

OFF_TOPIC_RESPONSE = (
    "I'm QueryPilot - I can help with questions about products, retailers, orders, "
    "payments, and inventory in this cold-drinks distribution database. "
    "Could you ask something about that data?"
)


def polite_redirect_node(state: AgentState) -> dict:
    return {"final_response": OFF_TOPIC_RESPONSE}


def schema_question_node(state: AgentState) -> dict:
    summary = full_schema_summary()
    return {
        "final_response": (
            "Here's the database schema:\n\n" + summary
        )
    }


def ask_clarifying_question_node(state: AgentState) -> dict:
    question = state["ambiguity"].get("clarifying_question") or "Could you clarify what you mean?"
    return {"final_response": question, "awaiting_confirmation": False}


def error_response_node(state: AgentState) -> dict:
    reason = (state.get("client_error") or state.get("error") or "an unexpected error occurred").rstrip(". ")
    # Guardrail messages (e.g. the row-impact ceiling) already tell the user
    # exactly what to do next - don't tack on a generic "please rephrase"
    # that would just repeat the same ask in different words.
    if "please" in reason.lower():
        response = f"I couldn't do this: {reason}."
    else:
        response = (
            f"I wasn't able to do this after several attempts. Last issue: {reason}. "
            "Could you rephrase your request with more specific details?"
        )
    return {"final_response": response}
