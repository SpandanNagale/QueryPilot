"""Audit trail for every manipulation request: which session, what SQL, the
validation outcome, the preview shown, confirm/cancel, rows affected,
latency, retry count. Stored in the operational database (Neon in
production, local SQLite otherwise) instead of a JSONL file, so it survives
a Render free-tier spin-down and isn't lost with the ephemeral filesystem.
"""
from __future__ import annotations

from typing import Any

from app.core.models import AuditEvent
from app.core.opdb import get_session


def log_event(*, session_id: str, event: str, **fields: Any) -> None:
    with get_session() as session:
        session.add(AuditEvent(session_id=session_id, event=event, payload=fields))


def read_events(limit: int = 100, offset: int = 0, session_id: str | None = None) -> list[dict]:
    """Most-recent-first. Pass session_id to restrict to one session's
    events (the default for the public /audit endpoint); omit it only for
    the admin-token-gated full log."""
    with get_session() as session:
        query = session.query(AuditEvent).order_by(AuditEvent.timestamp.desc())
        if session_id is not None:
            query = query.filter(AuditEvent.session_id == session_id)
        rows = query.offset(offset).limit(limit).all()
        return [
            {
                "timestamp": row.timestamp.isoformat(),
                "session_id": row.session_id,
                "event": row.event,
                **row.payload,
            }
            for row in rows
        ]
