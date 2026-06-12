"""Аутентификация: пароль (PBKDF2) + сессии в httpOnly-cookie."""

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from intern_agent import db

COOKIE_NAME = "ia_session"
SESSION_DAYS = 30
_ITERATIONS = 200_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Возвращает строку 'salt$hash' (hex)."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def password_is_set(conn: sqlite3.Connection) -> bool:
    return bool(db.get_setting(conn, "password_hash"))


def set_password(conn: sqlite3.Connection, password: str) -> None:
    db.set_setting(conn, "password_hash", hash_password(password))


def check_password(conn: sqlite3.Connection, password: str) -> bool:
    stored = db.get_setting(conn, "password_hash")
    return bool(stored) and verify_password(password, stored)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection) -> str:
    """Создаёт сессию, возвращает токен для cookie."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    db.insert_session(conn, _token_hash(token), expires.isoformat(timespec="seconds"))
    return token


def session_valid(conn: sqlite3.Connection, token: str | None) -> bool:
    return bool(token) and db.session_valid(conn, _token_hash(token))


def drop_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        db.delete_session(conn, _token_hash(token))
