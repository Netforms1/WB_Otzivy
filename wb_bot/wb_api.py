import httpx

BASE_URL = "https://feedbacks-api.wildberries.ru"


class WBError(Exception):
    pass


class WBClient:
    """Клиент Wildberries Feedbacks API.

    Документация: https://dev.wildberries.ru/openapi/user-communication
    """

    def __init__(self, token: str, timeout: float = 30.0):
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": self.token, "Content-Type": "application/json"}

    async def get_unanswered(self, take: int = 20, skip: int = 0) -> list[dict]:
        params = {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{BASE_URL}/api/v1/feedbacks",
                headers=self._headers(),
                params=params,
            )
            if r.status_code != 200:
                raise WBError(f"WB API {r.status_code}: {r.text}")
            data = r.json()
            if data.get("error"):
                raise WBError(data.get("errorText") or "WB API error")
            return data.get("data", {}).get("feedbacks") or []

    async def answer(self, feedback_id: str, text: str) -> None:
        payload = {"id": feedback_id, "text": text}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{BASE_URL}/api/v1/feedbacks/answer",
                headers=self._headers(),
                json=payload,
            )
            if r.status_code not in (200, 204):
                raise WBError(f"WB API {r.status_code}: {r.text}")

    async def ping(self) -> bool:
        """Простая проверка валидности токена."""
        try:
            await self.get_unanswered(take=1)
            return True
        except WBError:
            return False
