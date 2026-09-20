"""Daily USD spend tracking: one BudgetEntry row per priced LLM attempt,
attributed to a session via app.core.request_context's contextvar rather
than a session_id parameter threaded through app.core.llm's chat()/
structured_chat()/stream_chat(). record_llm_attempt is registered once at
startup as an llm_attempt listener (app/main.py's lifespan); today_spend_usd
is the single place that sums BudgetEntry rows, shared by GET /health/budget
and /chat's cap check so the "sum today's rows" query isn't duplicated.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func

from app.core.models import BudgetEntry
from app.core.opdb import get_session
from app.core.request_context import get_session_id


def record_llm_attempt(event: dict) -> None:
    """llm_attempt listener. Skips events with no cost (a failed attempt --
    estimate_cost only runs on success, see app/core/llm.py::_record_attempt)
    and events with no session in context (e.g. the startup cache pre-warm
    thread, which calls node functions directly rather than through
    app.main's request handling)."""
    cost = event.get("cost_usd")
    if cost is None:
        return
    session_id = get_session_id()
    if session_id is None:
        return
    with get_session() as session:
        session.add(
            BudgetEntry(
                session_id=session_id,
                provider=event.get("provider") or "",
                model=event.get("model") or "",
                cost_usd=cost,
            )
        )


def _start_of_today_utc() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def today_spend_usd() -> float:
    with get_session() as session:
        total = (
            session.query(func.coalesce(func.sum(BudgetEntry.cost_usd), 0.0))
            .filter(BudgetEntry.timestamp >= _start_of_today_utc())
            .scalar()
        )
    return float(total)
