from __future__ import annotations

import httpx

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


TONE_LABELS = {
    "friendly":   "дружелюбный, тёплый, человечный",
    "formal":     "деловой, вежливый, без фамильярности",
    "neutral":    "нейтральный, спокойный, информативный",
    "apologetic": "извиняющийся, с эмпатией к проблеме клиента",
    "fun":        "лёгкий, с долей уместного юмора и позитива",
}

STYLE_LABELS = {
    "short":  "1-2 короткие фразы, максимально лаконично",
    "medium": "3-5 предложений, среднее по длине сообщение",
    "long":   "развёрнутый ответ с деталями, 6-10 предложений",
    "emoji":  "среднее по длине, с уместными эмодзи (1-3 шт)",
}


class GeminiError(Exception):
    pass


class GeminiClient:
    def __init__(self, api_key: str, model: str, timeout: float = 30.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _build_prompt(
        self,
        review_text: str,
        rating: int,
        product_name: str | None,
        tone: str,
        style: str,
        signature: str | None,
    ) -> str:
        tone_desc = TONE_LABELS.get(tone, TONE_LABELS["friendly"])
        style_desc = STYLE_LABELS.get(style, STYLE_LABELS["medium"])
        sig_block = (
            f"\nПодпись магазина (добавить в конец): {signature}"
            if signature else ""
        )
        product_block = f"\nТовар: {product_name}" if product_name else ""

        return f"""Ты — менеджер магазина на Wildberries, отвечаешь на отзыв покупателя.

Правила:
- Тон: {tone_desc}
- Стиль: {style_desc}
- Пиши только на русском языке.
- Не используй markdown, ссылки, контакты вне Wildberries, цены или обещания.
- Не упоминай конкурентов и не критикуй WB.
- Обращайся к покупателю на «Вы».
- Если отзыв негативный — признай проблему, извинись, предложи решение в рамках WB (обращение в поддержку, возврат, обмен).
- Если позитивный — поблагодари искренне, без шаблонов «спасибо за заказ».
- Не повторяй текст отзыва дословно.
- В ответе должен быть ТОЛЬКО текст ответа покупателю, без префиксов вроде «Ответ:».
{product_block}
Оценка покупателя: {rating}/5
Текст отзыва:
\"\"\"{review_text}\"\"\"
{sig_block}
"""

    async def generate_question_answer(
        self,
        question_text: str,
        product_name: str | None,
        tone: str,
        style: str,
        signature: str | None,
    ) -> str:
        tone_desc = TONE_LABELS.get(tone, TONE_LABELS["friendly"])
        style_desc = STYLE_LABELS.get(style, STYLE_LABELS["medium"])
        sig_block = f"\nПодпись магазина: {signature}" if signature else ""
        product_block = f"\nТовар: {product_name}" if product_name else ""
        prompt = f"""Ты — менеджер магазина на Wildberries, отвечаешь на ВОПРОС покупателя.

Правила:
- Тон: {tone_desc}
- Стиль: {style_desc}
- Пиши только на русском.
- Не используй markdown, ссылки, контакты вне Wildberries, цены или обещания.
- Не упоминай конкурентов и не критикуй WB.
- Обращайся к покупателю на «Вы».
- Если в вопросе спрашивают факт о товаре (размер, материал, состав), а информации нет — честно укажи, что точные характеристики смотрите в карточке товара.
- В ответе должен быть ТОЛЬКО текст ответа покупателю, без префиксов.
{product_block}
Вопрос покупателя:
\"\"\"{question_text}\"\"\"
{sig_block}
"""
        return await self._call(prompt)

    async def _call(self, prompt: str) -> str:
        url = API_URL.format(model=self.model)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.8,
                "topP": 0.9,
                "maxOutputTokens": 800,
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, params={"key": self.api_key}, json=payload)
            if r.status_code != 200:
                raise GeminiError(f"Gemini {r.status_code}: {r.text}")
            data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as e:
            raise GeminiError(f"Не удалось распарсить ответ Gemini: {data}") from e
        return text.strip()

    async def generate_answer(
        self,
        review_text: str,
        rating: int,
        product_name: str | None,
        tone: str,
        style: str,
        signature: str | None,
    ) -> str:
        prompt = self._build_prompt(
            review_text, rating, product_name, tone, style, signature
        )
        return await self._call(prompt)
