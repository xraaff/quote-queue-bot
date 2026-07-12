"""Telegram-бот: приём ссылок от саппортеров + эфир и inline-действия для админа."""

import asyncio
import html
import logging
import re
from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)

from . import db
from .config import ADMIN_ID, ADMIN_NAME, BOT_TOKEN, PROFILE_URL, WEBAPP_URL

log = logging.getLogger(__name__)

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
dp = Dispatcher()
router = Router()
dp.include_router(router)

URL_RE = re.compile(r"https?://[^\s<>\"']+")

KIND_LABELS = {"support": "Support my quotes", "article": "Quote my article"}
KIND_EMOJI = {"support": "💬", "article": "📰"}


def extract_urls(text: str) -> list[str]:
    urls, seen = [], set()
    for raw in URL_RE.findall(text or ""):
        url = raw.rstrip(".,;:!?)»”'\"")
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


class Submit(StatesGroup):
    waiting_links = State()


class AdminReply(StatesGroup):
    waiting_quote_link = State()


class AdminPush(StatesGroup):
    waiting_headline = State()
    confirm = State()


PUSH_HEADERS = {
    "quote": "🔥 <b>NEW QUOTE JUST DROPPED</b> 🔥",
    "article": "📰 <b>NEW ARTICLE IS OUT</b> 📰",
}
PUSH_KIND_WORD = {"quote": "quote", "article": "article"}


def build_push_text(kind: str, headline: str) -> str:
    return "\n".join([
        PUSH_HEADERS[kind],
        "",
        f"<b>{ADMIN_NAME}</b> just published a new {PUSH_KIND_WORD[kind]} — go show it some love:",
        "❤️ Like  ·  🔖 Bookmark  ·  💬 Comment",
        "",
        f"📌 Headline of new {PUSH_KIND_WORD[kind]}:",
        f"<b>«{html.escape(headline)}»</b>",
        "",
        "When you're done — smash the button below 👇",
    ])


def push_kb(broadcast_id: int, supported: bool = False) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="👤 Open profile", url=PROFILE_URL)]]
    if not supported:
        rows.append([InlineKeyboardButton(text="✅ I supported it", callback_data=f"bsup:{broadcast_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Support my quotes", callback_data="pick:support")],
        [InlineKeyboardButton(text="📰 Quote my article", callback_data="pick:article")],
    ])


def admin_ping_kb(kind: str, request_id: int) -> InlineKeyboardMarkup:
    if kind == "support":
        button = InlineKeyboardButton(text="✅ Supported", callback_data=f"adone:{request_id}")
    else:
        button = InlineKeyboardButton(text="✅ Quoted (кинуть ссылку)", callback_data=f"aquote:{request_id}")
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def user_label(row: dict) -> str:
    name = html.escape(row.get("first_name") or "")
    if row.get("username"):
        return f'{name} (@{row["username"]})'.strip()
    return name or str(row["user_id"])


# ---------- сторона саппортера ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if message.from_user.id == ADMIN_ID:
        rows = []
        if WEBAPP_URL:
            rows.append([InlineKeyboardButton(text="📊 Dashboard", web_app=WebAppInfo(url=WEBAPP_URL))])
        rows.append([InlineKeyboardButton(text="📣 New push", callback_data="push:new")])
        pending = await db.list_requests(status="pending")
        await message.answer(
            f"Админ-режим. В очереди: <b>{len(pending)}</b>.\n"
            "Новые заявки будут падать сюда эфиром, разобранный список — в дашборде.\n"
            "📣 New push — разослать всем свой новый квот/артикл.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        "Yo! Choose what you want:\n\n"
        "💬 <b>Support my quotes</b> — send link(s) to your quote tweet(s)\n"
        "📰 <b>Quote my article</b> — send a link to your article",
        reply_markup=main_menu_kb(),
    )


async def file_links(kind: str, user, urls: list[str], reply_to: Message) -> None:
    """Записывает ссылки в базу, шлёт эфир админу и подтверждение юзеру.
    user передаётся явно: в callback'ах message.from_user — это бот, а не человек."""
    await db.upsert_user(user.id, user.username, user.first_name)
    added, duplicates = [], 0
    for url in urls:
        row = await db.add_request(kind, user.id, user.username, user.first_name, url)
        if row is None:
            duplicates += 1
        else:
            added.append(row)

    for row in added:
        await notify_admin_new(row)

    parts = []
    if added:
        parts.append(f"✅ Got it — {len(added)} link(s) in the queue.")
    if duplicates:
        parts.append(f"↩️ {duplicates} already submitted earlier, skipped.")
    parts.append("You'll get a ping when it's done. Send more links or pick another option:")
    await reply_to.answer("\n".join(parts), reply_markup=main_menu_kb())


