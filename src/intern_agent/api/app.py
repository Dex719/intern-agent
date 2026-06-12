"""FastAPI: анализ вакансий + трекер откликов + статика."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from intern_agent import config, db, hh, llm


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.get_conn()
    conn.close()
    yield


app = FastAPI(title="Intern Agent", version="0.1.0", lifespan=lifespan)


class ResumeIn(BaseModel):
    content: str = Field(min_length=80, description="Текст резюме")


class AnalyzeIn(BaseModel):
    url: str | None = None
    text: str | None = None


class StatusIn(BaseModel):
    status: str


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "llm_configured": bool(config.GEMINI_API_KEY)}


# ---------- резюме ----------


@app.get("/api/resume")
def read_resume() -> dict:
    conn = db.get_conn()
    try:
        resume = db.get_resume(conn)
    finally:
        conn.close()
    return {"has_resume": resume is not None, **(resume or {})}


@app.put("/api/resume")
def write_resume(body: ResumeIn) -> dict:
    conn = db.get_conn()
    try:
        db.save_resume(conn, body.content.strip())
    finally:
        conn.close()
    return {"ok": True}


# ---------- анализ ----------


@app.post("/api/analyze")
async def analyze(body: AnalyzeIn) -> dict:
    conn = db.get_conn()
    try:
        resume = db.get_resume(conn)
        if not resume:
            raise HTTPException(400, "Сначала сохрани резюме (кнопка «Моё резюме»)")

        source, url, meta = "manual", None, {}
        if body.url and body.url.strip():
            vacancy_id = hh.extract_vacancy_id(body.url)
            if not vacancy_id:
                raise HTTPException(
                    400,
                    "Не похоже на ссылку hh.kz/hh.ru. Для других площадок вставь текст вакансии",
                )
            try:
                data = hh.fetch_vacancy(vacancy_id)
            except hh.HHError as exc:
                raise HTTPException(502, str(exc)) from exc
            vacancy_text = hh.vacancy_to_text(data)
            meta = hh.vacancy_meta(data)
            source, url = "hh", meta.get("url") or body.url.strip()
        elif body.text and len(body.text.strip()) >= 80:
            vacancy_text = body.text.strip()
        else:
            raise HTTPException(400, "Нужна ссылка на вакансию hh или её текст (от 80 символов)")

        try:
            result = await llm.analyze(resume["content"], vacancy_text)
        except llm.LLMError as exc:
            raise HTTPException(502, str(exc)) from exc

        application = {
            "source": source,
            "url": url,
            "company": meta.get("company") or result.get("company"),
            "position": meta.get("position") or result.get("position"),
            "vacancy_text": vacancy_text,
            "status": "analyzed",
            **{k: result.get(k) for k in (
                "match_score", "verdict", "matched", "missing", "recommendations",
                "tailored_resume", "cover_letter_ru", "cover_letter_en",
            )},
        }
        app_id = db.insert_application(conn, application)
        return {"id": app_id, **db.get_application(conn, app_id)}
    finally:
        conn.close()


# ---------- лента вакансий ----------


class QueriesIn(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=5)


@app.get("/api/settings")
def read_settings() -> dict:
    conn = db.get_conn()
    try:
        return {"queries": db.get_search_queries(conn)}
    finally:
        conn.close()


@app.put("/api/settings")
def write_settings(body: QueriesIn) -> dict:
    queries = [q.strip() for q in body.queries if q.strip()]
    if not queries:
        raise HTTPException(400, "Нужен хотя бы один поисковый запрос")
    conn = db.get_conn()
    try:
        db.save_search_queries(conn, queries)
    finally:
        conn.close()
    return {"ok": True, "queries": queries}


@app.post("/api/scan")
async def scan() -> dict:
    """Сканирует hh по сохранённым запросам, оценивает новые вакансии, кладёт в ленту."""
    conn = db.get_conn()
    try:
        resume = db.get_resume(conn)
        if not resume:
            raise HTTPException(400, "Сначала сохрани резюме (кнопка «Моё резюме»)")
        queries = db.get_search_queries(conn)
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
                raise HTTPException(502, "Поиск hh недоступен: " + "; ".join(errors))
            return {"added": 0, "items": [], "errors": errors}

        vacancies: list[dict] = []
        for vacancy_id in candidate_ids:
            try:
                data = hh.fetch_vacancy(vacancy_id)
            except hh.HHError:
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
            raise HTTPException(502, "Не удалось загрузить ни одной вакансии с hh")

        try:
            scores = await llm.screen_batch(
                resume["content"], [{"id": v["id"], "text": v["text"]} for v in vacancies]
            )
        except llm.LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        score_map = {s["id"]: s for s in scores}

        added = 0
        for v in vacancies:
            s = score_map.get(v["id"])
            if not s:
                continue
            db.insert_feed_item(
                conn,
                {
                    "vacancy_id": v["id"],
                    "url": v["meta"].get("url"),
                    "position": v["meta"].get("position"),
                    "company": v["meta"].get("company"),
                    "salary": v["salary"],
                    "score": s["score"],
                    "reason": s["reason"],
                    "vacancy_text": v["text"],
                },
            )
            added += 1
        return {"added": added, "items": db.list_feed(conn, "new"), "errors": errors}
    finally:
        conn.close()


@app.get("/api/feed")
def feed(status: str = "new") -> dict:
    if status not in config.FEED_STATUSES:
        raise HTTPException(400, f"Статус должен быть одним из: {config.FEED_STATUSES}")
    conn = db.get_conn()
    try:
        return {"items": db.list_feed(conn, status)}
    finally:
        conn.close()


@app.patch("/api/feed/{item_id}")
def feed_status(item_id: int, body: StatusIn) -> dict:
    if body.status not in config.FEED_STATUSES:
        raise HTTPException(400, f"Статус должен быть одним из: {config.FEED_STATUSES}")
    conn = db.get_conn()
    try:
        ok = db.set_feed_status(conn, item_id, body.status)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(404, "Вакансия не найдена")
    return {"ok": True}


@app.post("/api/feed/{item_id}/apply")
async def feed_apply(item_id: int) -> dict:
    """Готовит отклик: полный разбор + письма, сохраняет в трекер как «отклик»."""
    conn = db.get_conn()
    try:
        item = db.get_feed_item(conn, item_id)
        if not item:
            raise HTTPException(404, "Вакансия не найдена")
        resume = db.get_resume(conn)
        if not resume:
            raise HTTPException(400, "Сначала сохрани резюме (кнопка «Моё резюме»)")
        try:
            result = await llm.analyze(resume["content"], item["vacancy_text"])
        except llm.LLMError as exc:
            raise HTTPException(502, str(exc)) from exc
        application = {
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
        app_id = db.insert_application(conn, application)
        db.set_feed_status(conn, item_id, "applied")
        return {"id": app_id, **db.get_application(conn, app_id)}
    finally:
        conn.close()


# ---------- трекер ----------


@app.get("/api/applications")
def applications(status: str | None = None) -> dict:
    conn = db.get_conn()
    try:
        items = db.list_applications(conn, status)
        stats = db.stats_by_status(conn)
    finally:
        conn.close()
    return {"items": items, "stats": stats}


@app.get("/api/applications/{app_id}")
def application_detail(app_id: int) -> dict:
    conn = db.get_conn()
    try:
        item = db.get_application(conn, app_id)
    finally:
        conn.close()
    if not item:
        raise HTTPException(404, "Отклик не найден")
    return item


@app.patch("/api/applications/{app_id}")
def application_status(app_id: int, body: StatusIn) -> dict:
    if body.status not in config.APPLICATION_STATUSES:
        raise HTTPException(400, f"Статус должен быть одним из: {config.APPLICATION_STATUSES}")
    conn = db.get_conn()
    try:
        ok = db.update_status(conn, app_id, body.status)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(404, "Отклик не найден")
    return {"ok": True}


@app.delete("/api/applications/{app_id}")
def application_delete(app_id: int) -> dict:
    conn = db.get_conn()
    try:
        ok = db.delete_application(conn, app_id)
    finally:
        conn.close()
    if not ok:
        raise HTTPException(404, "Отклик не найден")
    return {"ok": True}


# ---------- статика ----------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
