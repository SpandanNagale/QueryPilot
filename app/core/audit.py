"""Append-only JSONL audit log for every manipulation request: timestamp,
user input, generated SQL, validation outcome, preview shown, whether the
user confirmed or cancelled, rows affected, latency, and retry count.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import settings

_lock = threading.Lock()


def log_event(**fields: Any) -> None:
    settings.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **fields}
    line = json.dumps(record, default=str)
    with _lock:
        with open(settings.audit_log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def read_events(limit: int = 100, offset: int = 0, session_id: str | None = None) -> list[dict]:
    """Most-recent-first. Pass session_id to restrict to one session's events
    (the default for the public /audit endpoint); omit it only for the
    admin-token-gated full log."""
    if not settings.audit_log_path.exists():
        return []
    with open(settings.audit_log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines = list(reversed(lines))
    if session_id is not None:
        lines = [line for line in lines if json.loads(line).get("session_id") == session_id]
    selected = lines[offset : offset + limit]
    return [json.loads(line) for line in selected if line.strip()]
