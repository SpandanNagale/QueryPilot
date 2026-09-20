from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.core import audit, opdb
from app.core.opdb import Base


def _use_tmp_db(tmp_path, monkeypatch):
    """Point app.core.opdb's module-level engine/SessionLocal singletons at a
    fresh tmp-file SQLite DB, so audit.log_event/read_events (and anything
    else going through opdb.get_session) never touch the real local
    data/operational.db."""
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path / 'op.db'}")
    test_engine = create_engine(settings.resolved_database_url)
    Base.metadata.create_all(test_engine)
    monkeypatch.setattr(opdb, "engine", test_engine)
    monkeypatch.setattr(opdb, "SessionLocal", sessionmaker(bind=test_engine, expire_on_commit=False))


def test_log_event_and_read_events_round_trip(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)

    session_id = str(uuid.uuid4())
    audit.log_event(session_id=session_id, event="manipulation_executed", row_count=3)
    events = audit.read_events(session_id=session_id)
    assert len(events) == 1
    assert events[0]["event"] == "manipulation_executed"
    assert events[0]["row_count"] == 3


def test_read_events_filters_by_session(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)

    id_a, id_b = str(uuid.uuid4()), str(uuid.uuid4())
    audit.log_event(session_id=id_a, event="e1")
    audit.log_event(session_id=id_b, event="e2")

    assert [e["event"] for e in audit.read_events(session_id=id_a)] == ["e1"]
    assert [e["event"] for e in audit.read_events(session_id=id_b)] == ["e2"]


def test_audit_endpoint_requires_session_id_or_admin_token(tmp_path, monkeypatch):
    _use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "admin_token", "secret")
    from app.main import app

    with TestClient(app) as client:
        no_auth = client.get("/audit")
        assert no_auth.status_code == 400

        with_session = client.get(f"/audit?session_id={uuid.uuid4()}")
        assert with_session.status_code == 200

        with_admin = client.get("/audit", headers={"X-Admin-Token": "secret"})
        assert with_admin.status_code == 200
