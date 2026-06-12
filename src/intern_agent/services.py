"""Сканирование hh + авто-скан по расписанию + уведомления в Telegram."""

import asyncio
import html as html_lib
import sqlite3

import httpx

from intern_agent import config, db, hh, hh_account, llm

SETTING_KEYS = (
    "llm_provider",
    "llm_api_key",
    "llm_model",
    "auto_scan_hours",
    "tg_bot_token",
    "tg_chat_id",
    "hh_client_id",
    "hh_client_secret",
    "hh_resume_id",
    "hh_resume_title",
    "auto_apply_enabled",
    "auto_apply_min_score",
)
NOTIFY_MIN_SCORE = 60
AUTO_APPLY_MAX_PER_RUN = 5


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


# ---------- отклик через hh ----------


def build_application(item: dict, result: dict) -> dict:
    """Собирает запись трекера из элемента ленты и результата analyze."""
    return {
        "source": "feed",
        "url": item["url"],
        "company": item["company"] or result.get("company"),
        "position": item["position"] or result.get("position"),
        "vacancy_text": item["vacancy_text"],
        "status": "applied",
        **{k: result.get(k) for k in (
            "match_score", "verdict", "matched", "missing", "recommendations",
            "tailored_resume", "cover_letter_ru", "cover_letter_en",
        )},
    }


async def send_hh_response(conn: sqlite3.Connection, item: dict, cover_letter: str) -> None:
    """Шлёт отклик на hh от имени привязанного аккаунта. Бросает HHAccountError."""
    resume_id = db.get_setting(conn, "hh_resume_id")
    if not resume_id:
        raise hh_account.HHAccountError("Не выбрано резюме hh в настройках")
    token = await hh_account.get_valid_token(conn)
    await hh_account.apply_to_vacancy(token, item["vacancy_id"], resume_id, cover_letter)


async def auto_apply_new_items(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Автоотклик: для вакансий со скором выше порога делает разбор и шлёт отклик на hh."""
    if db.get_setting(conn, "auto_apply_enabled") != "1":
        return []
    if not db.get_setting(conn, "hh_access_token"):
        return []
    min_score = _safe_int(db.get_setting(conn, "auto_apply_min_score")) or 70
    good = [it for it in items if (it.get("score") or 0) >= min_score]
    good = good[:AUTO_APPLY_MAX_PER_RUN]
    resume = db.get_resume(conn)
    applied: list[dict] = []
    for item in good:
        title = f"{item.get('position') or 'вакансия'} — {item.get('company') or ''}".strip(" —")
        try:
            result = await llm.analyze(
                resume["content"], item["vacancy_text"], get_all_settings(conn)
            )
            await send_hh_response(conn, item, result.get("cover_letter_ru") or "")
        except (llm.LLMError, hh_account.HHAccountError) as exc:
            db.add_log(conn, "error", "auto-apply", f"{title}: {exc}")
            continue
        db.insert_application(conn, build_application(item, result))
        feed_row = db.get_feed_item_by_vacancy(conn, item["vacancy_id"])
        if feed_row:
            db.set_feed_status(conn, feed_row["id"], "applied")
        db.add_log(conn, "info", "auto-apply", f"отклик отправлен: {title}")
        applied.append(item)
    return applied


async def semi_auto_covers(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Полуавтомат: hh не привязан — пишет сопроводительное и шлёт его в Telegram,
    отклик пользователь отправляет руками (копировать → вставить)."""
    if db.get_setting(conn, "auto_apply_enabled") != "1":
        return []
    if db.get_setting(conn, "hh_access_token"):
        return []  # привязан hh — работает настоящий автоотклик
    token = db.get_setting(conn, "tg_bot_token")
    chat_id = db.get_setting(conn, "tg_chat_id")
    if not (token and chat_id):
        return []
    min_score = _safe_int(db.get_setting(conn, "auto_apply_min_score")) or 70
    good = [it for it in items if (it.get("score") or 0) >= min_score]
    good = good[:AUTO_APPLY_MAX_PER_RUN]
    resume = db.get_resume(conn)
    sent: list[dict] = []
    for item in good:
        title = f"{item.get('position') or 'вакансия'} — {item.get('company') or ''}".strip(" —")
        try:
            result = await llm.analyze(
                resume["content"], item["vacancy_text"], get_all_settings(conn)
            )
            cover = (result.get("cover_letter_ru") or "").strip()
            if not cover:
                continue
            text = (
                f"✍️ <b>{item.get('score')}/100 · {html_lib.escape(title)}</b>\n"
                f'<a href="{item.get("url")}">Открыть вакансию и откликнуться</a>\n\n'
                f"Сопроводительное — скопируй и вставь:\n"
                f"<pre>{html_lib.escape(cover)}</pre>"
            )
            await send_telegram(token, chat_id, text)
        except (llm.LLMError, httpx.HTTPError) as exc:
            db.add_log(conn, "error", "cover-tg", f"{title}: {exc}")
            continue
        application = build_application(item, result)
        application["status"] = "analyzed"
        db.insert_application(conn, application)
        db.add_log(conn, "info", "cover-tg", f"сопроводительное отправлено в TG: {title}")
        sent.append(item)
    return sent


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


def format_notification(
    items: list[dict],
    applied: list[dict] | None = None,
    covered: list[dict] | None = None,
) -> str:
    applied_ids = {it.get("vacancy_id") for it in applied or []}
    covered_ids = {it.get("vacancy_id") for it in covered or []}
    lines = ["<b>Новые стоящие вакансии</b>"]
    for it in items[:8]:
        title = f"{it.get('position') or 'Вакансия'} — {it.get('company') or ''}".strip(" —")
        mark = ""
        if it.get("vacancy_id") in applied_ids:
            mark = " ✅ отклик отправлен"
        elif it.get("vacancy_id") in covered_ids:
            mark = " ✍️ сопроводительное выше"
        lines.append(f"• <b>{it.get('score')}</b>/100 <a href=\"{it.get('url')}\">{title}</a>{mark}")
    return "\n".join(lines)


async def notify_new_items(
    conn: sqlite3.Connection,
    items: list[dict],
    applied: list[dict] | None = None,
    covered: list[dict] | None = None,
) -> bool:
    """Шлёт в TG вакансии со скором выше порога, если бот настроен."""
    token = db.get_setting(conn, "tg_bot_token")
    chat_id = db.get_setting(conn, "tg_chat_id")
    good = [it for it in items if (it.get("score") or 0) >= NOTIFY_MIN_SCORE]
    if not (token and chat_id and good):
        return False
    await send_telegram(token, chat_id, format_notification(good, applied, covered))
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
                applied = await auto_apply_new_items(conn, result["new_items"])
                covered = await semi_auto_covers(conn, result["new_items"])
                try:
                    await notify_new_items(conn, result["new_items"], applied, covered)
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
