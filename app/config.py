import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
# Публичный HTTPS-адрес сервиса (на Render: https://<service>.onrender.com)
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")
DB_PATH = os.environ.get("DB_PATH", "quotebot.db")
# Имя, которое видят саппортеры в уведомлениях
ADMIN_NAME = os.environ.get("ADMIN_NAME", "vanvster")
