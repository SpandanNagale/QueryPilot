"""Node lifecycle states for the `node_state` SSE event (Phase 4). Mirrors
the sibling Crewai-Research-pipeline repo's observability/node_state.py.

pending -> running -> succeeded | degraded | failed | not_reached | skipped_cached
"""
from __future__ import annotations

from enum import Enum


class NodeState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    FAILED = "failed"
    NOT_REACHED = "not_reached"
    SKIPPED_CACHED = "skipped_cached"
