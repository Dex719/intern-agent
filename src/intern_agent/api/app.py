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
