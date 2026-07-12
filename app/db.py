"""Слой БД с двумя бэкендами: Postgres (DATABASE_URL, прод на бесплатном Render,
где нет диска) и SQLite (локальная разработка). Одна строка = одна ссылка."""

import re
from datetime import datetime, timezone

from .config import DATABASE_URL, DB_PATH

USE_PG = bool(DATABASE_URL)

if USE_PG:
    import asyncpg
else:
    import aiosqlite

_pool = None  # asyncpg pool (только для PG)

SQLITE_SCHEMA = """
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

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id              SERIAL PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('support', 'article')),
    user_id         BIGINT NOT NULL,
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


def _clean_dsn(dsn: str) -> str:
    # asyncpg не понимает channel_binding из строк подключения Neon
    return re.sub(r"[?&]channel_binding=[^&]*", "", dsn)


async def init() -> None:
    global _pool
    if USE_PG:
        _pool = await asyncpg.create_pool(_clean_dsn(DATABASE_URL), min_size=0, max_size=5)
        async with _pool.acquire() as conn:
            await conn.execute(PG_SCHEMA)
    else:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.executescript(SQLITE_SCHEMA)
            await conn.commit()


async def close() -> None:
    if _pool is not None:
        await _pool.close()


async def add_request(kind: str, user_id: int, username: str | None,
                      first_name: str | None, url: str) -> dict | None:
    """Возвращает созданную заявку или None, если такая ссылка уже была (дедуп)."""
    now = _now()
    if USE_PG:
        row = await _pool.fetchrow(
            "INSERT INTO requests (kind, user_id, username, first_name, url, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6) "
            "ON CONFLICT (user_id, kind, url) DO NOTHING RETURNING *",
            kind, user_id, username, first_name, url, now,
        )
        return dict(row) if row else None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "INSERT OR IGNORE INTO requests (kind, user_id, username, first_name, url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, user_id, username, first_name, url, now),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        cur = await conn.execute("SELECT * FROM requests WHERE id = ?", (cur.lastrowid,))
        return dict(await cur.fetchone())


async def get_request(request_id: int) -> dict | None:
    if USE_PG:
        row = await _pool.fetchrow("SELECT * FROM requests WHERE id = $1", request_id)
        return dict(row) if row else None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_requests(status: str | None = None, kind: str | None = None) -> list[dict]:
    conditions, params = [], []
    for field, value in (("status", status), ("kind", kind)):
        if value:
            params.append(value)
            placeholder = f"${len(params)}" if USE_PG else "?"
            conditions.append(f"{field} = {placeholder}")
    query = "SELECT * FROM requests"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC"
    if USE_PG:
        rows = await _pool.fetch(query, *params)
        return [dict(r) for r in rows]
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(query, params)
        return [dict(r) for r in await cur.fetchall()]


async def mark_done(request_id: int, fulfillment_url: str | None = None) -> dict | None:
    """Атомарно переводит pending → done. None, если заявка не найдена или уже закрыта."""
    now = _now()
    if USE_PG:
        row = await _pool.fetchrow(
            "UPDATE requests SET status = 'done', fulfillment_url = $1, done_at = $2 "
            "WHERE id = $3 AND status = 'pending' RETURNING *",
            fulfillment_url, now, request_id,
        )
        return dict(row) if row else None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "UPDATE requests SET status = 'done', fulfillment_url = ?, done_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (fulfillment_url, now, request_id),
        )
        await conn.commit()
        if cur.rowcount == 0:
            return None
        cur = await conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
        return dict(await cur.fetchone())
