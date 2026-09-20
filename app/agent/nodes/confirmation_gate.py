"""Pauses the graph via LangGraph's interrupt() until the user responds.
The API layer resumes with Command(resume=<user's next message>) — see
app/main.py's /confirm endpoint. Any answer other than an explicit "yes"
cancels, per the spec: "anything else cancels and is treated as a new
request."
"""
from __future__ import annotations

import time

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from app.agent.state import AgentState
from app.core.audit import log_event

_AFFIRMATIVE = {"yes", "y", "confirm", "confirmed", "true", "ok", "okay", "go", "do it"}


def _interpret_answer(answer) -> bool:
    if isinstance(answer, bool):
        return answer
    if isinstance(answer, str):
        return answer.strip().lower() in _AFFIRMATIVE
    return False


def confirmation_gate_node(state: AgentState, config: RunnableConfig) -> dict:
    answer = interrupt(
        {
            "type": "confirmation_request",
            "preview": state["preview"],
            "generated_sql": state["generated_sql"],
            "message": "Please confirm to proceed (yes/no). Anything other than yes cancels.",
        }
    )
    confirmed = _interpret_answer(answer)
    update: dict = {"awaiting_confirmation": False, "confirmed": confirmed}

    if not confirmed:
        is_timeout = answer == "timeout"
        update["final_response"] = (
            "This confirmation request expired - no changes were made. Please ask again if you still want this."
            if is_timeout
            else "Cancelled - no changes were made."
        )
        started_at = state.get("manipulation_started_at")
        log_event(
            event="manipulation_timed_out" if is_timeout else "manipulation_cancelled",
            session_id=config["configurable"]["thread_id"],
            user_input=state["user_input"],
            generated_sql=state["generated_sql"],
            preview=state.get("preview"),
            latency_seconds=round(time.time() - started_at, 3) if started_at else None,
        )

    return update


def route_after_confirmation(state: AgentState) -> str:
    return "transactional_executor" if state.get("confirmed") else "cancelled"
