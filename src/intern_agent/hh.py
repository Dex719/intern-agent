"""Клиент официального открытого API hh.ru / hh.kz (ключ не нужен)."""

import html
import json
import re
from html.parser import HTMLParser

import httpx

from intern_agent import config

VACANCY_URL_RE = re.compile(
    r"(?:hh\.(?:kz|ru)|headhunter\.(?:kz|ru))/vacancy/(\d+)", re.IGNORECASE
)


class HHError(Exception):
    """Ошибка при обращении к API hh."""


def extract_vacancy_id(url: str) -> str | None:
    """Достаёт id вакансии из ссылки вида https://hh.kz/vacancy/12345?query=..."""
    match = VACANCY_URL_RE.search(url or "")
    return match.group(1) if match else None


class _TextExtractor(HTMLParser):
    """HTML описания вакансии -> плоский текст с переносами строк."""

    BLOCK_TAGS = {"p", "li", "ul", "ol", "br", "div", "h1", "h2", "h3", "h4"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("• ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(html.unescape(raw or ""))
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def salary_to_text(salary: dict | None) -> str:
    if not salary:
        return "не указана"
    low, high = salary.get("from"), salary.get("to")
    currency = salary.get("currency", "")
    parts = []
    if low:
        parts.append(f"от {low:,}".replace(",", " "))
    if high:
        parts.append(f"до {high:,}".replace(",", " "))
    text = " ".join(parts) or "не указана"
    if parts and currency:
        text += f" {currency}"
    if parts and salary.get("gross"):
        text += " (до налогов)"
    return text


def fetch_vacancy_api(vacancy_id: str) -> dict:
    """GET /vacancies/{id} — полное описание вакансии через открытое API."""
    try:
        resp = httpx.get(
            f"{config.HH_API_BASE}/vacancies/{vacancy_id}",
            headers={"User-Agent": config.HH_USER_AGENT},
            timeout=config.HH_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise HHError(f"hh API недоступно: {exc}") from exc
    if resp.status_code == 404:
        raise HHError("Вакансия не найдена — возможно, её уже закрыли")
    if resp.status_code != 200:
        raise HHError(f"hh API вернуло статус {resp.status_code}")
    return resp.json()


JSON_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>\s*(\{.*?\})\s*</script>', re.DOTALL
)


def normalize_json_ld(data: dict, vacancy_id: str) -> dict:
    """schema.org JobPosting -> формат, совместимый с ответом API."""
    org = data.get("hiringOrganization") or {}
    address = ((data.get("jobLocation") or {}).get("address")) or {}
    salary_raw = data.get("baseSalary") or {}
    value = salary_raw.get("value") or {}
    salary = None
    if value.get("minValue") or value.get("maxValue"):
        salary = {
            "from": value.get("minValue"),
            "to": value.get("maxValue"),
            "currency": salary_raw.get("currency"),
        }
    return {
        "name": html.unescape(data.get("title", "")),
        "alternate_url": f"https://hh.kz/vacancy/{vacancy_id}",
        "employer": {"name": html.unescape(org.get("name", ""))},
        "area": {"name": address.get("addressLocality", "")},
        "salary": salary,
        "experience": {},
        "employment": {"name": data.get("employmentType", "")},
        "schedule": {},
        "key_skills": [],
        "description": data.get("description", ""),
    }


def fetch_vacancy_html(vacancy_id: str) -> dict:
    """Запасной путь: JSON-LD со страницы hh.kz/vacancy/{id}."""
    try:
        resp = httpx.get(
            f"https://hh.kz/vacancy/{vacancy_id}",
            headers={
                "User-Agent": config.HH_BROWSER_UA,
                "Accept-Language": "ru",
            },
            timeout=config.HH_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise HHError(f"hh.kz недоступен: {exc}") from exc
    if resp.status_code == 404:
        raise HHError("Вакансия не найдена — возможно, её уже закрыли")
    if resp.status_code != 200:
        raise HHError(f"hh.kz вернул статус {resp.status_code}")
    match = JSON_LD_RE.search(resp.text)
    if not match:
        raise HHError("Не удалось прочитать вакансию со страницы hh")
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise HHError("Не удалось прочитать вакансию со страницы hh") from exc
    return normalize_json_ld(data, vacancy_id)


def fetch_vacancy(vacancy_id: str) -> dict:
    """Сначала открытое API; если оно закрыто для IP сервера (403) — HTML-страница."""
    try:
        return fetch_vacancy_api(vacancy_id)
    except HHError as exc:
        if "не найдена" in str(exc):
            raise
        return fetch_vacancy_html(vacancy_id)


def vacancy_to_text(data: dict) -> str:
    """Нормализует JSON вакансии в текст для LLM."""
    employer = (data.get("employer") or {}).get("name", "")
    area = (data.get("area") or {}).get("name", "")
    experience = (data.get("experience") or {}).get("name", "")
    employment = (data.get("employment") or {}).get("name", "")
    schedule = (data.get("schedule") or {}).get("name", "")
    skills = ", ".join(s.get("name", "") for s in data.get("key_skills") or [])
    lines = [
        f"Позиция: {data.get('name', '')}",
        f"Компания: {employer}",
        f"Город: {area}",
        f"Зарплата: {salary_to_text(data.get('salary'))}",
        f"Опыт: {experience}",
        f"Занятость: {employment}" + (f", {schedule}" if schedule else ""),
    ]
    if skills:
        lines.append(f"Ключевые навыки: {skills}")
    description = strip_html(data.get("description", ""))
    if description:
        lines.append(f"\nОписание:\n{description}")
    return "\n".join(lines)


def vacancy_meta(data: dict) -> dict:
    """Короткие поля для трекера."""
    return {
        "company": (data.get("employer") or {}).get("name"),
        "position": data.get("name"),
        "url": data.get("alternate_url"),
    }


# ---------- поиск вакансий (для ленты) ----------

SEARCH_ITEM_RE = re.compile(
    r'data-qa="serp-item__title[^"]*"[^>]*href="[^"]*?/vacancy/(\d+)', re.IGNORECASE
)


def search_vacancies_api(query: str, area: str) -> list[str]:
    """GET /vacancies?text=... — id вакансий по запросу (свежие сверху)."""
    try:
        resp = httpx.get(
            f"{config.HH_API_BASE}/vacancies",
            params={
                "text": query,
                "area": area,
                "order_by": "publication_time",
                "per_page": 20,
            },
            headers={"User-Agent": config.HH_USER_AGENT},
            timeout=config.HH_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise HHError(f"hh API недоступно: {exc}") from exc
    if resp.status_code != 200:
        raise HHError(f"hh API вернуло статус {resp.status_code}")
    return [str(item["id"]) for item in resp.json().get("items", [])]


def search_vacancies_html(query: str, area: str) -> list[str]:
    """Запасной путь: id вакансий со страницы поиска hh.kz."""
    try:
        resp = httpx.get(
            "https://hh.kz/search/vacancy",
            params={"text": query, "area": area, "order_by": "publication_time"},
            headers={"User-Agent": config.HH_BROWSER_UA, "Accept-Language": "ru"},
            timeout=config.HH_TIMEOUT,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise HHError(f"hh.kz недоступен: {exc}") from exc
    if resp.status_code != 200:
        raise HHError(f"hh.kz вернул статус {resp.status_code}")
    seen: list[str] = []
    for vacancy_id in SEARCH_ITEM_RE.findall(resp.text):
        if vacancy_id not in seen:
            seen.append(vacancy_id)
    return seen


def search_vacancies(query: str, area: str | None = None) -> list[str]:
    """Сначала открытое API; если оно закрыто для IP сервера — HTML-страница поиска."""
    area = area or config.DEFAULT_SEARCH_AREA
    try:
        return search_vacancies_api(query, area)
    except HHError:
        return search_vacancies_html(query, area)
