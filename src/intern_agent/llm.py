"""LLM-провайдеры (Gemini / OpenAI / OpenRouter): анализ вакансий и скрининг ленты."""

import asyncio
import json

import httpx

from intern_agent import config

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

OPENAI_BASES = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "openai": "gpt-4o-mini",
    "openrouter": "google/gemini-2.0-flash-001",
}

RETRY_STATUSES = {429, 500, 502, 503}
MAX_ATTEMPTS = 3

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

Ответ — строго JSON-объект с ключами: company, position, match_score, verdict,
matched (массив строк), missing (массив строк), recommendations (массив строк),
tailored_resume, cover_letter_ru, cover_letter_en. Без markdown и пояснений.

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
    """Ошибка вызова LLM."""


def resolve_config(settings: dict | None = None) -> dict:
    """Собирает конфиг провайдера: настройки из БД поверх переменных окружения."""
    settings = settings or {}
    provider = (settings.get("llm_provider") or "gemini").strip().lower()
    if provider not in ("gemini", "openai", "openrouter"):
        provider = "gemini"
    api_key = (settings.get("llm_api_key") or "").strip()
    if not api_key and provider == "gemini":
        api_key = config.GEMINI_API_KEY
    model = (settings.get("llm_model") or "").strip() or (
        config.GEMINI_MODEL if provider == "gemini" else DEFAULT_MODELS[provider]
    )
    return {"provider": provider, "api_key": api_key, "model": model}


def build_prompt(resume: str, vacancy: str) -> str:
    return PROMPT_TEMPLATE.format(resume=resume.strip(), vacancy=vacancy.strip())


def _json_from_text(text: str):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError("ИИ вернул некорректный JSON") from exc


def _extract_text_gemini(payload: dict) -> str:
    try:
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        block = (payload.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise LLMError(f"Gemini отклонил запрос: {block}") from exc
        raise LLMError("Пустой ответ от Gemini") from exc


def _extract_text_openai(payload: dict) -> str:
    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("Пустой ответ от ИИ-провайдера") from exc


async def _post_with_retry(url: str, *, headers: dict, params: dict, body: dict) -> dict:
    """POST с ретраями на 429/5xx (перегруз провайдера)."""
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        async with httpx.AsyncClient(timeout=config.GEMINI_TIMEOUT) as client:
            try:
                resp = await client.post(url, headers=headers, params=params, json=body)
            except httpx.HTTPError as exc:
                last_error = f"сеть: {exc}"
                resp = None
        if resp is not None:
            if resp.status_code == 200:
                return resp.json()
            detail = resp.text[:200].replace("\n", " ")
            last_error = f"статус {resp.status_code}: {detail}"
            if resp.status_code not in RETRY_STATUSES:
                break
        if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(2 * attempt)
    raise LLMError(f"ИИ-провайдер недоступен после {MAX_ATTEMPTS} попыток ({last_error})")


async def _call_json(prompt: str, cfg: dict, *, schema: dict | None, temperature: float):
    """Вызов LLM, возвращает распарсенный JSON (dict или list)."""
    provider, api_key, model = cfg["provider"], cfg["api_key"], cfg["model"]
    if not api_key:
        raise LLMError("API-ключ не задан — добавь его в настройках (шестерёнка)")
    if provider == "gemini":
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "responseMimeType": "application/json",
                **({"responseSchema": schema} if schema else {}),
            },
        }
        payload = await _post_with_retry(
            GEMINI_URL.format(model=model), headers={}, params={"key": api_key}, body=body
        )
        return _json_from_text(_extract_text_gemini(payload))
    body = {
        "model": model,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": prompt}],
    }
    payload = await _post_with_retry(
        f"{OPENAI_BASES[provider]}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        params={},
        body=body,
    )
    return _json_from_text(_extract_text_openai(payload))


def _validate_analysis(result) -> dict:
    if not isinstance(result, dict):
        raise LLMError("ИИ вернул некорректный JSON")
    result["match_score"] = max(0, min(100, int(result.get("match_score", 0))))
    return result


def parse_response(payload: dict) -> dict:
    """Достаёт и валидирует JSON из ответа Gemini generateContent."""
    return _validate_analysis(_json_from_text(_extract_text_gemini(payload)))


async def analyze(resume: str, vacancy: str, settings: dict | None = None) -> dict:
    """Один вызов LLM: скор + разбор требований + резюме + два письма."""
    cfg = resolve_config(settings)
    result = await _call_json(
        build_prompt(resume, vacancy), cfg, schema=RESPONSE_SCHEMA, temperature=0.5
    )
    return _validate_analysis(result)


# ---------- быстрый скрининг пачки вакансий (для ленты) ----------

SCREEN_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "id": {"type": "STRING"},
            "score": {"type": "INTEGER"},
            "reason": {"type": "STRING"},
        },
        "required": ["id", "score", "reason"],
    },
}

SCREEN_PROMPT = """Ты — технический рекрутер. Кандидат ищет стажировку / junior-позицию в IT.
Быстро оцени каждую вакансию из списка по его резюме.

Для каждой вакансии верни:
- id: id вакансии без изменений.
- score: целое 0–100 — насколько кандидату стоит откликаться. Будь реалистичен:
  для стажировки полное совпадение не нужно, но нерелевантные вакансии
  (другая профессия, требуется большой опыт) оценивай низко.
- reason: одно короткое предложение по-русски — почему такой балл.

Ответ — строго JSON: либо массив объектов {{"id", "score", "reason"}},
либо объект {{"items": [...]}} с таким массивом. Без markdown и пояснений.

РЕЗЮМЕ КАНДИДАТА:
---
{resume}
---

ВАКАНСИИ:
{vacancies}
"""


def _validate_screen(items) -> list[dict]:
    if isinstance(items, dict):
        items = items.get("items", [])
    result = []
    for item in items if isinstance(items, list) else []:
        try:
            result.append(
                {
                    "id": str(item["id"]),
                    "score": max(0, min(100, int(item["score"]))),
                    "reason": str(item.get("reason", "")).strip(),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not result:
        raise LLMError("ИИ не вернул оценки вакансий")
    return result


def parse_screen_response(payload: dict) -> list[dict]:
    return _validate_screen(_json_from_text(_extract_text_gemini(payload)))


async def screen_batch(
    resume: str, vacancies: list[dict], settings: dict | None = None
) -> list[dict]:
    """Один вызов LLM: список {id, text} -> список {id, score, reason}."""
    cfg = resolve_config(settings)
    blocks = [f"=== Вакансия id={v['id']} ===\n{v['text'][:3500]}" for v in vacancies]
    prompt = SCREEN_PROMPT.format(resume=resume.strip(), vacancies="\n\n".join(blocks))
    schema = SCREEN_SCHEMA if cfg["provider"] == "gemini" else None
    items = await _call_json(prompt, cfg, schema=schema, temperature=0.3)
    return _validate_screen(items)
