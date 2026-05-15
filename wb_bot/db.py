import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        INTEGER PRIMARY KEY,
    wb_token       TEXT,
    tone           TEXT NOT NULL DEFAULT 'friendly',
    style          TEXT NOT NULL DEFAULT 'medium',
    signature      TEXT,
    auto_enabled   INTEGER NOT NULL DEFAULT 0,
    answer_rating  TEXT NOT NULL DEFAULT 'all',
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS answered (
    user_id     INTEGER NOT NULL,
    feedback_id TEXT NOT NULL,
    answered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, feedback_id)
);
"""


class DB:
    def __init__(self, path: str):
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
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
        allowed = {"wb_token", "tone", "style", "signature",
                   "auto_enabled", "answer_rating"}
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
