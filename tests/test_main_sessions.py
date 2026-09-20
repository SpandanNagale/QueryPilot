from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.config import settings


def test_chat_rejects_non_uuid4_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/chat", json={"session_id": "../../etc/passwd", "message": "hi"})
    assert response.status_code == 400
    assert response.json()["detail"] == {"code": "SESSION_INVALID", "message": "session_id must be a valid UUID4."}


def test_session_reset_rejects_non_uuid4():
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/session/reset", json={"session_id": "not-a-uuid"})
    assert response.status_code == 400
    assert response.json()["detail"] == {"code": "SESSION_INVALID", "message": "session_id must be a valid UUID4."}


def test_tables_endpoint_falls_back_to_template_with_no_session_id():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/tables")
    assert response.status_code == 200
    assert len(response.json()["tables"]) > 0


def test_tables_endpoint_reflects_session_specific_deletes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.core import sessions
    from app.core.db import get_engines
    from sqlalchemy import text

    session_id = str(uuid.uuid4())
    path = sessions.get_session_db_path(session_id)
    _, rw = get_engines(str(path))
    with rw.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    from app.main import app

    with TestClient(app) as client:
        response = client.get(f"/tables/orders/preview?session_id={session_id}")
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["rows"]]
    assert 1 not in ids

    with TestClient(app) as client:
        response = client.get("/tables/orders/preview")  # no session_id -> template, unaffected
    ids = [row["id"] for row in response.json()["rows"]]
    assert 1 in ids


def test_session_reset_restores_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.core import sessions
    from app.core.db import get_engines
    from sqlalchemy import text

    session_id = str(uuid.uuid4())
    path = sessions.get_session_db_path(session_id)
    _, rw = get_engines(str(path))
    with rw.connect() as conn:
        with conn.begin():
            conn.execute(text("DELETE FROM orders WHERE id = 1"))

    from app.main import app

    with TestClient(app) as client:
        reset_response = client.post("/session/reset", json={"session_id": session_id})
        assert reset_response.status_code == 200
        preview_response = client.get(f"/tables/orders/preview?session_id={session_id}")
    ids = [row["id"] for row in preview_response.json()["rows"]]
    assert 1 in ids
