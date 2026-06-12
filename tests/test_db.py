import pytest

from intern_agent import db


@pytest.fixture()
def conn(tmp_path):
    connection = db.get_conn(tmp_path / "test.db")
    yield connection
    connection.close()


def test_resume_roundtrip(conn):
    assert db.get_resume(conn) is None
    db.save_resume(conn, "Моё резюме v1")
    assert db.get_resume(conn)["content"] == "Моё резюме v1"
    db.save_resume(conn, "Моё резюме v2")
    assert db.get_resume(conn)["content"] == "Моё резюме v2"


def _sample_app():
    return {
        "source": "hh",
        "url": "https://hh.kz/vacancy/1",
        "company": "Kolesa Group",
        "position": "Intern",
        "vacancy_text": "текст вакансии",
        "match_score": 72,
        "verdict": "Стоит откликнуться",
        "matched": ["Python", "Git"],
        "missing": ["Docker"],
        "recommendations": ["Подтянуть Docker"],
        "tailored_resume": "резюме под вакансию",
        "cover_letter_ru": "письмо",
        "cover_letter_en": "letter",
    }


def test_application_crud(conn):
    app_id = db.insert_application(conn, _sample_app())

    items = db.list_applications(conn)
    assert len(items) == 1
    assert items[0]["company"] == "Kolesa Group"
    assert "vacancy_text" not in items[0]  # список — без тяжёлых полей

    detail = db.get_application(conn, app_id)
    assert detail["matched"] == ["Python", "Git"]
    assert detail["cover_letter_en"] == "letter"

    assert db.update_status(conn, app_id, "interview")
    assert db.get_application(conn, app_id)["status"] == "interview"

    assert db.delete_application(conn, app_id)
    assert db.get_application(conn, app_id) is None
    assert not db.delete_application(conn, app_id)


def test_stats(conn):
    db.insert_application(conn, _sample_app())
    second = db.insert_application(conn, _sample_app())
    db.update_status(conn, second, "offer")
    stats = db.stats_by_status(conn)
    assert stats["total"] == 2
    assert stats["analyzed"] == 1
    assert stats["offer"] == 1


def test_list_filter_by_status(conn):
    db.insert_application(conn, _sample_app())
    assert db.list_applications(conn, "offer") == []
    assert len(db.list_applications(conn, "analyzed")) == 1
