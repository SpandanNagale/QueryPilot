"""Per-request session_id, read by app/core/budget.py's llm_attempt listener
so it can attribute LLM cost to a session without app/core/llm.py's
chat()/structured_chat()/stream_chat() -- which only know an alias, never a
session -- gaining a session_id parameter threaded through every node.

Deliberately no reset()/Token here. See app/main.py::_run_graph_stream for
why: Starlette drives a sync StreamingResponse body generator through
anyio's threadpool, dispatching each next() call in its own copy of the
ambient context, so a Token minted in one dispatch can't be reset from
another (verified empirically -- raises "Token ... was created in a
different Context"). A plain set() has no such restriction and is cheap
enough to call on every dispatch.
"""
from __future__ import annotations

import contextvars

_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("qp_session_id", default=None)


def set_session_id(session_id: str | None) -> None:
    _session_id.set(session_id)


def get_session_id() -> str | None:
    return _session_id.get()


def clear_session_id() -> None:
    """Test-only: reset to the unset default between tests (pytest runs
    sync tests in one shared context, not a fresh one per test)."""
    _session_id.set(None)
