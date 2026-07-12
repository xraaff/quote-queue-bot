import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
# Публичный HTTPS-адрес сервиса (на Render: https://<service>.onrender.com)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
DB_PATH = os.environ.get("DB_PATH", "quotebot.db")
# Postgres (Neon/Supabase). Если пусто — локальный SQLite (только для разработки:
# на бесплатном Render файловая система стирается при каждом рестарте)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Имя, которое видят саппортеры в уведомлениях
ADMIN_NAME = os.environ.get("ADMIN_NAME", "vanvster")
