# Quote Queue Bot

Двухсторонний Telegram-бот для сбора ссылок от саппортеров.

**Сторона саппортера:** жмёт `/start` → выбирает кнопку → кидает ссылку (или батч в одном сообщении) → получает уведомление, когда ты закрыл заявку:
- 💬 `Support my quotes` → «✅ vanvster supported your quote» + ссылка на его квот
- 📰 `Quote my article` → «✅ vanvster quoted your article» + ссылка на твой квот

**Твоя сторона:** каждая заявка падает тебе в личку эфиром с кнопкой Done прямо в сообщении + вебвью-дашборд (Telegram WebApp, доступ только по твоему `ADMIN_ID`) со списком pending/done и фильтрами.

## Структура

```
app/
  config.py          # env-переменные
  db.py              # SQLite (aiosqlite): заявки, дедуп, статусы
  bot.py             # aiogram: флоу юзера, эфир, inline done-кнопки
  server.py          # FastAPI: дашборд, API, валидация initData, поллинг бота в фоне
  webapp/index.html  # вебвью-дашборд
render.yaml          # blueprint для Render
```

Один процесс: uvicorn поднимает FastAPI, а поллинг бота стартует фоновой задачей в lifespan. Одна строка в БД = одна ссылка (батч разворачивается), дедуп по `(user_id, тип, url)`.

## Деплой на Render

1. Создай бота у [@BotFather](https://t.me/BotFather) → возьми токен. Узнай свой user id у [@userinfobot](https://t.me/userinfobot).
2. Запушь этот репозиторий на GitHub.
3. Render → **New → Blueprint** → выбери репозиторий (подхватит `render.yaml`).
4. Заполни env vars: `BOT_TOKEN`, `ADMIN_ID`, а `WEBAPP_URL` — адрес сервиса, который Render выдаст (`https://quotebot-xxxx.onrender.com`). Его видно после первого деплоя — впиши и передеплой.
5. Нажми `/start` у бота со своего аккаунта — появится кнопка **📊 Dashboard** и меню-кнопка в чате.

⚠️ Нужен план **Starter** (~$7/мес): на free-плане нет персистентного диска (SQLite сотрётся при каждом деплое) и сервис засыпает через 15 минут — бот перестанет принимать сообщения.

## Локальный запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполни BOT_TOKEN и ADMIN_ID
uvicorn app.server:app --port 8000
```

Бот и эфир работают сразу (поллинг не требует публичного URL). Дашборд изнутри Telegram требует HTTPS: для локального теста подними туннель (`ngrok http 8000`) и впиши его адрес в `WEBAPP_URL`.

## Как пользоваться

- Твит вышел → саппортеры кидают ссылки боту, тебе всё падает эфиром.
- Лайкнул/ретвитнул квот → жми **✅ Supported** в сообщении или в дашборде — человеку уйдёт уведомление.
- Заквотил статью → жми **✅ Quoted**, бот попросит ссылку на твой квот — её и отправит автору.
- Дашборд: меню-кнопка в чате с ботом → вкладки Pending/Done, фильтры по типу.
