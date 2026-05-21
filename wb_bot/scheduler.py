"""Файл оставлен для обратной совместимости. Фоновый опрос WB удалён —
обновление только по нажатию кнопки «🔄 Обновить с WB» в боте."""
from __future__ import annotations

import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Settings
from .db import DB
from .gemini import GeminiClient

log = logging.getLogger(__name__)


def setup_scheduler(
    bot: Bot, db: DB, gemini: GeminiClient, settings: Settings
) -> AsyncIOScheduler:
    """Пустой scheduler. Сохранён, чтобы bot.py не падал."""
    return AsyncIOScheduler(timezone="UTC")
