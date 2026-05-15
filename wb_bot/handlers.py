from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .config import Settings
from .db import DB
from .gemini import GeminiClient, GeminiError
from .keyboards import (
    RATING_FILTERS,
    STYLES,
    TONES,
    cancel_kb,
    feedback_kb,
    main_menu,
    rating_kb,
    style_kb,
    tone_kb,
)
from .wb_api import WBClient, WBError

log = logging.getLogger(__name__)
router = Router()


# In-memory cache: user_id -> list of (feedback_dict, generated_answer)
_session_cache: dict[int, list[dict]] = {}


class TokenInput(StatesGroup):
    waiting = State()


class SignatureInput(StatesGroup):
    waiting = State()


def _is_admin(user_id: int, settings: Settings) -> bool:
    return not settings.admin_ids or user_id in settings.admin_ids


@router.message.outer_middleware()
@router.callback_query.outer_middleware()
async def admin_only_mw(handler, event, data):
    settings: Settings = data["settings"]
    user = event.from_user
    if user is None or not _is_admin(user.id, settings):
        if isinstance(event, CallbackQuery):
            await event.answer("⛔️ Доступ запрещён.", show_alert=True)
        else:
            await event.answer("⛔️ Доступ запрещён.")
        return
    return await handler(event, data)


def _filter_by_rating(feedbacks: list[dict], rating_filter: str) -> list[dict]:
    if rating_filter == "neg":
        return [f for f in feedbacks if (f.get("productValuation") or 0) <= 3]
    if rating_filter == "pos":
        return [f for f in feedbacks if (f.get("productValuation") or 0) >= 4]
    return feedbacks


def _format_feedback(fb: dict, generated: str, idx: int, total: int) -> str:
    rating = fb.get("productValuation") or 0
    stars = "⭐" * rating + "☆" * (5 - rating)
    product = (fb.get("productDetails") or {}).get("productName") or "—"
    text = fb.get("text") or "(без текста)"
    author = fb.get("userName") or "Покупатель"
    return (
        f"<b>Отзыв {idx + 1}/{total}</b>\n"
        f"{stars} ({rating}/5)\n"
        f"👤 <i>{html.escape(author)}</i>\n"
        f"📦 <i>{html.escape(product)}</i>\n\n"
        f"<b>Отзыв:</b>\n{html.escape(text)}\n\n"
        f"<b>🤖 Сгенерированный ответ:</b>\n{html.escape(generated)}"
    )


async def _settings_text(db: DB, user_id: int) -> str:
    u = await db.get_user(user_id)
    if not u:
        return "Настройки не найдены."
    tone_label = dict(TONES).get(u["tone"], u["tone"])
    style_label = dict(STYLES).get(u["style"], u["style"])
    rating_label = dict(RATING_FILTERS).get(u["answer_rating"], u["answer_rating"])
    return (
        "<b>Текущие настройки:</b>\n"
        f"• Тон: {html.escape(tone_label)}\n"
        f"• Стиль: {html.escape(style_label)}\n"
        f"• Фильтр оценок: {html.escape(rating_label)}\n"
        f"• Подпись: <i>{html.escape(u['signature'] or '—')}</i>\n"
        f"• WB-токен: {'задан ✅' if u['wb_token'] else 'не задан ❌'}\n"
        f"• Автоответ: {'ВКЛ 🟢' if u['auto_enabled'] else 'ВЫКЛ ⚪️'}"
    )


# --------------------- /start ---------------------

@router.message(Command("start"))
async def cmd_start(msg: Message, db: DB, settings: Settings) -> None:
    await db.ensure_user(msg.from_user.id)
    u = await db.get_user(msg.from_user.id)
    await msg.answer(
        "👋 Привет! Я — бот-автоответчик отзывов Wildberries на базе Gemini.\n\n"
        "Для начала задайте WB-токен через кнопку ниже. Управление полностью кнопками.",
        reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
    )


# --------------------- Главное меню ---------------------

@router.callback_query(F.data == "back_main")
async def cb_back_main(cq: CallbackQuery, db: DB, state: FSMContext) -> None:
    await state.clear()
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🏠 Главное меню",
        reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
    )
    await cq.answer()


@router.callback_query(F.data == "show_settings")
async def cb_show_settings(cq: CallbackQuery, db: DB) -> None:
    text = await _settings_text(db, cq.from_user.id)
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        text,
        reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
    )
    await cq.answer()


