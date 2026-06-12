"""Привязка аккаунта hh через OAuth и отклики от имени кандидата.

Официальный API: https://api.hh.ru (приложение регистрируется на dev.hh.ru,
redirect_uri = <сайт>/hh/callback). Токены храним в settings.
"""

import time
import urllib.parse

import httpx

from intern_agent import config, db

AUTHORIZE_URL = "https://hh.ru/oauth/authorize"
TOKEN_URL = "https://api.hh.ru/token"
API_BASE = "https://api.hh.ru"


class HHAccountError(Exception):
    """Ошибка работы с аккаунтом hh."""


def _headers(access_token: str | None = None) -> dict:
    headers = {"User-Agent": config.HH_USER_AGENT}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def auth_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "state": state,
            "redirect_uri": redirect_uri,
        }
    )
    return f"{AUTHORIZE_URL}?{params}"


async def _token_request(data: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(TOKEN_URL, data=data, headers=_headers())
    if resp.status_code != 200:
        detail = resp.text[:200].replace("\n", " ")
        raise HHAccountError(f"hh не выдал токен (статус {resp.status_code}: {detail})")
    payload = resp.json()
    if "access_token" not in payload:
        raise HHAccountError("hh вернул ответ без access_token")
    return payload


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    return await _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
    )


async def refresh_token(refresh: str) -> dict:
    return await _token_request({"grant_type": "refresh_token", "refresh_token": refresh})


def save_tokens(conn, payload: dict) -> None:
    db.set_setting(conn, "hh_access_token", payload["access_token"])
    db.set_setting(conn, "hh_refresh_token", payload.get("refresh_token", ""))
    expires = int(time.time()) + int(payload.get("expires_in") or 1209600)
    db.set_setting(conn, "hh_token_expires_ts", str(expires))


def clear_tokens(conn) -> None:
    for key in (
        "hh_access_token",
        "hh_refresh_token",
        "hh_token_expires_ts",
        "hh_account_name",
        "hh_resume_id",
        "hh_resume_title",
    ):
        db.set_setting(conn, key, "")


def token_expired(expires_ts: str, *, margin: int = 300) -> bool:
    try:
        return int(expires_ts) - margin <= time.time()
    except (TypeError, ValueError):
        return True


async def get_valid_token(conn) -> str:
    """Возвращает живой access_token, при необходимости обновляет по refresh."""
    token = db.get_setting(conn, "hh_access_token")
    if not token:
        raise HHAccountError("Аккаунт hh не привязан")
    if not token_expired(db.get_setting(conn, "hh_token_expires_ts")):
        return token
    refresh = db.get_setting(conn, "hh_refresh_token")
    if not refresh:
        raise HHAccountError("Токен hh истёк — привяжи аккаунт заново")
    payload = await refresh_token(refresh)
    save_tokens(conn, payload)
    return payload["access_token"]


async def _get(path: str, token: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{API_BASE}{path}", headers=_headers(token))
    if resp.status_code == 403:
        raise HHAccountError("hh отклонил запрос (403) — проверь права приложения")
    if resp.status_code != 200:
        raise HHAccountError(f"hh вернул статус {resp.status_code} на {path}")
    return resp.json()


async def get_me(token: str) -> dict:
    me = await _get("/me", token)
    name = " ".join(p for p in (me.get("first_name"), me.get("last_name")) if p)
    return {"name": name or me.get("email") or "пользователь hh"}


async def list_resumes(token: str) -> list[dict]:
    payload = await _get("/resumes/mine", token)
    return [
        {"id": item.get("id"), "title": item.get("title") or "Без названия"}
        for item in payload.get("items", [])
        if item.get("id")
    ]


async def apply_to_vacancy(token: str, vacancy_id: str, resume_id: str, message: str) -> None:
    """Отклик на вакансию с сопроводительным письмом."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{API_BASE}/negotiations",
            headers=_headers(token),
            data={"vacancy_id": vacancy_id, "resume_id": resume_id, "message": message},
        )
    if resp.status_code in (200, 201, 204):
        return
    detail = resp.text[:300].replace("\n", " ")
    if resp.status_code == 403 and "already_applied" in detail:
        raise HHAccountError("Отклик на эту вакансию уже отправлен")
    if "test_required" in detail or "questionnaire" in detail:
        raise HHAccountError("Вакансия требует пройти тест на hh — автоотклик невозможен")
    raise HHAccountError(f"hh не принял отклик (статус {resp.status_code}: {detail})")
