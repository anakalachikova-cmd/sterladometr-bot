# webhook.py
from telegram.ext import ApplicationBuilder
import os
import asyncio

from bot import main  # ← твой основной код

async def start_bot():
    await main()

if __name__ == "__main__":
    asyncio.run(start_bot())
