from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    gemini_api_key: str
    gemini_model: str
    admin_ids: frozenset[int]
    auto_interval_min: int
    batch_size: int
    db_path: str


def _parse_admins(raw: str) -> frozenset[int]:
    if not raw:
        return frozenset()
    return frozenset(int(x.strip()) for x in raw.split(",") if x.strip())


def load_settings() -> Settings:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_key:
        raise RuntimeError("GEMINI_API_KEY не задан в .env")

    return Settings(
        bot_token=bot_token,
        gemini_api_key=gemini_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        admin_ids=_parse_admins(os.getenv("ADMIN_IDS", "")),
        auto_interval_min=int(os.getenv("AUTO_INTERVAL_MIN", "30")),
        batch_size=int(os.getenv("BATCH_SIZE", "20")),
        db_path=os.getenv("DB_PATH", "bot.db"),
    )
