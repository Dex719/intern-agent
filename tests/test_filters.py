"""Фильтры поиска: параметры hh, настройки, отсев компаний при скане."""

import httpx
import pytest
from fastapi.testclient import TestClient

from intern_agent import config, hh, services
from intern_agent.api.app import app

# ---------- hh: параметры запроса ----------


def test_filter_params_empty():
    assert hh._filter_params(None) == {}
    assert hh._filter_params({"min_salary": 0, "remote_only": False}) == {}


def test_filter_params_salary_and_remote():
    params = hh._filter_params({"min_salary": 250000, "remote_only": True})
    assert params["salary"] == 250000
    assert params["only_with_salary"] == "true"
    assert params["schedule"] == "remote"
    assert "work_format" not in params
    assert hh._filter_params({"remote_only": True}, html=True)["work_format"] == "REMOTE"


def test_search_api_passes_filters(monkeypatch):
    captured = {}

    def fake_get(url, params=None, **kwargs):
        captured.update(params)
        request = httpx.Request("GET", url)
        return httpx.Response(200, json={"items": [{"id": "1"}]}, request=request)

    monkeypatch.setattr(httpx, "get", fake_get)
    ids = hh.search_vacancies_api("python intern", "160", {"min_salary": 200000, "remote_only": True})
    assert ids == ["1"]
    assert captured["salary"] == 200000
    assert captured["only_with_salary"] == "true"
    assert captured["schedule"] == "remote"


# ---------- services: хелперы ----------


def test_search_filters_and_excluded():
    settings = {
        "filter_min_salary": "300000",
        "filter_remote_only": "1",
        "filter_exclude_companies": "Kaspi, EPAM Systems,  ,БЦК",
    }
    assert services.search_filters(settings) == {"min_salary": 300000, "remote_only": True}
    excluded = services.excluded_companies(settings)
    assert excluded == ["kaspi", "epam systems", "бцк"]
    assert services.is_company_excluded("EPAM Systems Kazakhstan", excluded)
    assert services.is_company_excluded("ТОО Kaspi.kz", excluded)
    assert not services.is_company_excluded("Kolesa Group", excluded)
    assert not services.is_company_excluded(None, excluded)


def test_search_filters_defaults():
    assert services.search_filters({}) == {"min_salary": 0, "remote_only": False}
    assert services.excluded_companies({}) == []


# ---------- API: сохранение и выдача настроек ----------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    with TestClient(app) as test_client:
        yield test_client


def test_settings_filters_roundtrip(client):
    resp = client.put(
        "/api/settings",
        json={
            "filter_min_salary": 250000,
            "filter_remote_only": True,
            "filter_exclude_companies": "Kaspi, EPAM",
        },
    )
    assert resp.status_code == 200
    data = client.get("/api/settings").json()
    assert data["filter_min_salary"] == 250000
    assert data["filter_remote_only"] is True
    assert data["filter_exclude_companies"] == "Kaspi, EPAM"

    # сброс фильтров
    resp = client.put(
        "/api/settings",
        json={"filter_min_salary": 0, "filter_remote_only": False, "filter_exclude_companies": ""},
    )
    assert resp.status_code == 200
    data = client.get("/api/settings").json()
    assert data["filter_min_salary"] == 0
    assert data["filter_remote_only"] is False
    assert data["filter_exclude_companies"] == ""


def test_settings_filters_validation(client):
    assert client.put("/api/settings", json={"filter_min_salary": -5}).status_code == 422
    assert client.put("/api/settings", json={"filter_exclude_companies": "x" * 501}).status_code == 422


def test_settings_exclude_logos_roundtrip(client):
    logos = '{"Kaspi.kz": "https://img.hhcdn.ru/employer-logo/1.png"}'
    resp = client.put("/api/settings", json={"filter_exclude_logos": logos})
    assert resp.status_code == 200
    assert client.get("/api/settings").json()["filter_exclude_logos"] == logos
    # сброс
    assert client.put("/api/settings", json={"filter_exclude_logos": ""}).status_code == 200
    assert client.get("/api/settings").json()["filter_exclude_logos"] == ""


def test_settings_exclude_logos_validation(client):
    assert client.put("/api/settings", json={"filter_exclude_logos": "not json"}).status_code == 422
    assert client.put("/api/settings", json={"filter_exclude_logos": "[1,2]"}).status_code == 422
    assert client.put("/api/settings", json={"filter_exclude_logos": '{"a": 1}'}).status_code == 422
    assert client.put("/api/settings", json={"filter_exclude_logos": "x" * 4001}).status_code == 422
