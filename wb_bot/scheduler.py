from __future__ import annotations

import asyncio
import json
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Settings
from .db import DB
from .gemini import GeminiClient, GeminiError
from .handlers import _filter_by_rating, format_push
from .keyboards import push_feedback_kb
from .wb_api import WBClient, WBError

log = logging.getLogger(__name__)


async def auto_answer_cycle(
    bot: Bot, db: DB, gemini: GeminiClient, settings: Settings
) -> None:
    users = await db.list_auto_users()
    log.info("Auto cycle: %d пользователей", len(users))
    for u in users:
        try:
            await _process_user(bot, db, gemini, settings, u)
        except Exception:
            log.exception("Auto cycle failed for user %s", u["user_id"])


async def _process_user(
    bot: Bot, db: DB, gemini: GeminiClient, settings: Settings, u: dict
) -> int:
    user_id = u["user_id"]
    wb = WBClient(u["wb_token"])
    try:
        raw = await wb.get_unanswered(take=settings.batch_size)
    except WBError as e:
        log.warning("WB error for user %s: %s", user_id, e)
        return 0

    feedbacks = _filter_by_rating(raw, u["answer_rating"])
    if not feedbacks:
        return 0

    pushed = 0
    for fb in feedbacks:
        fb_id = fb["id"]
        if await db.is_answered(user_id, fb_id) or await db.is_notified(user_id, fb_id):
            continue

        try:
            answer = await gemini.generate_answer(
                review_text=fb.get("text") or "",
                rating=fb.get("productValuation") or 5,
                product_name=(fb.get("productDetails") or {}).get("productName"),
                tone=u["tone"],
                style=u["style"],
                signature=u["signature"],
            )
        except GeminiError as e:
            log.warning("Gemini failed: %s", e)
            continue

        try:
            await bot.send_message(
                user_id,
                format_push(fb, answer),
                parse_mode="HTML",
                reply_markup=push_feedback_kb(fb_id),
            )
        except Exception:
            log.exception("Не удалось отправить пуш-сообщение user=%s", user_id)
            continue

        await db.add_notified(user_id, fb_id, answer, json.dumps(fb, ensure_ascii=False))
        pushed += 1
        await asyncio.sleep(1.5)  # мягкий троттлинг

    if pushed:
        log.info("Авто-показ: пользователю %s выслано %d новых отзывов", user_id, pushed)
    return pushed


def setup_scheduler(
    bot: Bot, db: DB, gemini: GeminiClient, settings: Settings
) -> AsyncIOScheduler:
    from datetime import datetime, timezone
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(
        auto_answer_cycle,
        "interval",
        minutes=settings.auto_interval_min,
        args=[bot, db, gemini, settings],
        id="auto_answer",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    return sched


async def run_user_check(
    bot: Bot, db: DB, gemini: GeminiClient, settings: Settings, user_id: int
) -> int:
    """Сразу проверить отзывы для одного пользователя. Возвращает кол-во пушей."""
    u = await db.get_user(user_id)
    if not u or not u["wb_token"]:
        return 0
    try:
        return await _process_user(bot, db, gemini, settings, u)
    except Exception:
        log.exception("Manual check failed for user %s", user_id)
        return 0
