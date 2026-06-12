"""Лента вакансий: поиск, скрининг, эндпоинты."""

import pytest
from fastapi.testclient import TestClient

from intern_agent import config, hh, llm
from intern_agent.api.app import app

RESUME = "Бауыржан, студент 2 курса. Python, SQL, FastAPI. Пет-проекты: ML-оценщик квартир." * 2

SEARCH_HTML = """
<a data-qa="serp-item__title serp-item__title-link" href="https://hh.kz/vacancy/111?from=search">A</a>
<a data-qa="serp-item__title" href="/vacancy/222?query=python">B</a>
<a data-qa="serp-item__title" href="https://hh.kz/vacancy/111?dup=1">A dup</a>
"""

ANALYSIS = {
    "company": "Kolesa Group",
    "position": "Python Developer (Intern)",
    "match_score": 68,
    "verdict": "Стоит откликнуться.",
    "matched": ["Python"],
    "missing": ["Docker"],
    "recommendations": ["Подтянуть Docker"],
    "tailored_resume": "О себе…",
    "cover_letter_ru": "Здравствуйте!",
    "cover_letter_en": "Hello!",
}


def test_search_html_regex_dedup():
    ids = hh.SEARCH_ITEM_RE.findall(SEARCH_HTML)
    assert ids == ["111", "222", "111"]
    seen = []
    for vacancy_id in ids:
        if vacancy_id not in seen:
            seen.append(vacancy_id)
    assert seen == ["111", "222"]


def test_parse_screen_response_clamps_and_skips():
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": '[{"id": 111, "score": 150, "reason": "ок"},'
                            ' {"score": 10}, {"id": "222", "score": -5, "reason": "нет"}]'
                        }
                    ]
                }
            }
        ]
    }
    items = llm.parse_screen_response(payload)
    assert items == [
        {"id": "111", "score": 100, "reason": "ок"},
        {"id": "222", "score": 0, "reason": "нет"},
    ]


def test_parse_screen_response_empty():
    with pytest.raises(llm.LLMError):
        llm.parse_screen_response({"candidates": [{"content": {"parts": [{"text": "[]"}]}}]})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    monkeypatch.setattr(
        "intern_agent.api.app.hh.search_vacancies", lambda query, area=None: ["111", "222"]
    )
    monkeypatch.setattr(
        "intern_agent.api.app.hh.fetch_vacancy",
        lambda vacancy_id: {
            "name": f"Вакансия {vacancy_id}",
            "alternate_url": f"https://hh.kz/vacancy/{vacancy_id}",
            "employer": {"name": "Kolesa Group"},
            "area": {"name": "Алматы"},
            "description": "<p>Ищем стажёра: Python, SQL</p>",
        },
    )

    async def fake_screen(resume, vacancies, settings=None):
        return [{"id": v["id"], "score": 75, "reason": "Хорошее совпадение."} for v in vacancies]

    async def fake_analyze(resume, vacancy, settings=None):
        return dict(ANALYSIS)

    monkeypatch.setattr("intern_agent.api.app.llm.screen_batch", fake_screen)
    monkeypatch.setattr("intern_agent.api.app.llm.analyze", fake_analyze)
    with TestClient(app) as test_client:
        test_client.put("/api/resume", json={"content": RESUME})
        yield test_client


def test_settings_roundtrip(client):
    assert client.get("/api/settings").json()["queries"] == config.DEFAULT_SEARCH_QUERIES
    resp = client.put("/api/settings", json={"queries": ["  django intern  ", ""]})
    assert resp.status_code == 200
    assert client.get("/api/settings").json()["queries"] == ["django intern"]


def test_scan_fills_feed_and_dedups(client):
    resp = client.post("/api/scan")
    assert resp.status_code == 200
    data = resp.json()
    assert data["added"] == 2
    assert {it["vacancy_id"] for it in data["items"]} == {"111", "222"}
    assert all(it["score"] == 75 for it in data["items"])

    # повторный скан не добавляет дубликаты
    assert client.post("/api/scan").json()["added"] == 0
    assert len(client.get("/api/feed").json()["items"]) == 2


def test_feed_ignore_and_apply(client):
    client.post("/api/scan")
    items = client.get("/api/feed").json()["items"]

    ignored = items[0]
    assert client.patch(
        f"/api/feed/{ignored['id']}", json={"status": "ignored"}
    ).status_code == 200
    assert len(client.get("/api/feed").json()["items"]) == 1
    assert client.get("/api/feed", params={"status": "ignored"}).json()["items"]

    applied = items[1]
    resp = client.post(f"/api/feed/{applied['id']}/apply")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "applied"
    assert body["cover_letter_ru"]
    # вакансия ушла из ленты, отклик появился в трекере
    assert not client.get("/api/feed").json()["items"]
    assert client.get("/api/applications").json()["stats"]["applied"] == 1


def test_feed_bad_status(client):
    assert client.patch("/api/feed/1", json={"status": "враньё"}).status_code == 400
    assert client.get("/api/feed", params={"status": "враньё"}).status_code == 400


def test_apply_missing_item(client):
    assert client.post("/api/feed/999/apply").status_code == 404
