"""Расширенные настройки, логи, сброс резюме."""

import pytest
from fastapi.testclient import TestClient

from intern_agent import config, db
from intern_agent.api.app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_settings_providers_and_masking(client):
    resp = client.put(
        "/api/settings",
        json={"llm_provider": "openai", "llm_api_key": "sk-test-12345678", "llm_model": ""},
    )
    assert resp.status_code == 200
    data = client.get("/api/settings").json()
    assert data["llm_provider"] == "openai"
    assert data["llm_api_key"] == "…5678"  # ключ не возвращается целиком
    assert "sk-test" not in str(data)

    assert client.put("/api/settings", json={"llm_provider": "skynet"}).status_code == 400
    assert client.put("/api/settings", json={"auto_scan_hours": 6}).status_code == 200
    assert client.get("/api/settings").json()["auto_scan_hours"] == 6


def test_logs_endpoint(client):
    conn = db.get_conn()
    try:
        db.add_log(conn, "warn", "scan", "тестовое предупреждение")
    finally:
        conn.close()
    items = client.get("/api/logs").json()["items"]
    assert any(it["source"] == "scan" and it["level"] == "warn" for it in items)


def test_resume_delete(client):
    resume = "Иван Иванов, python-стажёр. Опыт: pet-проекты на FastAPI и SQLite. " * 3
    assert client.put("/api/resume", json={"content": resume}).status_code == 200
    assert client.get("/api/resume").json()["has_resume"] is True

    assert client.delete("/api/resume").status_code == 200
    assert client.get("/api/resume").json()["has_resume"] is False