@router.callback_query(F.data == "toggle_auto")
async def cb_toggle_auto(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    if not u["wb_token"]:
        await cq.answer("Сначала задайте WB-токен.", show_alert=True)
        return
    new_val = 0 if u["auto_enabled"] else 1
    await db.update_field(cq.from_user.id, "auto_enabled", new_val)
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_reply_markup(
        reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"]))
    )
    await cq.answer("Автоответ ВКЛЮЧЁН" if new_val else "Автоответ выключен")


# --------------------- Выбор тона / стиля / фильтра ---------------------

@router.callback_query(F.data == "menu:tone")
async def cb_menu_tone(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🎭 Выберите тон ответа:", reply_markup=tone_kb(u["tone"])
    )
    await cq.answer()


@router.callback_query(F.data == "menu:style")
async def cb_menu_style(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🪶 Выберите стиль ответа:", reply_markup=style_kb(u["style"])
    )
    await cq.answer()


@router.callback_query(F.data == "menu:rating")
async def cb_menu_rating(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🔍 На какие отзывы отвечать?",
        reply_markup=rating_kb(u["answer_rating"]),
    )
    await cq.answer()


@router.callback_query(F.data.startswith("tone:"))
async def cb_set_tone(cq: CallbackQuery, db: DB) -> None:
    code = cq.data.split(":", 1)[1]
    await db.update_field(cq.from_user.id, "tone", code)
    await cq.message.edit_reply_markup(reply_markup=tone_kb(code))
    await cq.answer(f"Тон: {dict(TONES).get(code, code)}")


@router.callback_query(F.data.startswith("style:"))
async def cb_set_style(cq: CallbackQuery, db: DB) -> None:
    code = cq.data.split(":", 1)[1]
    await db.update_field(cq.from_user.id, "style", code)
    await cq.message.edit_reply_markup(reply_markup=style_kb(code))
    await cq.answer(f"Стиль: {dict(STYLES).get(code, code)}")


@router.callback_query(F.data.startswith("rating:"))
async def cb_set_rating(cq: CallbackQuery, db: DB) -> None:
    code = cq.data.split(":", 1)[1]
    await db.update_field(cq.from_user.id, "answer_rating", code)
    await cq.message.edit_reply_markup(reply_markup=rating_kb(code))
    await cq.answer(f"Фильтр: {dict(RATING_FILTERS).get(code, code)}")


# --------------------- Ввод токена ---------------------

@router.callback_query(F.data == "set_token")
async def cb_set_token(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TokenInput.waiting)
    await cq.message.edit_text(
        "🔑 Пришлите WB API-токен (категория «Отзывы и вопросы»).\n\n"
        "Получить: <b>Настройки → Доступ к API</b> в личном кабинете продавца.",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@router.message(TokenInput.waiting)
async def msg_token(msg: Message, state: FSMContext, db: DB, settings: Settings) -> None:
    token = (msg.text or "").strip()
    try:
        await msg.delete()  # чтобы токен не висел в чате
    except Exception:
        log.info("Не удалось удалить сообщение с токеном (нет прав)")
    if len(token) < 20:
        await msg.answer("❌ Похоже, это не токен. Попробуйте ещё раз.",
                         reply_markup=cancel_kb())
        return

    wb = WBClient(token)
    ok = await wb.ping()
    if not ok:
        await msg.answer("❌ Токен не прошёл проверку у WB. Попробуйте другой.",
                         reply_markup=cancel_kb())
        return

    await db.update_field(msg.from_user.id, "wb_token", token)
    await state.clear()
    u = await db.get_user(msg.from_user.id)
    await msg.answer(
        "✅ Токен сохранён и проверен.",
        reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
    )


# --------------------- Ввод подписи ---------------------

@router.callback_query(F.data == "set_signature")
async def cb_set_signature(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SignatureInput.waiting)
    await cq.message.edit_text(
        "✍️ Пришлите подпись магазина (она будет добавляться в конец ответов).\n"
        "Например: <i>«С уважением, команда Магазина»</i>\n\n"
        "Отправьте «-» чтобы убрать подпись.",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@router.message(SignatureInput.waiting)
async def msg_signature(msg: Message, state: FSMContext, db: DB) -> None:
    sig = (msg.text or "").strip()
    value = None if sig == "-" else sig[:200]
    await db.update_field(msg.from_user.id, "signature", value)
    await state.clear()
    u = await db.get_user(msg.from_user.id)
    await msg.answer(
        "✅ Подпись " + ("удалена." if value is None else "сохранена."),
        reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
    )


# --------------------- Показ отзывов ---------------------

@router.callback_query(F.data.startswith("show:"))
async def cb_show(
    cq: CallbackQuery, db: DB, gemini: GeminiClient, settings: Settings
) -> None:
    idx = int(cq.data.split(":", 1)[1])
    u = await db.get_user(cq.from_user.id)
    if not u["wb_token"]:
        await cq.answer("Сначала задайте WB-токен.", show_alert=True)
        return

    cache = _session_cache.get(cq.from_user.id)
    if not cache or idx == 0:
        await cq.message.edit_text("⏳ Загружаю отзывы из WB...")
        try:
            wb = WBClient(u["wb_token"])
            raw = await wb.get_unanswered(take=settings.batch_size)
        except WBError as e:
            log.exception("WB error")
            await cq.message.edit_text(
                f"❌ Ошибка WB API: {html.escape(str(e))}",
                reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
            )
            return
        filtered = _filter_by_rating(raw, u["answer_rating"])
        if not filtered:
            await cq.message.edit_text(
                "📭 Неотвеченных отзывов нет.",
                reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
            )
            return
        cache = [{"fb": fb, "answer": None} for fb in filtered]
        _session_cache[cq.from_user.id] = cache

    if idx >= len(cache):
        await cq.answer("Это был последний отзыв.", show_alert=True)
        return

    item = cache[idx]
    if not item["answer"]:
        await cq.message.edit_text(f"🤖 Генерирую ответ ({idx + 1}/{len(cache)})...")
        item["answer"] = await _gen(gemini, u, item["fb"])

    await cq.message.edit_text(
        _format_feedback(item["fb"], item["answer"], idx, len(cache)),
        parse_mode="HTML",
        reply_markup=feedback_kb(item["fb"]["id"], idx, len(cache)),
    )
    await cq.answer()


async def _gen(gemini: GeminiClient, user: dict, fb: dict) -> str:
    try:
        return await gemini.generate_answer(
            review_text=fb.get("text") or "",
            rating=fb.get("productValuation") or 5,
            product_name=(fb.get("productDetails") or {}).get("productName"),
            tone=user["tone"],
            style=user["style"],
            signature=user["signature"],
        )
    except GeminiError as e:
        log.exception("Gemini error")
        return f"[Ошибка генерации: {e}]"


@router.callback_query(F.data.startswith("regen:"))
async def cb_regen(cq: CallbackQuery, db: DB, gemini: GeminiClient) -> None:
    fb_id = cq.data.split(":", 1)[1]
    cache = _session_cache.get(cq.from_user.id) or []
    idx, item = next(
        ((i, it) for i, it in enumerate(cache) if it["fb"]["id"] == fb_id),
        (None, None),
    )
    if item is None:
        await cq.answer("Отзыв не найден в текущей сессии.", show_alert=True)
        return
    await cq.answer("Перегенерирую...")
    u = await db.get_user(cq.from_user.id)
    item["answer"] = await _gen(gemini, u, item["fb"])
    await cq.message.edit_text(
        _format_feedback(item["fb"], item["answer"], idx, len(cache)),
        parse_mode="HTML",
        reply_markup=feedback_kb(item["fb"]["id"], idx, len(cache)),
    )


@router.callback_query(F.data.startswith("send:"))
async def cb_send(cq: CallbackQuery, db: DB, gemini: GeminiClient) -> None:
    fb_id = cq.data.split(":", 1)[1]
    cache = _session_cache.get(cq.from_user.id) or []
    idx, item = next(
        ((i, it) for i, it in enumerate(cache) if it["fb"]["id"] == fb_id),
        (None, None),
    )
    if item is None:
        await cq.answer("Отзыв не найден в текущей сессии.", show_alert=True)
        return

    u = await db.get_user(cq.from_user.id)
    wb = WBClient(u["wb_token"])
    try:
        await wb.answer(fb_id, item["answer"])
    except WBError as e:
        await cq.answer(f"Ошибка WB: {e}", show_alert=True)
        return

    await db.mark_answered(cq.from_user.id, fb_id)
    await cq.answer("✅ Ответ отправлен на WB")

    if idx + 1 < len(cache):
        await _open_idx(cq, db, gemini, idx + 1)
    else:
        await cq.message.edit_text(
            "🎉 Все отзывы из текущей пачки обработаны.",
            reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
        )
        _session_cache.pop(cq.from_user.id, None)


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(cq: CallbackQuery, db: DB, gemini: GeminiClient) -> None:
    idx = int(cq.data.split(":", 1)[1])
    cache = _session_cache.get(cq.from_user.id) or []
    if idx + 1 >= len(cache):
        await cq.answer("Это был последний отзыв.", show_alert=True)
        u = await db.get_user(cq.from_user.id)
        await cq.message.edit_text(
            "🏠 Главное меню",
            reply_markup=main_menu(bool(u["auto_enabled"]), bool(u["wb_token"])),
        )
        _session_cache.pop(cq.from_user.id, None)
        return
    await _open_idx(cq, db, gemini, idx + 1)


async def _open_idx(cq: CallbackQuery, db: DB, gemini: GeminiClient, idx: int) -> None:
    cache = _session_cache.get(cq.from_user.id) or []
    if idx >= len(cache):
        return
    item = cache[idx]
    if not item["answer"]:
        await cq.message.edit_text(f"🤖 Генерирую ответ ({idx + 1}/{len(cache)})...")
        u = await db.get_user(cq.from_user.id)
        item["answer"] = await _gen(gemini, u, item["fb"])
    await cq.message.edit_text(
        _format_feedback(item["fb"], item["answer"], idx, len(cache)),
        parse_mode="HTML",
        reply_markup=feedback_kb(item["fb"]["id"], idx, len(cache)),
    )
