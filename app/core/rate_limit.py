"""Per-IP sliding-window rate limit for `/chat` and `/confirm`.

In-memory only, stdlib-only (`collections.deque` + `time`) -- no new
dependency. Each IP gets a deque of its request timestamps; a check prunes
entries older than the 60s window before counting, giving a true sliding
window rather than a fixed-window counter's edge-of-window burst.
"""
from __future__ import annotations

import time
from collections import deque

from app.config import settings

_WINDOW_SECONDS = 60.0

# ponytail: this dict grows one entry per distinct IP ever seen and nothing
# ever removes a key, so it grows unboundedly over a long-running process
# with many distinct IPs. Fine for a single free-tier demo instance's
# traffic within its 15-minute spin-down window; upgrade path is evicting
# IPs with no recent requests if this ever needs to run long-lived at high
# IP cardinality.
_requests: dict[str, deque[float]] = {}


def check_rate_limit(client_ip: str) -> bool:
    """True if `client_ip` may proceed; False if it has already made
    `settings.rate_limit_per_minute` requests in the trailing 60s window.
    A call that returns True also records itself, so checking and counting
    happen atomically -- there's no separate "record" step to forget."""
    now = time.time()
    bucket = _requests.setdefault(client_ip, deque())
    while bucket and now - bucket[0] > _WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= settings.rate_limit_per_minute:
        return False
    bucket.append(now)
    return True


if __name__ == "__main__":
    # ponytail: smallest runnable check for this module's actual logic
    # (window enforcement, per-IP isolation) -- no test framework needed.
    _requests.clear()
    settings.rate_limit_per_minute = 2
    assert check_rate_limit("1.1.1.1") is True
    assert check_rate_limit("1.1.1.1") is True
    assert check_rate_limit("1.1.1.1") is False  # 3rd request in-window blocked
    assert check_rate_limit("2.2.2.2") is True  # different IP unaffected
    _requests["1.1.1.1"][0] -= 61  # simulate the oldest entry aging out of the window
    assert check_rate_limit("1.1.1.1") is True
    print("ok")
