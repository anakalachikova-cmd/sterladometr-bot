# wsgi.py

from telegram.ext import ApplicationBuilder
import os
import asyncio

# Импортируем твой бот
from bot import main  # ← замени на имя файла с твоим кодом

# Запускаем бота в фоне
async def start_bot():
    await main()

# Обёртка для WSGI
def application(environ, start_response):
    # Всё, что нужно — запустить бота
    asyncio.get_event_loop().run_until_complete(start_bot())
    return [], start_response("200 OK", [("Content-Type", "text/plain")])

# Это необходимо для gunicorn
application = application
