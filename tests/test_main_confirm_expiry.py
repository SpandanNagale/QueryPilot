from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.config import settings


def test_confirm_with_no_pending_checkpoint_returns_session_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "session_dir", tmp_path)
    from app.main import app

    with TestClient(app) as client:
        response = client.post("/confirm", json={"session_id": str(uuid.uuid4()), "confirmed": True})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "SESSION_EXPIRED"
