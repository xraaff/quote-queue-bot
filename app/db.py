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
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('quote', 'article')),
    url        TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcast_receipts (
    broadcast_id INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    username     TEXT,
    first_name   TEXT,
    status       TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'supported', 'failed')),
    supported_at TEXT,
    PRIMARY KEY (broadcast_id, user_id)
);
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
CREATE TABLE IF NOT EXISTS users (
    user_id    BIGINT PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_seen  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id         SERIAL PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('quote', 'article')),
    url        TEXT NOT NULL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS broadcast_receipts (
    broadcast_id INTEGER NOT NULL,
    user_id      BIGINT NOT NULL,
    username     TEXT,
    first_name   TEXT,
    status       TEXT NOT NULL DEFAULT 'sent' CHECK (status IN ('sent', 'supported', 'failed')),
    supported_at TEXT,
    PRIMARY KEY (broadcast_id, user_id)
);
"""

# все, кто когда-либо кидал заявки до появления таблицы users, тоже должны получать пуши
BACKFILL_USERS_PG = """
INSERT INTO users (user_id, username, first_name, last_seen)
SELECT DISTINCT ON (user_id) user_id, username, first_name, created_at
FROM requests ORDER BY user_id, created_at DESC
ON CONFLICT (user_id) DO NOTHING
"""
BACKFILL_USERS_SQLITE = """
INSERT OR IGNORE INTO users (user_id, username, first_name, last_seen)
SELECT user_id, username, first_name, MAX(created_at)
FROM requests GROUP BY user_id
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
            await conn.execute(BACKFILL_USERS_PG)
    else:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.executescript(SQLITE_SCHEMA)
            await conn.execute(BACKFILL_USERS_SQLITE)
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


# ---------- пуши (рассылки) ----------

async def upsert_user(user_id: int, username: str | None, first_name: str | None) -> None:
    now = _now()
    if USE_PG:
        await _pool.execute(
            "INSERT INTO users (user_id, username, first_name, last_seen) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id) DO UPDATE SET username = $2, first_name = $3, last_seen = $4",
            user_id, username, first_name, now)
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO users (user_id, username, first_name, last_seen) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET username = excluded.username, "
            "first_name = excluded.first_name, last_seen = excluded.last_seen",
            (user_id, username, first_name, now))
        await conn.commit()


async def list_users() -> list[dict]:
    if USE_PG:
        return [dict(r) for r in await _pool.fetch("SELECT * FROM users")]
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM users")
        return [dict(r) for r in await cur.fetchall()]


async def create_broadcast(kind: str, url: str, note: str | None) -> dict:
    now = _now()
    if USE_PG:
        row = await _pool.fetchrow(
            "INSERT INTO broadcasts (kind, url, note, created_at) VALUES ($1, $2, $3, $4) RETURNING *",
            kind, url, note, now)
        return dict(row)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "INSERT INTO broadcasts (kind, url, note, created_at) VALUES (?, ?, ?, ?)",
            (kind, url, note, now))
        await conn.commit()
        cur = await conn.execute("SELECT * FROM broadcasts WHERE id = ?", (cur.lastrowid,))
        return dict(await cur.fetchone())


async def save_receipt(broadcast_id: int, user_id: int, username: str | None,
                       first_name: str | None, status: str) -> None:
    if USE_PG:
        await _pool.execute(
            "INSERT INTO broadcast_receipts (broadcast_id, user_id, username, first_name, status) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (broadcast_id, user_id) DO NOTHING",
            broadcast_id, user_id, username, first_name, status)
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO broadcast_receipts (broadcast_id, user_id, username, first_name, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (broadcast_id, user_id, username, first_name, status))
        await conn.commit()


async def mark_supported(broadcast_id: int, user_id: int) -> bool:
    """True, если отметка засчитана; False, если уже была или пуш ему не отправлялся."""
    now = _now()
    if USE_PG:
        result = await _pool.execute(
            "UPDATE broadcast_receipts SET status = 'supported', supported_at = $1 "
            "WHERE broadcast_id = $2 AND user_id = $3 AND status = 'sent'",
            now, broadcast_id, user_id)
        return result.endswith("1")
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE broadcast_receipts SET status = 'supported', supported_at = ? "
            "WHERE broadcast_id = ? AND user_id = ? AND status = 'sent'",
            (now, broadcast_id, user_id))
        await conn.commit()
        return cur.rowcount == 1


async def list_broadcasts() -> list[dict]:
    """Пуши новыми вперёд, каждый со своими отметками (для страницы Pushes)."""
    if USE_PG:
        broadcasts = [dict(r) for r in await _pool.fetch(
            "SELECT * FROM broadcasts ORDER BY id DESC")]
        receipts = [dict(r) for r in await _pool.fetch(
            "SELECT * FROM broadcast_receipts ORDER BY status DESC, supported_at")]
    else:
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM broadcasts ORDER BY id DESC")
            broadcasts = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute(
                "SELECT * FROM broadcast_receipts ORDER BY status DESC, supported_at")
            receipts = [dict(r) for r in await cur.fetchall()]
    by_id = {b["id"]: b for b in broadcasts}
    for b in broadcasts:
        b["receipts"] = []
    for r in receipts:
        parent = by_id.get(r["broadcast_id"])
        if parent is not None:
            parent["receipts"].append(r)
    return broadcasts