@router.callback_query(F.data.startswith("pick:"))
async def pick_kind(callback: CallbackQuery, state: FSMContext) -> None:
    kind = callback.data.split(":", 1)[1]
    data = await state.get_data()
    pending_urls = data.get("pending_urls")
    await state.set_state(Submit.waiting_links)
    await state.update_data(kind=kind, pending_urls=None)
    if pending_urls:
        # ссылки прислали до выбора типа — записываем сразу, второй раз кидать не надо
        await callback.message.edit_reply_markup(reply_markup=None)
        await file_links(kind, callback.from_user, pending_urls, callback.message)
        await callback.answer("Saved ✅")
        return
    if kind == "support":
        prompt = ("Drop the link(s) to your quote tweet(s) — "
                  "one message, multiple links are fine 👇")
    else:
        prompt = "Drop the link to your article (or several) 👇"
    await callback.message.edit_text(f"{KIND_EMOJI[kind]} <b>{KIND_LABELS[kind]}</b>\n\n{prompt}")
    await callback.answer()


@router.message(Submit.waiting_links, F.text | F.caption)
async def receive_links(message: Message, state: FSMContext) -> None:
    urls = extract_urls(message.text or message.caption or "")
    if not urls:
        await message.answer("I don't see a link 🤔 Send it starting with http(s)://")
        return
    data = await state.get_data()
    await file_links(data["kind"], message.from_user, urls, message)


# ---------- сторона админа ----------

async def notify_admin_new(row: dict) -> None:
    text = (
        f"{KIND_EMOJI[row['kind']]} <b>{KIND_LABELS[row['kind']]}</b> #{row['id']}\n"
        f"От: {user_label(row)}\n"
        f"{html.escape(row['url'])}"
    )
    try:
        await bot.send_message(ADMIN_ID, text, reply_markup=admin_ping_kb(row["kind"], row["id"]))
    except Exception:
        log.exception("Failed to notify admin about request %s", row["id"])


async def notify_user_done(row: dict) -> None:
    """Уведомление саппортеру после done. Не роняет вызывающего, если юзер заблокировал бота."""
    if row["kind"] == "support":
        text = f"✅ <b>{ADMIN_NAME}</b> supported your quote\n{html.escape(row['url'])}"
    else:
        link = row.get("fulfillment_url") or row["url"]
        text = f"✅ <b>{ADMIN_NAME}</b> quoted your article\n{html.escape(link)}"
    try:
        await bot.send_message(row["user_id"], text)
    except Exception:
        log.exception("Failed to notify user %s for request %s", row["user_id"], row["id"])


@router.callback_query(F.data.startswith("adone:"))
async def admin_done_support(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not for you 😉", show_alert=True)
        return
    request_id = int(callback.data.split(":", 1)[1])
    row = await db.mark_done(request_id)
    if row is None:
        await callback.answer("Уже закрыта (видимо, через дашборд).")
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await notify_user_done(row)
    await callback.message.edit_text(callback.message.html_text + "\n\n✅ <b>done</b>, юзер уведомлён")
    await callback.answer("Done ✅")


@router.callback_query(F.data.startswith("aquote:"))
async def admin_quote_article(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not for you 😉", show_alert=True)
        return
    request_id = int(callback.data.split(":", 1)[1])
    row = await db.get_request(request_id)
    if row is None or row["status"] == "done":
        await callback.answer("Уже закрыта.")
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await state.set_state(AdminReply.waiting_quote_link)
    await state.update_data(request_id=request_id)
    await callback.message.answer(
        f"Кинь ссылку на твой квот для заявки #{request_id} — её и отправлю юзеру. /cancel — отмена."
    )
    await callback.answer()


@router.message(AdminReply.waiting_quote_link, Command("cancel"))
async def admin_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.")


@router.message(AdminReply.waiting_quote_link, F.text)
async def admin_receive_quote_link(message: Message, state: FSMContext) -> None:
    urls = extract_urls(message.text)
    if not urls:
        await message.answer("Не вижу ссылки. Кинь http(s)://… или /cancel.")
        return
    data = await state.get_data()
    await state.clear()
    row = await db.mark_done(data["request_id"], fulfillment_url=urls[0])
    if row is None:
        await message.answer("Заявка уже была закрыта — уведомление не дублирую.")
        return
    await notify_user_done(row)
    await message.answer(f"✅ #{row['id']} закрыта, юзер получил ссылку на квот.")


# ---------- пуши: админ рассылает свой новый квот/артикл ----------

@router.callback_query(F.data == "push:new")
async def push_new(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not for you 😉", show_alert=True)
        return
    await callback.message.answer(
        "Что рассылаем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 My quote", callback_data="push:kind:quote")],
            [InlineKeyboardButton(text="📰 My article", callback_data="push:kind:article")],
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("push:kind:"))
async def push_pick_kind(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not for you 😉", show_alert=True)
        return
    kind = callback.data.rsplit(":", 1)[1]
    await state.set_state(AdminPush.waiting_headline)
    await state.update_data(push_kind=kind)
    await callback.message.edit_text(
        f"{KIND_EMOJI['support' if kind == 'quote' else 'article']} "
        f"Напиши заголовок твоего {PUSH_KIND_WORD[kind]} — он попадёт в рассылку. /cancel — отмена."
    )
    await callback.answer()


@router.message(AdminPush.waiting_headline, Command("cancel"))
@router.message(AdminPush.confirm, Command("cancel"))
async def push_cancel_cmd(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Ок, отменил.")


@router.message(AdminPush.waiting_headline, F.text)
async def push_receive_headline(message: Message, state: FSMContext) -> None:
    headline = message.text.strip()
    if not headline or headline.startswith("/"):
        await message.answer("Напиши текст заголовка или /cancel.")
        return
    data = await state.get_data()
    kind = data["push_kind"]
    await state.set_state(AdminPush.confirm)
    await state.update_data(push_headline=headline)

    recipients = [u for u in await db.list_users() if u["user_id"] != ADMIN_ID]
    # превью: ровно то, что увидят люди (id=0 — черновик, кнопка недействующая)
    await message.answer(build_push_text(kind, headline), reply_markup=push_kb(0))
    await message.answer(
        f"☝️ Превью. Отправить <b>{len(recipients)}</b> людям?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"🚀 Send to {len(recipients)}", callback_data="push:send"),
            InlineKeyboardButton(text="❌ Cancel", callback_data="push:cancel"),
        ]]),
    )


