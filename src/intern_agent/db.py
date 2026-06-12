"""SQLite-хранилище: резюме (одна активная запись) и трекер откликов."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from intern_agent import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS resume (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    content TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    url TEXT,
    company TEXT,
    position TEXT,
    vacancy_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'analyzed',
    match_score INTEGER,
    verdict TEXT,
    matched_json TEXT,
    missing_json TEXT,
    recommendations_json TEXT,
    tailored_resume TEXT,
    cover_letter_ru TEXT,
    cover_letter_en TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vacancy_id TEXT NOT NULL UNIQUE,
    url TEXT,
    position TEXT,
    company TEXT,
    salary TEXT,
    score INTEGER,
    reason TEXT,
    vacancy_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# ---------- резюме ----------


def save_resume(conn: sqlite3.Connection, content: str) -> None:
    conn.execute(
        """INSERT INTO resume (id, content, updated_at) VALUES (1, ?, ?)
           ON CONFLICT(id) DO UPDATE SET content = excluded.content,
                                         updated_at = excluded.updated_at""",
        (content, _now()),
    )
    conn.commit()


def get_resume(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT content, updated_at FROM resume WHERE id = 1").fetchone()
    return dict(row) if row else None


# ---------- отклики ----------


def insert_application(conn: sqlite3.Connection, app: dict) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO applications
           (source, url, company, position, vacancy_text, status, match_score, verdict,
            matched_json, missing_json, recommendations_json,
            tailored_resume, cover_letter_ru, cover_letter_en, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            app.get("source", "manual"),
            app.get("url"),
            app.get("company"),
            app.get("position"),
            app.get("vacancy_text", ""),
            app.get("status", "analyzed"),
            app.get("match_score"),
            app.get("verdict"),
            json.dumps(app.get("matched", []), ensure_ascii=False),
            json.dumps(app.get("missing", []), ensure_ascii=False),
            json.dumps(app.get("recommendations", []), ensure_ascii=False),
            app.get("tailored_resume"),
            app.get("cover_letter_ru"),
            app.get("cover_letter_en"),
            now,
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _row_to_application(row: sqlite3.Row, full: bool = False) -> dict:
    item = {
        "id": row["id"],
        "source": row["source"],
        "url": row["url"],
        "company": row["company"],
        "position": row["position"],
        "status": row["status"],
        "match_score": row["match_score"],
        "verdict": row["verdict"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if full:
        item.update(
            {
                "vacancy_text": row["vacancy_text"],
                "matched": json.loads(row["matched_json"] or "[]"),
                "missing": json.loads(row["missing_json"] or "[]"),
                "recommendations": json.loads(row["recommendations_json"] or "[]"),
                "tailored_resume": row["tailored_resume"],
                "cover_letter_ru": row["cover_letter_ru"],
                "cover_letter_en": row["cover_letter_en"],
            }
        )
    return item


def list_applications(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    if status:
        rows = conn.execute(
            "SELECT * FROM applications WHERE status = ? ORDER BY id DESC", (status,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM applications ORDER BY id DESC").fetchall()
    return [_row_to_application(r) for r in rows]


def get_application(conn: sqlite3.Connection, app_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    return _row_to_application(row, full=True) if row else None


def update_status(conn: sqlite3.Connection, app_id: int, status: str) -> bool:
    cur = conn.execute(
        "UPDATE applications SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now(), app_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_application(conn: sqlite3.Connection, app_id: int) -> bool:
    cur = conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    conn.commit()
    return cur.rowcount > 0


def stats_by_status(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
    ).fetchall()
    counts = {s: 0 for s in config.APPLICATION_STATUSES}
    for row in rows:
        counts[row["status"]] = row["n"]
    counts["total"] = sum(counts.values())
    return counts


# ---------- настройки поиска ----------


def get_search_queries(conn: sqlite3.Connection) -> list[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = 'queries'").fetchone()
    if not row:
        return list(config.DEFAULT_SEARCH_QUERIES)
    try:
        queries = json.loads(row["value"])
    except json.JSONDecodeError:
        return list(config.DEFAULT_SEARCH_QUERIES)
    return [q for q in queries if isinstance(q, str) and q.strip()]


def save_search_queries(conn: sqlite3.Connection, queries: list[str]) -> None:
    conn.execute(
        """INSERT INTO settings (key, value) VALUES ('queries', ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (json.dumps(queries, ensure_ascii=False),),
    )
    conn.commit()


# ---------- лента вакансий ----------


def feed_known_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT vacancy_id FROM feed").fetchall()
    return {row["vacancy_id"] for row in rows}


def insert_feed_item(conn: sqlite3.Connection, item: dict) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO feed
           (vacancy_id, url, position, company, salary, score, reason, vacancy_text,
            status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)""",
        (
            item["vacancy_id"],
            item.get("url"),
            item.get("position"),
            item.get("company"),
            item.get("salary"),
            item.get("score"),
            item.get("reason"),
            item.get("vacancy_text", ""),
            _now(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def list_feed(conn: sqlite3.Connection, status: str = "new") -> list[dict]:
    rows = conn.execute(
        """SELECT id, vacancy_id, url, position, company, salary, score, reason,
                  status, created_at
           FROM feed WHERE status = ? ORDER BY score DESC, id DESC""",
        (status,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_feed_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM feed WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def set_feed_status(conn: sqlite3.Connection, item_id: int, status: str) -> bool:
    cur = conn.execute("UPDATE feed SET status = ? WHERE id = ?", (status, item_id))
    conn.commit()
    return cur.rowcount > 0
