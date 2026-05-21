import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_settings
from .db import DB
from .gemini import GeminiClient
from .handlers import router
from .scheduler import setup_scheduler


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    settings = load_settings()
    db = DB(settings.db_path)
    await db.init()

    gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["db"] = db
    dp["gemini"] = gemini
    dp["settings"] = settings
    dp.include_router(router)

    sched = setup_scheduler(bot, db, gemini, settings)
    sched.start()

    try:
        for attempt in range(3):
            try:
                await bot.delete_webhook(drop_pending_updates=True)
                break
            except Exception as e:
                logging.warning("delete_webhook fail (%d/3): %s", attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        await dp.start_polling(bot)
    finally:
        sched.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
