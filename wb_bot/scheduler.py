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
from .wb_api import WBClient, WBError, WBRateLimited

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
) -> dict:
    user_id = u["user_id"]
    stats = {"total": 0, "filtered_out": 0, "already_seen": 0, "pushed": 0, "error": None}
    wb = WBClient(u["wb_token"])
    left = wb.cooldown_left()
    if left > 0:
        stats["error"] = f"rate_limit: ждём ещё {left} сек"
        return stats
    try:
        raw = await wb.get_unanswered(take=settings.batch_size)
    except WBRateLimited as e:
        log.info("user %s: WB лимит, пропускаем цикл", user_id)
        stats["error"] = f"rate_limit: {e}"
        return stats
    except WBError as e:
        log.warning("WB error for user %s: %s", user_id, e)
        stats["error"] = str(e)
        return stats

    stats["total"] = len(raw)
    feedbacks = _filter_by_rating(raw, u["answer_rating"])
    stats["filtered_out"] = len(raw) - len(feedbacks)
    if not feedbacks:
        return stats

    for fb in feedbacks:
        fb_id = fb["id"]
        if await db.is_answered(user_id, fb_id) or await db.is_notified(user_id, fb_id):
            stats["already_seen"] += 1
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
        stats["pushed"] += 1
        await asyncio.sleep(1.5)

    if stats["pushed"]:
        log.info("Авто-показ: user=%s pushed=%d", user_id, stats["pushed"])
    return stats


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
) -> dict:
    """Сразу проверить отзывы. Возвращает статистику цикла."""
    u = await db.get_user(user_id)
    if not u or not u["wb_token"]:
        return {"error": "no_token"}
    try:
        return await _process_user(bot, db, gemini, settings, u)
    except Exception as e:
        log.exception("Manual check failed for user %s", user_id)
        return {"error": str(e)}
