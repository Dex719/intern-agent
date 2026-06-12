"""Привязка hh: OAuth-URL, токены, эндпоинты, автоотклик."""

import time

import pytest
from fastapi.testclient import TestClient

from intern_agent import config, db, hh_account, services
from intern_agent.api.app import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_auth_url_contains_params():
    url = hh_account.auth_url("my-id", "https://x.kz/hh/callback", "st4te")
    assert url.startswith("https://hh.ru/oauth/authorize?")
    assert "client_id=my-id" in url
    assert "state=st4te" in url
    assert "redirect_uri=https%3A%2F%2Fx.kz%2Fhh%2Fcallback" in url


def test_token_expired():
    assert hh_account.token_expired("")
    assert hh_account.token_expired("not-a-number")
    assert hh_account.token_expired(str(int(time.time()) + 10))  # внутри margin
    assert not hh_account.token_expired(str(int(time.time()) + 3600))


def test_connect_requires_credentials(client):
    resp = client.get("/api/hh/connect")
    assert resp.status_code == 400


def test_connect_returns_auth_url(client):
    client.put("/api/settings", json={"hh_client_id": "cid", "hh_client_secret": "sec"})
    resp = client.get("/api/hh/connect")
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://hh.ru/oauth/authorize?")
    # state сохранился для проверки callback
    conn = db.get_conn()
    try:
        assert db.get_setting(conn, "hh_oauth_state")
    finally:
        conn.close()


def test_callback_rejects_bad_state(client):
    resp = client.get("/hh/callback?code=abc&state=wrong", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert "hh=error" in resp.headers["location"]


def test_settings_expose_hh_state_and_mask_secret(client):
    client.put("/api/settings", json={
        "hh_client_id": "cid", "hh_client_secret": "secret-value",
        "auto_apply_enabled": True, "auto_apply_min_score": 80,
    })
    data = client.get("/api/settings").json()
    assert data["hh_linked"] is False
    assert data["hh_client_secret"] == "…alue"
    assert data["auto_apply_enabled"] is True
    assert data["auto_apply_min_score"] == 80


def test_auto_apply_skipped_when_disabled(client, monkeypatch):
    conn = db.get_conn()
    try:
        called = False

        async def fake_analyze(*args, **kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(services.llm, "analyze", fake_analyze)
        import asyncio

        result = asyncio.get_event_loop().run_until_complete(
            services.auto_apply_new_items(conn, [{"score": 99, "vacancy_id": "1"}])
        )
        assert result == []
        assert not called
    finally:
        conn.close()
