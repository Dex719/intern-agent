from intern_agent import hh


def test_extract_vacancy_id_variants():
    assert hh.extract_vacancy_id("https://hh.kz/vacancy/12345678") == "12345678"
    assert hh.extract_vacancy_id("https://almaty.hh.kz/vacancy/987?from=search") == "987"
    assert hh.extract_vacancy_id("http://hh.ru/vacancy/42") == "42"
    assert hh.extract_vacancy_id("https://headhunter.kz/vacancy/77") == "77"


def test_extract_vacancy_id_invalid():
    assert hh.extract_vacancy_id("https://linkedin.com/jobs/view/123") is None
    assert hh.extract_vacancy_id("https://hh.kz/employer/123") is None
    assert hh.extract_vacancy_id("") is None
    assert hh.extract_vacancy_id(None) is None


def test_strip_html():
    raw = "<p>Мы ищем <strong>стажёра</strong>.</p><ul><li>Python</li><li>SQL</li></ul>"
    text = hh.strip_html(raw)
    assert "Мы ищем стажёра." in text
    assert "• Python" in text
    assert "• SQL" in text
    assert "<" not in text


def test_strip_html_entities():
    assert hh.strip_html("Junior&nbsp;Developer &amp; DevOps") == "Junior Developer & DevOps"


def test_salary_to_text():
    assert hh.salary_to_text(None) == "не указана"
    assert hh.salary_to_text({"from": 150000, "to": None, "currency": "KZT"}) == "от 150 000 KZT"
    assert "до 300 000" in hh.salary_to_text({"from": None, "to": 300000, "currency": "KZT"})


VACANCY = {
    "name": "Python Developer (Intern)",
    "alternate_url": "https://hh.kz/vacancy/111",
    "employer": {"name": "Kolesa Group"},
    "area": {"name": "Алматы"},
    "salary": {"from": 200000, "to": None, "currency": "KZT"},
    "experience": {"name": "Нет опыта"},
    "employment": {"name": "Стажировка"},
    "schedule": {"name": "Гибкий график"},
    "key_skills": [{"name": "Python"}, {"name": "Git"}],
    "description": "<p>Ищем стажёра в команду бэкенда.</p>",
}


def test_vacancy_to_text():
    text = hh.vacancy_to_text(VACANCY)
    assert "Python Developer (Intern)" in text
    assert "Kolesa Group" in text
    assert "Алматы" in text
    assert "Python, Git" in text
    assert "Ищем стажёра" in text


def test_vacancy_meta():
    meta = hh.vacancy_meta(VACANCY)
    assert meta == {
        "company": "Kolesa Group",
        "position": "Python Developer (Intern)",
        "url": "https://hh.kz/vacancy/111",
    }


JSON_LD = {
    "@type": "JobPosting",
    "title": "DevOPS администратор",
    "hiringOrganization": {"name": "Халык-Life, АО"},
    "jobLocation": {"address": {"addressLocality": "Алматы"}},
    "baseSalary": {"currency": "KZT", "value": {"minValue": 400000, "maxValue": 600000}},
    "description": "<p><strong>Обязанности</strong>:</p><ul><li>Администрирование серверов</li></ul>",
}


def test_normalize_json_ld():
    data = hh.normalize_json_ld(JSON_LD, "129562590")
    assert data["name"] == "DevOPS администратор"
    assert data["employer"]["name"] == "Халык-Life, АО"
    assert data["area"]["name"] == "Алматы"
    assert data["salary"] == {"from": 400000, "to": 600000, "currency": "KZT"}
    assert data["alternate_url"] == "https://hh.kz/vacancy/129562590"
    text = hh.vacancy_to_text(data)
    assert "Администрирование серверов" in text


def test_normalize_json_ld_no_salary():
    data = hh.normalize_json_ld({"title": "X"}, "1")
    assert data["salary"] is None


def test_json_ld_regex():
    page = '<html><script type="application/ld+json" nonce="">\n{"title": "Интерн"}\n</script></html>'
    match = hh.JSON_LD_RE.search(page)
    assert match
    import json
    assert json.loads(match.group(1))["title"] == "Интерн"
