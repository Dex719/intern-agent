"""FastAPI: лента вакансий + анализ + трекер откликов + auth + статика."""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from intern_agent import auth, config, db, hh, llm, services


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.get_conn()
    conn.close()
    task = asyncio.create_task(services.auto_scan_loop())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="Intern Agent", version="0.1.0", lifespan=lifespan)

PUBLIC_PATHS = {"/api/health", "/api/auth/state", "/api/auth/setup", "/api/auth/login"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Все /api/* (кроме публичных) требуют сессию, если пароль установлен."""
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        conn = db.get_conn()
        try:
            if auth.password_is_set(conn) and not auth.session_valid(
                conn, request.cookies.get(auth.COOKIE_NAME)
            ):
                return JSONResponse({"detail": "Требуется вход"}, status_code=401)
        finally:
            conn.close()
    return await call_next(request)


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    secure = (
        request.url.scheme == "https"
        or request.headers.get("x-forwarded-proto", "") == "https"
    )
    response.set_cookie(
        auth.COOKIE_NAME,
        token,
        max_age=auth.SESSION_DAYS * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=secure,
    )


class PasswordIn(BaseModel):
    password: str = Field(min_length=8, max_length=128)


@app.get("/api/auth/state")
def auth_state(request: Request) -> dict:
    conn = db.get_conn()
    try:
        password_set = auth.password_is_set(conn)
        authed = not password_set or auth.session_valid(
            conn, request.cookies.get(auth.COOKIE_NAME)
        )
    finally:
        conn.close()
    return {"password_set": password_set, "authed": authed}


@app.post("/api/auth/setup")
def auth_setup(body: PasswordIn, request: Request, response: Response) -> dict:
    """Первичная установка пароля (только пока он не задан)."""
    conn = db.get_conn()
    try:
        if auth.password_is_set(conn):
            raise HTTPException(403, "Пароль уже установлен")
        auth.set_password(conn, body.password)
        token = auth.create_session(conn)
        db.add_log(conn, "info", "auth", "пароль установлен")
    finally:
        conn.close()
    _set_session_cookie(request, response, token)
    return {"ok": True}


@app.post("/api/auth/login")
async def auth_login(body: PasswordIn, request: Request, response: Response) -> dict:
    conn = db.get_conn()
    try:
        if not auth.password_is_set(conn):
            raise HTTPException(400, "Пароль ещё не установлен")
        if not auth.check_password(conn, body.password):
            db.add_log(conn, "warn", "auth", "неверный пароль при входе")
            await asyncio.sleep(0.7)  # тормозим перебор
            raise HTTPException(401, "Неверный пароль")
        token = auth.create_session(conn)
    finally:
        conn.close()
    _set_session_cookie(request, response, token)
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict:
    conn = db.get_conn()
    try:
        auth.drop_session(conn, request.cookies.get(auth.COOKIE_NAME))
    finally:
        conn.close()
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


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
            result = await llm.analyze(
                resume["content"], vacancy_text, services.get_all_settings(conn)
            )
        except llm.LLMError as exc:
            db.add_log(conn, "error", "analyze", str(exc))
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


class SettingsIn(BaseModel):
    queries: list[str] | None = Field(default=None, max_length=5)
    llm_provider: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    auto_scan_hours: int | None = Field(default=None, ge=0, le=48)
    tg_bot_token: str | None = None
    tg_chat_id: str | None = None


SECRET_KEYS = {"llm_api_key", "tg_bot_token"}


def _mask(value: str) -> str:
    return f"…{value[-4:]}" if value else ""


def _settings_out(conn) -> dict:
    settings = services.get_all_settings(conn)
    for key in SECRET_KEYS:
        settings[key] = _mask(settings[key])
    settings["llm_provider"] = settings["llm_provider"] or "gemini"
    settings["llm_default_key"] = bool(config.GEMINI_API_KEY)
    settings["auto_scan_hours"] = services._safe_int(settings["auto_scan_hours"])
    return settings


@app.get("/api/settings")
def read_settings() -> dict:
    conn = db.get_conn()
    try:
        return _settings_out(conn)
    finally:
        conn.close()


@app.put("/api/settings")
def write_settings(body: SettingsIn) -> dict:
    conn = db.get_conn()
    try:
        if body.queries is not None:
            queries = [q.strip() for q in body.queries if q.strip()]
            if not queries:
                raise HTTPException(400, "Нужен хотя бы один поисковый запрос")
            db.save_search_queries(conn, queries)
        if body.llm_provider is not None:
            if body.llm_provider not in ("gemini", "openai", "openrouter"):
                raise HTTPException(400, "Провайдер: gemini, openai или openrouter")
            db.set_setting(conn, "llm_provider", body.llm_provider)
        for key in ("llm_api_key", "llm_model", "tg_bot_token", "tg_chat_id"):
            value = getattr(body, key)
            if value is not None:
                db.set_setting(conn, key, value.strip())
        if body.auto_scan_hours is not None:
            db.set_setting(conn, "auto_scan_hours", str(body.auto_scan_hours))
        return {"ok": True, **_settings_out(conn)}
    finally:
        conn.close()


@app.get("/api/logs")
def read_logs(limit: int = 100) -> dict:
    conn = db.get_conn()
    try:
        return {"items": db.list_logs(conn, max(1, min(300, limit)))}
    finally:
        conn.close()


@app.post("/api/scan")
async def scan() -> dict:
    """Сканирует hh по сохранённым запросам, оценивает новые вакансии, кладёт в ленту."""
    conn = db.get_conn()
    try:
        try:
            result = await services.run_scan(conn)
        except services.ScanError as exc:
            db.add_log(conn, "error", "scan", str(exc))
            raise HTTPException(502, str(exc)) from exc
        db.add_log(
            conn,
            "info",
            "scan",
            f"добавлено {result['added']} вакансий" + (
                f"; ошибки: {'; '.join(result['errors'])}" if result["errors"] else ""
            ),
        )
        return {k: result[k] for k in ("added", "items", "errors")}
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
            result = await llm.analyze(
                resume["content"], item["vacancy_text"], services.get_all_settings(conn)
            )
        except llm.LLMError as exc:
            db.add_log(conn, "error", "apply", str(exc))
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
