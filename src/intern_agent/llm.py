"""Gemini API: анализ соответствия резюме и вакансии, генерация писем."""

import json

import httpx

from intern_agent import config

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Строгая JSON-схема ответа (Gemini structured output).
RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "company": {"type": "STRING"},
        "position": {"type": "STRING"},
        "match_score": {"type": "INTEGER"},
        "verdict": {"type": "STRING"},
        "matched": {"type": "ARRAY", "items": {"type": "STRING"}},
        "missing": {"type": "ARRAY", "items": {"type": "STRING"}},
        "recommendations": {"type": "ARRAY", "items": {"type": "STRING"}},
        "tailored_resume": {"type": "STRING"},
        "cover_letter_ru": {"type": "STRING"},
        "cover_letter_en": {"type": "STRING"},
    },
    "required": [
        "company",
        "position",
        "match_score",
        "verdict",
        "matched",
        "missing",
        "recommendations",
        "tailored_resume",
        "cover_letter_ru",
        "cover_letter_en",
    ],
}

PROMPT_TEMPLATE = """Ты — опытный карьерный консультант и технический рекрутер.
Кандидат ищет стажировку / junior-позицию в IT. Сравни его резюме с вакансией.

Правила:
- match_score: целое 0–100 — насколько кандидат подходит. Будь реалистичен: стажировка
  не требует полного совпадения, но завышать оценку нельзя.
- verdict: 1–2 предложения по-русски — стоит ли откликаться и почему.
- matched: требования вакансии, которые кандидат уже закрывает (по резюме, коротко).
- missing: требования, которых в резюме нет или они слабые. Не выдумывай.
- recommendations: конкретные шаги — что подтянуть или подчеркнуть в отклике.
- tailored_resume: переписанный раздел «О себе» + ключевые пункты резюме, заточенные
  под ЭТУ вакансию. Только факты из исходного резюме, ничего не выдумывать.
  Язык — как у исходного резюме.
- cover_letter_ru / cover_letter_en: сопроводительное письмо (120–180 слов), живое и
  конкретное, без клише вроде «я командный игрок». Упомянуть 1–2 проекта кандидата,
  релевантных вакансии. Подпись — имя кандидата из резюме.
- company / position: название компании и позиции из вакансии.

РЕЗЮМЕ КАНДИДАТА:
---
{resume}
---

ВАКАНСИЯ:
---
{vacancy}
---
"""


class LLMError(Exception):
    """Ошибка вызова Gemini."""


def build_prompt(resume: str, vacancy: str) -> str:
    return PROMPT_TEMPLATE.format(resume=resume.strip(), vacancy=vacancy.strip())


def parse_response(payload: dict) -> dict:
    """Достаёт и валидирует JSON из ответа generateContent."""
    try:
        text = payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        block = (payload.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMError(f"Gemini отклонил запрос: {block}") from exc
        raise LLMError("Пустой ответ от Gemini") from exc
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("Gemini вернул некорректный JSON") from exc
    result["match_score"] = max(0, min(100, int(result.get("match_score", 0))))
    return result


async def analyze(resume: str, vacancy: str) -> dict:
    """Один вызов Gemini: скор + разбор требований + резюме + два письма."""
    if not config.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY не задан — добавь его в переменные окружения")
    body = {
        "contents": [{"parts": [{"text": build_prompt(resume, vacancy)}]}],
        "generationConfig": {
            "temperature": 0.5,
            "responseMimeType": "application/json",
            "responseSchema": RESPONSE_SCHEMA,
        },
    }
    url = API_URL.format(model=config.GEMINI_MODEL)
    async with httpx.AsyncClient(timeout=config.GEMINI_TIMEOUT) as client:
        try:
            resp = await client.post(
                url, params={"key": config.GEMINI_API_KEY}, json=body
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"Gemini API недоступно: {exc}") from exc
    if resp.status_code == 429:
        raise LLMError("Лимит запросов Gemini исчерпан — подожди минуту и попробуй снова")
    if resp.status_code != 200:
        raise LLMError(f"Gemini API вернуло статус {resp.status_code}")
    return parse_response(resp.json())
