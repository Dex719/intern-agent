"""Сканирование hh + авто-скан по расписанию + уведомления в Telegram."""

import asyncio
import sqlite3

import httpx

from intern_agent import config, db, hh, llm

SETTING_KEYS = (
    "llm_provider",
    "llm_api_key",
    "llm_model",
    "auto_scan_hours",
    "tg_bot_token",
    "tg_chat_id",
)
NOTIFY_MIN_SCORE = 60


def get_all_settings(conn: sqlite3.Connection) -> dict:
    settings = {key: db.get_setting(conn, key) for key in SETTING_KEYS}
    settings["queries"] = db.get_search_queries(conn)
    return settings


class ScanError(Exception):
    """Скан не удался целиком."""


async def run_scan(conn: sqlite3.Connection) -> dict:
    """Ищет новые вакансии по сохранённым запросам, оценивает, кладёт в ленту."""
    resume = db.get_resume(conn)
    if not resume:
        raise ScanError("Сначала сохрани резюме (кнопка «Моё резюме»)")
    settings = get_all_settings(conn)
    queries = settings["queries"]
    known = db.feed_known_ids(conn)

    candidate_ids: list[str] = []
    errors: list[str] = []
    for query in queries:
        try:
            for vacancy_id in hh.search_vacancies(query):
                if vacancy_id not in known and vacancy_id not in candidate_ids:
                    candidate_ids.append(vacancy_id)
        except hh.HHError as exc:
            errors.append(f"«{query}»: {exc}")
    candidate_ids = candidate_ids[: config.SCAN_MAX_NEW]
    if not candidate_ids:
        if errors and len(errors) == len(queries):
            raise ScanError("Поиск hh недоступен: " + "; ".join(errors))
        return {"added": 0, "items": [], "errors": errors}

    vacancies: list[dict] = []
    for vacancy_id in candidate_ids:
        try:
            data = hh.fetch_vacancy(vacancy_id)
        except hh.HHError as exc:
            errors.append(f"вакансия {vacancy_id}: {exc}")
            continue
        vacancies.append(
            {
                "id": vacancy_id,
                "text": hh.vacancy_to_text(data),
                "meta": hh.vacancy_meta(data),
                "salary": hh.salary_to_text(data.get("salary")),
            }
        )
    if not vacancies:
        raise ScanError("Не удалось загрузить ни одной вакансии с hh")

    try:
        scores = await llm.screen_batch(
            resume["content"],
            [{"id": v["id"], "text": v["text"]} for v in vacancies],
            settings,
        )
    except llm.LLMError as exc:
        raise ScanError(str(exc)) from exc
    score_map = {s["id"]: s for s in scores}

    new_items: list[dict] = []
    for v in vacancies:
        s = score_map.get(v["id"])
        if not s:
            continue
        item = {
            "vacancy_id": v["id"],
            "url": v["meta"].get("url"),
            "position": v["meta"].get("position"),
            "company": v["meta"].get("company"),
            "salary": v["salary"],
            "score": s["score"],
            "reason": s["reason"],
            "vacancy_text": v["text"],
        }
        db.insert_feed_item(conn, item)
        new_items.append(item)
    return {
        "added": len(new_items),
        "items": db.list_feed(conn, "new"),
        "new_items": new_items,
        "errors": errors,
    }


# ---------- Telegram ----------


async def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            url,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        resp.raise_for_status()


def format_notification(items: list[dict]) -> str:
    lines = ["<b>Новые стоящие вакансии</b>"]
    for it in items[:8]:
        title = f"{it.get('position') or 'Вакансия'} — {it.get('company') or ''}".strip(" —")
        lines.append(f"• <b>{it.get('score')}</b>/100 <a href=\"{it.get('url')}\">{title}</a>")
    return "\n".join(lines)


async def notify_new_items(conn: sqlite3.Connection, items: list[dict]) -> bool:
    """Шлёт в TG вакансии со скором выше порога, если бот настроен."""
    token = db.get_setting(conn, "tg_bot_token")
    chat_id = db.get_setting(conn, "tg_chat_id")
    good = [it for it in items if (it.get("score") or 0) >= NOTIFY_MIN_SCORE]
    if not (token and chat_id and good):
        return False
    await send_telegram(token, chat_id, format_notification(good))
    return True


# ---------- авто-скан по расписанию ----------


async def auto_scan_loop() -> None:
    """Фоновая задача: скан каждые auto_scan_hours часов (0 = выключено)."""
    while True:
        await asyncio.sleep(15 * 60)
        conn = db.get_conn()
        try:
            hours = _safe_int(db.get_setting(conn, "auto_scan_hours"))
            if hours <= 0:
                continue
            last = db.get_setting(conn, "last_auto_scan_ts")
            if last and not _due(last, hours):
                continue
            db.set_setting(conn, "last_auto_scan_ts", db._now())
            try:
                result = await run_scan(conn)
            except ScanError as exc:
                db.add_log(conn, "error", "auto-scan", str(exc))
                continue
            db.add_log(
                conn,
                "info",
                "auto-scan",
                f"добавлено {result['added']} вакансий" + (
                    f"; ошибки: {'; '.join(result['errors'])}" if result["errors"] else ""
                ),
            )
            if result["added"]:
                try:
                    await notify_new_items(conn, result["new_items"])
                except httpx.HTTPError as exc:
                    db.add_log(conn, "error", "telegram", f"не смог отправить: {exc}")
        except Exception as exc:  # noqa: BLE001 — фон не должен падать
            db.add_log(conn, "error", "auto-scan", f"неожиданная ошибка: {exc}")
        finally:
            conn.close()


def _safe_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _due(last_iso: str, hours: int) -> bool:
    from datetime import datetime, timedelta, timezone

    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - last >= timedelta(hours=hours)
