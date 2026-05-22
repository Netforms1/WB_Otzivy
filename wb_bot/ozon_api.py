"""Клиент Ozon Seller API для работы с отзывами.

Документация: https://docs.ozon.ru/api/seller/#tag/ReviewAPI
Требуется подписка Premium Plus у продавца.
"""
from __future__ import annotations

import logging
import time

import httpx

BASE_URL = "https://api-seller.ozon.ru"
log = logging.getLogger(__name__)

# Per-token cooldown
_cooldown_until: dict[str, float] = {}


class OzonError(Exception):
    pass


class OzonRateLimited(OzonError):
    pass


class OzonClient:
    def __init__(self, client_id: str, api_key: str, timeout: float = 30.0):
        self.client_id = client_id
        self.api_key = api_key
        self.timeout = timeout
        self._cd_key = f"{client_id}:{api_key[:8]}"

    def _headers(self) -> dict:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def cooldown_left(self) -> int:
        left = _cooldown_until.get(self._cd_key, 0.0) - time.monotonic()
        return max(0, int(left))

    def _check_cooldown(self) -> None:
        left = self.cooldown_left()
        if left > 0:
            raise OzonRateLimited(f"Ozon на cooldown ещё {left} сек")

    async def _post(self, path: str, body: dict) -> httpx.Response:
        self._check_cooldown()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(f"{BASE_URL}{path}", headers=self._headers(), json=body)
        except httpx.RequestError as e:
            raise OzonError(f"Сетевая ошибка: {e.__class__.__name__}") from e
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After")
            try:
                wait = float(retry_after) if retry_after else 120.0
            except ValueError:
                wait = 120.0
            wait = min(max(wait, 60.0), 600.0)
            _cooldown_until[self._cd_key] = time.monotonic() + wait
            log.info("Ozon 429, cooldown %.0f сек", wait)
            raise OzonRateLimited(f"Ozon лимит: ждать {int(wait)} сек")
        return r

    async def get_reviews(self, limit: int = 20) -> list[dict]:
        body = {"limit": limit, "status": "UNPROCESSED", "sort_dir": "DESC"}
        r = await self._post("/v1/review/list", body)
        if r.status_code != 200:
            raise OzonError(f"Ozon /v1/review/list {r.status_code}: {r.text[:400]}")
        data = r.json()
        return data.get("reviews", []) or []

    async def get_review_info(self, review_id: str) -> dict:
        r = await self._post("/v1/review/info", {"review_id": review_id})
        if r.status_code != 200:
            raise OzonError(f"Ozon /v1/review/info {r.status_code}: {r.text[:200]}")
        return r.json()

    async def answer_review(self, review_id: str, text: str) -> None:
        body = {"review_id": review_id, "text": text, "mark_review_as_processed": True}
        r = await self._post("/v1/review/comment/create", body)
        if r.status_code != 200:
            raise OzonError(f"Ozon /v1/review/comment/create {r.status_code}: {r.text[:400]}")

    async def check(self) -> tuple[str, str]:
        """Возвращает (status, message). status: ok | rate_limited | unauthorized | premium_required | network | error."""
        left = self.cooldown_left()
        if left > 0:
            return "rate_limited", f"Ozon на cooldown ещё ~{left // 60 + 1} мин"
        body = {"limit": 1, "status": "UNPROCESSED", "sort_dir": "DESC"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(
                    f"{BASE_URL}/v1/review/list",
                    headers=self._headers(),
                    json=body,
                )
        except httpx.RequestError as e:
            return "network", f"Сетевая ошибка: {e}"

        if r.status_code == 200:
            data = r.json()
            count = len(data.get("reviews", []) or [])
            return "ok", f"Креды валидны. Необработанных отзывов (в выборке): {count}"
        if r.status_code in (401, 403):
            body_text = r.text.lower()
            if "premium" in body_text or "subscription" in body_text or r.status_code == 403:
                return "premium_required", "403 — нужна подписка Ozon Premium Plus или ключ без доступа к отзывам"
            return "unauthorized", f"{r.status_code} — креды невалидны"
        if r.status_code == 429:
            _cooldown_until[self._cd_key] = time.monotonic() + 120.0
            return "rate_limited", "429 — Ozon лимит, cooldown 2 мин"
        return "error", f"Ozon вернул {r.status_code}: {r.text[:200]}"