@router.callback_query(F.data == "push:cancel")
async def push_cancel_cb(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.callback_query(F.data == "push:send", AdminPush.confirm)
async def push_send(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Not for you 😉", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    kind, headline = data["push_kind"], data["push_headline"]
    broadcast = await db.create_broadcast(kind, PROFILE_URL, headline)
    text = build_push_text(kind, headline)

    recipients = [u for u in await db.list_users() if u["user_id"] != ADMIN_ID]
    await callback.message.edit_text(f"📤 Рассылаю {len(recipients)} людям…")
    sent = failed = 0
    for u in recipients:
        try:
            await bot.send_message(u["user_id"], text,
                                   reply_markup=push_kb(broadcast["id"]))
            status = "sent"
            sent += 1
        except Exception:
            status = "failed"
            failed += 1
        await db.save_receipt(broadcast["id"], u["user_id"], u["username"], u["first_name"], status)
        await asyncio.sleep(0.1)  # лимит Telegram ~30 msg/sec, не упираемся

    report = f"📣 Push #{broadcast['id']} отправлен: ✅ {sent}"
    if failed:
        report += f", 🚫 {failed} недоступны (заблокировали бота)"
    report += "\nКто просапортил — смотри на странице Pushes в дашборде."
    await callback.message.edit_text(report)
    await callback.answer("Sent 🚀")


@router.callback_query(F.data.startswith("bsup:"))
async def push_supported(callback: CallbackQuery) -> None:
    # старые сообщения могут иметь формат bsup:{id}:{kind} — берём только id
    broadcast_id = int(callback.data.split(":")[1])
    counted = await db.mark_supported(broadcast_id, callback.from_user.id)
    if not counted:
        await callback.answer("Already counted ✅")
        return
    with suppress(Exception):
        await callback.message.edit_text(
            callback.message.html_text + "\n\n✅ <b>Thanks for the support! You're a legend 🙌</b>",
            reply_markup=push_kb(broadcast_id, supported=True),
        )
    await callback.answer("Counted! Thank you 🙏")


# Catch-all: регистрируется ПОСЛЕДНИМ, ловит всё, что не поймали хендлеры выше.
# Главная страховка от потери ссылок: юзер кинул ссылку, не нажав кнопку, —
# запоминаем её и просим только выбрать тип, повторно кидать не нужно.
@router.message(F.text | F.caption)
async def catch_all(message: Message, state: FSMContext) -> None:
    if message.from_user.id == ADMIN_ID:
        await message.answer("Очередь и действия — в дашборде или через кнопки в эфире. /start — меню.")
        return
    await db.upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    urls = extract_urls(message.text or message.caption or "")
    if urls:
        await state.update_data(pending_urls=urls)
        await message.answer(
            f"Got your link{'s' if len(urls) > 1 else ''} 👌 What is it?",
            reply_markup=main_menu_kb(),
        )
    else:
        await message.answer("Pick what you want or just drop a link:", reply_markup=main_menu_kb())


async def setup_bot() -> None:
    """Команды и кнопка-меню с дашбордом (только в чате админа)."""
    await bot.set_my_commands([BotCommand(command="start", description="Menu")])
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                chat_id=ADMIN_ID,
                menu_button=MenuButtonWebApp(text="Dashboard", web_app=WebAppInfo(url=WEBAPP_URL)),
            )
        except Exception:
            # чат с админом ещё не существует, пока он не нажал Start — не критично
            log.warning("Could not set admin menu button (admin hasn't started the bot yet?)")
