"""Конфигурация приложения: всё настраивается через переменные окружения."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# SQLite. На Railway лучше подключить Volume и указать DB_PATH=/data/intern.db,
# иначе трекер откликов обнулится при каждом редеплое.
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "data" / "intern.db"))

STATIC_DIR = Path(os.getenv("STATIC_DIR", BASE_DIR / "static"))

# Gemini API (https://aistudio.google.com). Ключ — только через env, в репо его нет.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "120"))

# hh.kz / hh.ru — официальное открытое API.
HH_API_BASE = "https://api.hh.ru"
HH_USER_AGENT = "intern-agent/0.1 (pet project)"
# Для запасного пути через HTML-страницу (api.hh.ru закрыто для IP дата-центров).
HH_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HH_TIMEOUT = float(os.getenv("HH_TIMEOUT", "20"))

# Статусы воронки откликов.
APPLICATION_STATUSES = ["analyzed", "applied", "reply", "interview", "offer", "rejected"]
