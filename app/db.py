"""SQLite-слой. Одна строка = одна ссылка (батч разворачивается в отдельные заявки)."""

from datetime import datetime, timezone

import aiosqlite

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL CHECK (kind IN ('support', 'article')),
    user_id         INTEGER NOT NULL,
    username        TEXT,
    first_name      TEXT,
    url             TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done')),
    fulfillment_url TEXT,
    created_at      TEXT NOT NULL,
    done_at         TEXT,
    UNIQUE (user_id, kind, url)
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests (status, kind);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def init() -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def add_request(kind: str, user_id: int, username: str | None,
                      first_name: str | None, url: str) -> dict | None:
    """Возвращает созданную заявку или None, если такая ссылка уже была (дедуп)."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "INSERT OR IGNORE INTO requests (kind, user_id, username, first_name, url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, user_id, username, first_name, url, _now()),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        cur = await conn.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,))
        row = await cur.fetchone()
        return dict(row)


async def get_request(request_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_requests(status: str | None = None, kind: str | None = None) -> list[dict]:
    query = "SELECT * FROM requests"
    conditions, params = [], []
    if status:
        conditions.append("status = ?")
        params.append(status)
    if kind:
        conditions.append("kind = ?")
        params.append(kind)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(query, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def mark_done(request_id: int, fulfillment_url: str | None = None) -> dict | None:
    """Атомарно переводит pending → done. None, если заявка не найдена или уже закрыта."""
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "UPDATE requests SET status = 'done', fulfillment_url = ?, done_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (fulfillment_url, _now(), request_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        cur = await conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = await cur.fetchone()
        return dict(row)
