from __future__ import annotations

import asyncio
import logging
import time

import httpx

BASE_URL = "https://feedbacks-api.wildberries.ru"
log = logging.getLogger(__name__)

# Глобальный cooldown по токену: после 429 не дёргаем WB до этого момента
_cooldown_until: dict[str, float] = {}


class WBError(Exception):
    pass


class WBRateLimited(WBError):
    """429 от WB — без ретраев, просто ждать следующего цикла."""


class WBClient:
    """Клиент Wildberries Feedbacks API.

    Документация: https://dev.wildberries.ru/openapi/user-communication
    """

    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json"}

    def _check_cooldown(self) -> None:
        until = _cooldown_until.get(self.token, 0.0)
        left = until - time.monotonic()
        if left > 0:
            raise WBRateLimited(
                f"WB rate-limit: ждём {int(left)} сек перед следующим запросом"
            )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self._check_cooldown()
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.request(
                method, f"{BASE_URL}{path}", headers=self._headers(), **kwargs
            )
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 60.0
            except ValueError:
                wait = 60.0
            wait = min(max(wait, 30.0), 300.0)
            _cooldown_until[self.token] = time.monotonic() + wait
            log.info("WB 429, cooldown %.0f сек", wait)
            raise WBRateLimited(f"WB лимит: следующий запрос через {int(wait)} сек")
        return r

    async def get_unanswered(self, take: int = 20, skip: int = 0) -> list[dict]:
        params = {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"}
        r = await self._request("GET", "/api/v1/feedbacks", params=params)
        if r.status_code != 200:
            raise WBError(f"WB API {r.status_code}: {r.text}")
        data = r.json()
        if data.get("error"):
            raise WBError(data.get("errorText") or "WB API error")
        return data.get("data", {}).get("feedbacks") or []

    async def answer(self, feedback_id: str, text: str) -> None:
        payload = {"id": feedback_id, "text": text}
        r = await self._request("POST", "/api/v1/feedbacks/answer", json=payload)
        if r.status_code not in (200, 204):
            raise WBError(f"WB API {r.status_code}: {r.text}")

    async def ping(self) -> bool:
        """Простая проверка валидности токена."""
        try:
            await self.get_unanswered(take=1)
            return True
        except WBRateLimited:
            # 429 = токен прошёл авторизацию, просто упёрлись в лимит
            return True
        except WBError:
            return False
