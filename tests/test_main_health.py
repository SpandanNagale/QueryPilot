# tests/test_main_health.py
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


def test_health_providers_endpoint_returns_check_providers_result():
    from app.main import app

    fake_result = {"sql_gen": [{"provider": "groq", "model": "x", "status": "ok", "detail": ""}], "chat": []}
    with patch("app.main.check_providers", return_value=fake_result):
        with TestClient(app) as client:
            response = client.get("/health/providers")
    assert response.status_code == 200
    assert response.json() == fake_result


def test_health_endpoint_no_longer_references_ollama():
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/health")
    body = response.json()
    assert "ollama" not in body
    assert "database" in body
