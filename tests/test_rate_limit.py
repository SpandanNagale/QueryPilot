"""Per-IP rate limiting: app/core/rate_limit.py's sliding window, and its
wiring into /chat and /confirm in app/main.py.

Endpoint-wiring tests call chat()/confirm() directly (FastAPI's decorator
returns the function unchanged, same pattern as tests/test_main_errors.py)
with a hand-built starlette Request rather than a full TestClient/lifespan
round trip, and never consume the returned StreamingResponse's body -- a
call that doesn't raise HTTPException is enough to prove the rate-limit
check let it through, with no need for a real graph.

Each test uses IPs not used by any other test, since app.core.rate_limit's
_requests dict is process-global module state with no per-test reset.
"""
from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.main as main_module
from app.config import settings
from app.core import rate_limit


def _fake_clock(monkeypatch, start: float = 1_000_000.0) -> dict:
    """Deterministic clock for rate_limit.time.time() -- avoids real
    sleep()s in the test. monkeypatch auto-restores the real time.time
    after the test, so this doesn't leak into other tests."""
    state = {"now": start}
    monkeypatch.setattr(rate_limit.time, "time", lambda: state["now"])
    return state


def _make_request(headers: dict[str, str] | None = None, client_host: str | None = "0.0.0.0") -> Request:
    scope = {
        "type": "http",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (client_host, 12345) if client_host else None,
    }
    return Request(scope)


# --- app/core/rate_limit.py unit tests ---


def test_allows_up_to_the_limit_then_blocks_the_nplus1th(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 3)
    _fake_clock(monkeypatch)
    ip = "10.0.0.1"
    assert rate_limit.check_rate_limit(ip)
    assert rate_limit.check_rate_limit(ip)
    assert rate_limit.check_rate_limit(ip)
    assert not rate_limit.check_rate_limit(ip)  # 4th request in the same window


def test_different_ip_is_unaffected(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    _fake_clock(monkeypatch)
    assert rate_limit.check_rate_limit("10.0.0.2")
    assert not rate_limit.check_rate_limit("10.0.0.2")
    assert rate_limit.check_rate_limit("10.0.0.3")  # separate bucket, unaffected


def test_requests_outside_the_window_succeed_again(monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    clock = _fake_clock(monkeypatch)
    ip = "10.0.0.4"
    assert rate_limit.check_rate_limit(ip)
    assert not rate_limit.check_rate_limit(ip)
    clock["now"] += 61  # past the 60s sliding window
    assert rate_limit.check_rate_limit(ip)


# --- app/main.py wiring tests ---


def test_chat_blocks_nplus1th_request_from_same_ip(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    _fake_clock(monkeypatch)
    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")

    main_module.chat(req, _make_request(client_host="10.0.1.1"))
    main_module.chat(req, _make_request(client_host="10.0.1.1"))
    with pytest.raises(HTTPException) as exc_info:
        main_module.chat(req, _make_request(client_host="10.0.1.1"))
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "RATE_LIMITED"


def test_confirm_blocks_nplus1th_request_from_same_ip(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    _fake_clock(monkeypatch)
    # No pending checkpoint for this session, so a request that clears the
    # rate limit still hits the pre-existing 409 SESSION_EXPIRED -- proves
    # the rate-limit check runs first without masking that later check.
    req = main_module.ConfirmRequest(session_id=str(uuid.uuid4()), confirmed=True)
    monkeypatch.setitem(main_module._state, "graph", _NoNextGraph())

    with pytest.raises(HTTPException) as first:
        main_module.confirm(req, _make_request(client_host="10.0.1.2"))
    assert first.value.status_code == 409  # rate limit passed; session-expired check fired next

    with pytest.raises(HTTPException) as second:
        main_module.confirm(req, _make_request(client_host="10.0.1.2"))
    assert second.value.status_code == 429
    assert second.value.detail["code"] == "RATE_LIMITED"


class _NoNextGraph:
    def get_state(self, config):
        class _State:
            next = ()

        return _State()


def test_a_different_ip_is_not_blocked_by_the_first(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    _fake_clock(monkeypatch)
    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")

    main_module.chat(req, _make_request(client_host="10.0.1.3"))
    with pytest.raises(HTTPException):
        main_module.chat(req, _make_request(client_host="10.0.1.3"))
    # A different IP still has its own untouched bucket.
    main_module.chat(req, _make_request(client_host="10.0.1.4"))


def test_x_forwarded_for_last_entry_is_used(monkeypatch, tmp_path):
    """Render's proxy appends exactly one hop -- the LAST entry is the one
    Render itself appended, and the only trustworthy value. Earlier entries
    are caller-supplied. Same rightmost (real) entry, different leftmost
    (spoofable) entry -- must be treated as the SAME caller."""
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    _fake_clock(monkeypatch)
    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")

    main_module.chat(
        req, _make_request(headers={"X-Forwarded-For": "10.0.1.5, 172.16.0.1"}, client_host="172.16.0.1")
    )
    with pytest.raises(HTTPException) as exc_info:
        # A different (spoofed) leftmost entry, same rightmost (real) hop --
        # must still hit the same bucket, not get a fresh one.
        main_module.chat(
            req, _make_request(headers={"X-Forwarded-For": "9.9.9.9, 172.16.0.1"}, client_host="172.16.0.1")
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "RATE_LIMITED"

    # A genuinely different rightmost (real) hop is a different caller.
    main_module.chat(
        req, _make_request(headers={"X-Forwarded-For": "10.0.1.6, 172.16.0.2"}, client_host="172.16.0.2")
    )


def test_x_forwarded_for_spoofed_leftmost_entry_still_gets_rate_limited(monkeypatch, tmp_path):
    """The actual security property being restored: a client sending a
    fresh, different spoofed leftmost entry on EVERY request must not get a
    fresh rate-limit bucket each time -- the rightmost (proxy-appended)
    entry stays constant, so the bucket is shared correctly."""
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 2)
    _fake_clock(monkeypatch)
    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")

    for i in range(2):
        main_module.chat(
            req,
            _make_request(
                headers={"X-Forwarded-For": f"spoofed-{i}-{uuid.uuid4()}, 172.16.0.9"}, client_host="172.16.0.9"
            ),
        )
    with pytest.raises(HTTPException) as exc_info:
        main_module.chat(
            req,
            _make_request(
                headers={"X-Forwarded-For": f"spoofed-final-{uuid.uuid4()}, 172.16.0.9"}, client_host="172.16.0.9"
            ),
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "RATE_LIMITED"


def test_missing_x_forwarded_for_falls_back_to_client_host(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    monkeypatch.setattr(settings, "rate_limit_per_minute", 1)
    _fake_clock(monkeypatch)
    req = main_module.ChatRequest(session_id=str(uuid.uuid4()), message="hi")

    # No X-Forwarded-For header at all -- must still work via request.client.host.
    main_module.chat(req, _make_request(headers=None, client_host="10.0.1.7"))
    with pytest.raises(HTTPException) as exc_info:
        main_module.chat(req, _make_request(headers=None, client_host="10.0.1.7"))
    assert exc_info.value.status_code == 429
