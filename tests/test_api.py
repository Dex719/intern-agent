import pytest
from fastapi.testclient import TestClient

from intern_agent import config, hh
from intern_agent.api.app import app

RESUME = "Бауыржан, студент 2 курса. Python, SQL, FastAPI. Пет-проекты: ML-оценщик квартир." * 2

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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")

    async def fake_analyze(resume, vacancy, settings=None):
        return dict(ANALYSIS)

    monkeypatch.setattr("intern_agent.api.app.llm.analyze", fake_analyze)
    monkeypatch.setattr(
        "intern_agent.api.app.hh.fetch_vacancy",
        lambda vacancy_id: {
            "name": "Python Developer (Intern)",
            "alternate_url": f"https://hh.kz/vacancy/{vacancy_id}",
            "employer": {"name": "Kolesa Group"},
            "area": {"name": "Алматы"},
            "description": "<p>Ищем стажёра</p>",
        },
    )
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_resume_roundtrip(client):
    assert client.get("/api/resume").json()["has_resume"] is False
    assert client.put("/api/resume", json={"content": RESUME}).status_code == 200
    data = client.get("/api/resume").json()
    assert data["has_resume"] is True
    assert data["content"] == RESUME.strip()


def test_resume_too_short(client):
    assert client.put("/api/resume", json={"content": "коротко"}).status_code == 422


def test_analyze_requires_resume(client):
    resp = client.post("/api/analyze", json={"url": "https://hh.kz/vacancy/1"})
    assert resp.status_code == 400


def test_analyze_by_url_and_tracker_flow(client):
    client.put("/api/resume", json={"content": RESUME})

    resp = client.post("/api/analyze", json={"url": "https://hh.kz/vacancy/123"})
    assert resp.status_code == 200
    item = resp.json()
    assert item["match_score"] == 68
    assert item["company"] == "Kolesa Group"
    assert item["source"] == "hh"
    assert item["cover_letter_en"] == "Hello!"

    listing = client.get("/api/applications").json()
    assert listing["stats"]["total"] == 1
    app_id = listing["items"][0]["id"]

    assert client.patch(f"/api/applications/{app_id}", json={"status": "applied"}).status_code == 200
    assert client.get(f"/api/applications/{app_id}").json()["status"] == "applied"

    assert client.patch(f"/api/applications/{app_id}", json={"status": "враньё"}).status_code == 400
    assert client.delete(f"/api/applications/{app_id}").status_code == 200
    assert client.get(f"/api/applications/{app_id}").status_code == 404


def test_analyze_by_text(client):
    client.put("/api/resume", json={"content": RESUME})
    vacancy_text = (
        "Компания X ищет стажёра-бэкендера в команду платформы. "
        "Требования: Python, SQL, базовый Linux, желание учиться и разбираться в чужом коде."
    )
    resp = client.post("/api/analyze", json={"text": vacancy_text})
    assert resp.status_code == 200
    assert resp.json()["source"] == "manual"


def test_analyze_bad_url(client):
    client.put("/api/resume", json={"content": RESUME})
    resp = client.post("/api/analyze", json={"url": "https://linkedin.com/jobs/view/1"})
    assert resp.status_code == 400


def test_analyze_no_input(client):
    client.put("/api/resume", json={"content": RESUME})
    assert client.post("/api/analyze", json={}).status_code == 400


def test_hh_error_maps_to_502(client, monkeypatch):
    client.put("/api/resume", json={"content": RESUME})

    def boom(vacancy_id):
        raise hh.HHError("Вакансия не найдена")

    monkeypatch.setattr("intern_agent.api.app.hh.fetch_vacancy", boom)
    resp = client.post("/api/analyze", json={"url": "https://hh.kz/vacancy/404404"})
    assert resp.status_code == 502
