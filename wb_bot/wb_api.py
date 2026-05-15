from __future__ import annotations

import asyncio
import logging

import httpx

BASE_URL = "https://feedbacks-api.wildberries.ru"
log = logging.getLogger(__name__)


class WBError(Exception):
    pass


class WBClient:
    """Клиент Wildberries Feedbacks API.

    Документация: https://dev.wildberries.ru/openapi/user-communication
    """

    def __init__(self, token: str, timeout: float = 30.0, max_retries: int = 3):
        self.token = token
        self.timeout = timeout
        self.max_retries = max_retries

    def _headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = f"{BASE_URL}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                r = await client.request(method, url, headers=self._headers(), **kwargs)
                if r.status_code != 429:
                    return r
                # Уважаем Retry-After, но не больше 30 сек
                retry_after = r.headers.get("Retry-After")
                try:
                    delay = min(float(retry_after), 30.0) if retry_after else 2 ** attempt
                except ValueError:
                    delay = 2 ** attempt
                if attempt == self.max_retries:
                    raise WBError(
                        f"WB API 429: лимит запросов превышен. Подождите ~{int(delay)} сек."
                    )
                log.warning("WB 429, retry in %.1fs (attempt %d)", delay, attempt + 1)
                await asyncio.sleep(delay)
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
        except WBError:
            return False
