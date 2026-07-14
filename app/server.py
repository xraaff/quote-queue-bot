"""FastAPI: дашборд, API, приём апдейтов Telegram.

Прод (WEBAPP_URL задан): webhook — входящее сообщение само будит уснувший
бесплатный инстанс Render, а Telegram ретраит доставку, пока сервис просыпается.
Локально (WEBAPP_URL пуст): обычный поллинг, публичный URL не нужен.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import db
from .bot import bot, dp, notify_user_done, setup_bot
from .config import ADMIN_ID, BOT_TOKEN, WEBAPP_URL

log = logging.getLogger(__name__)

WEBAPP_HTML = Path(__file__).parent / "webapp" / "index.html"
INIT_DATA_MAX_AGE = 24 * 3600
# Секрет для проверки, что POST /webhook пришёл именно от Telegram
WEBHOOK_SECRET = hashlib.sha256(f"webhook:{BOT_TOKEN}".encode()).hexdigest()[:32]
# Короткоживущий токен для страницы /open (её открывают в Arc, где нет initData)
OPEN_SECRET = hashlib.sha256(f"open:{BOT_TOKEN}".encode()).digest()
OPEN_TOKEN_TTL = 600  # 10 минут


def make_open_token() -> str:
    exp = str(int(time.time()) + OPEN_TOKEN_TTL)
    sig = hmac.new(OPEN_SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def verify_open_token(token: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        return False
    expected = hmac.new(OPEN_SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig) and time.time() < int(exp)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await setup_bot()
    polling: asyncio.Task | None = None
    if WEBAPP_URL:
        await bot.set_webhook(
            f"{WEBAPP_URL.rstrip('/')}/webhook",
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=False,
        )
        log.info("Webhook mode: %s/webhook", WEBAPP_URL)
    else:
        await bot.delete_webhook(drop_pending_updates=False)
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        log.info("Polling mode (local dev)")
    yield
    if polling:
        polling.cancel()
        with suppress(asyncio.CancelledError):
            await polling
    await bot.session.close()
    await db.close()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict:
    if not hmac.compare_digest(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(403, "Bad secret token")
    update = Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}


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


@app.get("/api/broadcasts")
async def api_broadcasts(x_init_data: str = Header(default="")) -> dict:
    validate_admin(x_init_data)
    return {"broadcasts": await db.list_broadcasts()}


@app.get("/api/users")
async def api_users(x_init_data: str = Header(default="")) -> dict:
    validate_admin(x_init_data)
    return {"users": await db.list_users_with_stats()}


@app.post("/api/open-token")
async def api_open_token(x_init_data: str = Header(default="")) -> dict:
    """Дашборд (внутри Telegram) минтит токен, отдаёт ссылку на /open для внешнего браузера."""
    validate_admin(x_init_data)
    base = WEBAPP_URL.rstrip("/")
    return {"url": f"{base}/open?token={make_open_token()}"}


@app.get("/open", response_class=HTMLResponse)
async def open_pending_page(token: str = "") -> HTMLResponse:
    """Страница-хендофф: открывается в Arc, одним кликом раскрывает все pending вкладками."""
    if not verify_open_token(token):
        return HTMLResponse(
            "<h2 style='font:16px system-ui;padding:24px'>Link expired — "
            "reopen it from the dashboard.</h2>", status_code=403)
    rows = await db.list_requests(status="pending")
    urls = [r["url"] for r in rows]
    page = OPEN_PAGE_HTML.replace("__URLS_JSON__", json.dumps(urls))
    return HTMLResponse(page)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


OPEN_PAGE_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Open pending links</title>
<style>
  body { font: 16px/1.5 -apple-system, system-ui, sans-serif; max-width: 640px;
         margin: 0 auto; padding: 24px; color: #18181b; background: #fafafa; }
  h1 { font-size: 20px; }
  button { font-size: 17px; font-weight: 600; padding: 14px 22px; border: none;
           border-radius: 12px; background: #2481cc; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; }
  #hint { display: none; margin-top: 16px; padding: 12px 14px; border-radius: 10px;
          background: #fff3cd; color: #664d03; font-size: 14px; }
  #status { margin-top: 14px; color: #16a34a; font-weight: 600; }
  ul { margin-top: 22px; padding-left: 18px; }
  li { margin-bottom: 8px; word-break: break-all; }
  a { color: #2481cc; }
  .muted { color: #71717a; font-size: 14px; }
</style></head><body>
<h1>🌐 Open <span id="count"></span> pending link(s)</h1>
<p class="muted">Opens each link in a new tab of this browser (Arc).</p>
<button id="go" onclick="openAll()">Open all now</button>
<div id="hint">⚠️ Your browser blocked the extra tabs. Allow pop-ups for this site
  (address-bar icon on the right), then click the button again — all tabs will open.</div>
<div id="status"></div>
<ul id="links"></ul>
<script>
  const urls = __URLS_JSON__;
  document.getElementById('count').textContent = urls.length;
  document.getElementById('links').innerHTML =
    urls.map(u => `<li><a href="${u}" target="_blank" rel="noopener">${u}</a></li>`).join('');
  if (!urls.length) {
    document.getElementById('go').disabled = true;
    document.getElementById('status').textContent = 'Nothing pending 🎉';
  }
  function openAll() {
    let blocked = 0;
    urls.forEach(u => { if (!window.open(u, '_blank')) blocked++; });
    if (blocked > 0) {
      document.getElementById('hint').style.display = 'block';
    } else {
      document.getElementById('status').textContent = `Opened ${urls.length} tab(s) ✅`;
    }
  }
</script>
</body></html>"""
