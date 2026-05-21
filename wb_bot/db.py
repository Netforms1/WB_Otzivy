from __future__ import annotations

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    wb_token       TEXT,
    ozon_client_id TEXT,
    ozon_api_key   TEXT,
    tone           TEXT NOT NULL DEFAULT 'friendly',
    style          TEXT NOT NULL DEFAULT 'medium',
    signature      TEXT,
    auto_enabled   INTEGER NOT NULL DEFAULT 0,
    auto_send      INTEGER NOT NULL DEFAULT 0,
    answer_rating  TEXT NOT NULL DEFAULT 'all',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS answered (
    user_id     INTEGER NOT NULL,
    feedback_id TEXT NOT NULL,
    answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, feedback_id)
);

CREATE TABLE IF NOT EXISTS notified (
    user_id     INTEGER NOT NULL,
    feedback_id TEXT NOT NULL,
    answer      TEXT,
    fb_json     TEXT,
    kind        TEXT NOT NULL DEFAULT 'feedback',
    source      TEXT NOT NULL DEFAULT 'wb',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, source, feedback_id)
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            # Миграция для старых БД
            try:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN auto_send INTEGER NOT NULL DEFAULT 0"
                )
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute(
                    "ALTER TABLE notified ADD COLUMN kind TEXT NOT NULL DEFAULT 'feedback'"
                )
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute(
                    "ALTER TABLE notified ADD COLUMN source TEXT NOT NULL DEFAULT 'wb'"
                )
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE users ADD COLUMN ozon_client_id TEXT")
            except aiosqlite.OperationalError:
                pass
            try:
                await db.execute("ALTER TABLE users ADD COLUMN ozon_api_key TEXT")
            except aiosqlite.OperationalError:
                pass
            await db.commit()

    async def ensure_user(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,)
            )
            await db.commit()

    async def get_user(self, user_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_field(self, user_id: int, field: str, value) -> None:
        allowed = {"wb_token", "ozon_client_id", "ozon_api_key",
                   "tone", "style", "signature",
                   "auto_enabled", "auto_send", "answer_rating"}
        if field not in allowed:
            raise ValueError(f"Поле {field} запрещено к обновлению")
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                f"UPDATE users SET {field} = ? WHERE user_id = ?",
                (value, user_id),
            )
            await db.commit()

    async def list_auto_users(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM users WHERE auto_enabled = 1 AND wb_token IS NOT NULL"
            )
            return [dict(r) for r in await cur.fetchall()]

    async def mark_answered(self, user_id: int, feedback_id: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO answered(user_id, feedback_id) VALUES (?, ?)",
                (user_id, feedback_id),
            )
            await db.commit()

    async def is_answered(self, user_id: int, feedback_id: str) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM answered WHERE user_id = ? AND feedback_id = ?",
                (user_id, feedback_id),
            )
            return await cur.fetchone() is not None

    async def is_notified(self, user_id: int, feedback_id: str, source: str = "wb") -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT 1 FROM notified WHERE user_id = ? AND feedback_id = ? AND source = ?",
                (user_id, feedback_id, source),
            )
            return await cur.fetchone() is not None

    async def add_notified(
        self,
        user_id: int,
        feedback_id: str,
        answer: str,
        fb_json: str,
        kind: str = "feedback",
        source: str = "wb",
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO notified(user_id, feedback_id, answer, fb_json, kind, source)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, feedback_id, answer, fb_json, kind, source),
            )
            await db.commit()

    async def clear_notified(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "DELETE FROM notified WHERE user_id = ?", (user_id,)
            )
            await db.commit()
            return cur.rowcount

    async def get_notified(
        self, user_id: int, feedback_id: str, source: str = "wb"
    ) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT answer, fb_json, kind, source FROM notified"
                " WHERE user_id = ? AND feedback_id = ? AND source = ?",
                (user_id, feedback_id, source),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def update_notified_answer(
        self, user_id: int, feedback_id: str, answer: str, source: str = "wb"
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE notified SET answer = ?"
                " WHERE user_id = ? AND feedback_id = ? AND source = ?",
                (answer, user_id, feedback_id, source),
            )
            await db.commit()

    async def count_answered(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM answered WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    async def count_notified(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM notified WHERE user_id = ?", (user_id,)
            )
            row = await cur.fetchone()
            return row[0] if row else 0

    async def list_notified(self, user_id: int) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT feedback_id, answer, fb_json, kind, source FROM notified"
                " WHERE user_id = ? ORDER BY created_at DESC",
                (user_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def count_notified_by_kind(self, user_id: int) -> dict:
        """Возвращает {(source, kind): count}."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT source, kind, COUNT(*) FROM notified WHERE user_id = ?"
                " GROUP BY source, kind",
                (user_id,),
            )
            return {(r[0], r[1]): r[2] for r in await cur.fetchall()}

    async def delete_notified(
        self, user_id: int, feedback_id: str, source: str = "wb"
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM notified WHERE user_id = ? AND feedback_id = ? AND source = ?",
                (user_id, feedback_id, source),
            )
            await db.commit()
