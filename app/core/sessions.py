"""Per-visitor SQLite sandbox: each session_id gets its own copy of the
read-only template database under settings.session_dir, so writes made in
one browser tab are invisible to every other visitor. Render's free tier has
no background workers, so expiry is a lightweight sweep run inline on
request instead of a scheduled job.
"""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from app.config import settings

_last_sweep_at: float = 0.0
_SWEEP_INTERVAL_SECONDS = 60  # ponytail: fixed 60s throttle on the directory scan so a request
# burst on Render's 0.1 CPU box doesn't re-glob settings.session_dir on every single call;
# revisit if QP_MAX_SESSIONS needs to be much larger than a couple hundred.


def is_valid_session_id(session_id: str) -> bool:
    """Strict UUID4 check. session_id becomes a file path component
    (settings.session_dir / f"{session_id}.db"), so anything else — including
    a technically-valid-but-differently-formatted UUID string — is rejected
    rather than normalized, to keep the path construction trivially safe."""
    try:
        parsed = uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        return False
    return parsed.version == 4 and str(parsed) == session_id.lower()


def _session_path(session_id: str) -> Path:
    return settings.session_dir / f"{session_id}.db"


def get_session_db_path(session_id: str) -> Path:
    """Returns the path to session_id's sandbox copy, creating it from the
    template on first call. Call sites must validate session_id with
    is_valid_session_id() first — this function trusts its input."""
    settings.session_dir.mkdir(parents=True, exist_ok=True)
    path = _session_path(session_id)
    if not path.exists():
        shutil.copyfile(settings.template_db_path, path)
    else:
        path.touch()  # idle-TTL is measured from last access, not creation
    _maybe_sweep()
    return path


def reset_session(session_id: str) -> Path:
    path = _session_path(session_id)
    if path.exists():
        path.unlink()
    return get_session_db_path(session_id)


def sweep_expired_sessions() -> int:
    if not settings.session_dir.exists():
        return 0

    now = time.time()
    ttl_seconds = settings.session_ttl_minutes * 60
    files = sorted(settings.session_dir.glob("*.db"), key=lambda p: p.stat().st_mtime)

    removed = 0
    survivors = []
    for f in files:
        if now - f.stat().st_mtime > ttl_seconds:
            f.unlink(missing_ok=True)
            removed += 1
        else:
            survivors.append(f)

    overflow = len(survivors) - settings.max_sessions
    if overflow > 0:
        for f in survivors[:overflow]:  # oldest-mtime first, since `files` was sorted ascending
            f.unlink(missing_ok=True)
            removed += 1

    return removed


def _maybe_sweep() -> None:
    global _last_sweep_at
    now = time.time()
    if now - _last_sweep_at < _SWEEP_INTERVAL_SECONDS:
        return
    _last_sweep_at = now
    sweep_expired_sessions()
