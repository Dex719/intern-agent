"""Аутентификация, расширенные настройки, логи."""

import pytest
from fastapi.testclient import TestClient

from intern_agent import auth, config
from intern_agent.api.app import app

PASSWORD = "super-secret-1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_password_hash_roundtrip():
    stored = auth.hash_password("hunter2-hunter2")
    assert auth.verify_password("hunter2-hunter2", stored)
    assert not auth.verify_password("wrong-password", stored)
    assert not auth.verify_password("hunter2-hunter2", "garbage")


def test_open_mode_then_setup_locks_api(client):
    state = client.get("/api/auth/state").json()
    assert state == {"password_set": False, "authed": True}
    # до установки пароля API открыт (первый запуск)
    assert client.get("/api/feed").status_code == 200

    assert client.post("/api/auth/setup", json={"password": PASSWORD}).status_code == 200
    assert auth.COOKIE_NAME in client.cookies
    # повторная установка запрещена
    assert client.post("/api/auth/setup", json={"password": "another-pass-1"}).status_code == 403

    # с кукой — доступ есть, без куки — 401
    assert client.get("/api/feed").status_code == 200
    client.cookies.clear()
    assert client.get("/api/feed").status_code == 401
    assert client.get("/api/health").status_code == 200  # публичный


def test_login_logout(client):
    client.post("/api/auth/setup", json={"password": PASSWORD})
    client.cookies.clear()

    assert client.post("/api/auth/login", json={"password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/feed").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/feed").status_code == 401


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


def test_logs_endpoint_records_auth_warnings(client):
    client.post("/api/auth/setup", json={"password": PASSWORD})
    client.cookies.clear()
    client.post("/api/auth/login", json={"password": "wrong-password"})
    client.post("/api/auth/login", json={"password": PASSWORD})

    items = client.get("/api/logs").json()["items"]
    assert any(it["source"] == "auth" and it["level"] == "warn" for it in items)
