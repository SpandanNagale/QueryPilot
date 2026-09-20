# tests/test_main_errors.py
"""Error-shape wiring in app/main.py: _run_graph_stream's exception path
and node-error branch, and /health's internal-failure fallback. Both
_run_graph_stream tests call the generator function directly (monkeypatching
app.main._state["graph"]) rather than going through a full TestClient/
lifespan round trip -- it's the unit under test and doesn't need a real
graph or checkpointer.
"""
from __future__ import annotations

import app.main as main_module


class _ExplodingGraph:
    def stream(self, *args, **kwargs):
        raise RuntimeError("super secret leak: postgres://user:pass@host/db")


class _NodeErrorGraph:
    def stream(self, *args, **kwargs):
        yield "updates", {
            "validator": {"error": "This would affect 500 rows, which exceeds the safety limit of 50."}
        }


def test_run_graph_stream_exception_path_never_leaks_raw_exception_text(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _ExplodingGraph())
    events = list(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))
    body = "".join(events)

    assert "postgres://user:pass@host/db" not in body
    assert "secret" not in body
    assert '"code": "INTERNAL"' in body
    assert '"node": "server"' in body


def test_run_graph_stream_node_error_carries_classified_code(monkeypatch):
    monkeypatch.setitem(main_module._state, "graph", _NodeErrorGraph())
    events = list(main_module._run_graph_stream("sess-1", {}, {"user_input": "hi"}))
    body = "".join(events)

    assert '"code": "ROW_CEILING_EXCEEDED"' in body
    assert '"node": "validator"' in body
    # the curated reason text itself is still fine to surface (not a raw traceback)
    assert "exceeds the safety limit of 50" in body


def test_health_endpoint_never_leaks_traceback_on_internal_failure(monkeypatch):
    # health() is a plain function (FastAPI's decorator returns it unchanged)
    # -- call it directly rather than through TestClient/lifespan, since the
    # only thing under test here is the outer try/except around response
    # construction, not app startup.
    class _ExplodingSettings:
        @property
        def read_only_mode(self):
            raise RuntimeError("boom: unexpected internal detail")

    monkeypatch.setattr(main_module, "settings", _ExplodingSettings())
    body = main_module.health()

    assert body["healthy"] is False
    assert body["code"] == "INTERNAL"
    assert "boom" not in str(body)
