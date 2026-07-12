"""FastAPI: отдаёт вебвью-дашборд, API для него и держит поллинг бота в фоне."""

import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qsl

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import db
from .bot import bot, dp, notify_user_done, setup_bot
from .config import ADMIN_ID, BOT_TOKEN

WEBAPP_HTML = Path(__file__).parent / "webapp" / "index.html"
INIT_DATA_MAX_AGE = 24 * 3600


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await setup_bot()
    polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    yield
    polling.cancel()
    with suppress(asyncio.CancelledError):
        await polling
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


def validate_admin(init_data: str) -> None:
    """Проверка подписи Telegram WebApp initData + что открыл именно админ."""
    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "No initData hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Bad initData signature")
    if time.time() - int(parsed.get("auth_date", 0)) > INIT_DATA_MAX_AGE:
        raise HTTPException(401, "initData expired, reopen the dashboard")
    user = json.loads(parsed.get("user", "{}"))
    if user.get("id") != ADMIN_ID:
        raise HTTPException(403, "Admins only")


@app.get("/", response_class=HTMLResponse)
async def webapp() -> str:
    return WEBAPP_HTML.read_text(encoding="utf-8")


@app.get("/api/requests")
async def api_list(status: str | None = None, kind: str | None = None,
                   x_init_data: str = Header(default="")) -> dict:
    validate_admin(x_init_data)
    rows = await db.list_requests(status=status, kind=kind)
    pending = await db.list_requests(status="pending")
    return {"requests": rows, "pending_count": len(pending)}


class DoneBody(BaseModel):
    fulfillment_url: str | None = None


@app.post("/api/requests/{request_id}/done")
async def api_done(request_id: int, body: DoneBody,
                   x_init_data: str = Header(default="")) -> dict:
    validate_admin(x_init_data)
    existing = await db.get_request(request_id)
    if existing is None:
        raise HTTPException(404, "Request not found")
    if existing["kind"] == "article" and not body.fulfillment_url:
        raise HTTPException(422, "Article requests need your quote link")
    row = await db.mark_done(request_id, fulfillment_url=body.fulfillment_url)
    if row is None:
        raise HTTPException(409, "Already done")
    await notify_user_done(row)
    return {"request": row}


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
