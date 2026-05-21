from __future__ import annotations

import asyncio
import html
import json
import logging
from datetime import datetime, timezone

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
    buffer_item_kb,
    cancel_kb,
    main_menu,
    rating_kb,
    settings_menu,
    style_kb,
    tone_kb,
)
from .ozon_api import OzonClient, OzonError, OzonRateLimited
from .wb_api import WBClient, WBError, WBRateLimited

log = logging.getLogger(__name__)
router = Router()


# user_id -> {"wb_feedbacks": int, "wb_questions": int, "ozon_reviews": int, "ts": datetime}
last_check: dict[int, dict] = {}


# --------------------- Доступ ---------------------

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


# --------------------- FSM ---------------------

class TokenInput(StatesGroup):
    waiting_wb = State()
    waiting_ozon = State()


class SignatureInput(StatesGroup):
    waiting = State()


# --------------------- Helpers ---------------------

def _has_wb(u: dict | None) -> bool:
    return bool(u and u.get("wb_token"))


def _has_ozon(u: dict | None) -> bool:
    return bool(u and u.get("ozon_client_id") and u.get("ozon_api_key"))


def _filter_by_rating(feedbacks: list[dict], rating_filter: str) -> list[dict]:
    if rating_filter == "neg":
        return [f for f in feedbacks if (f.get("productValuation") or f.get("rating") or 0) <= 3]
    if rating_filter == "pos":
        return [f for f in feedbacks if (f.get("productValuation") or f.get("rating") or 0) >= 4]
    return feedbacks


def _format_item(fb: dict, answer: str, kind: str, source: str) -> str:
    text = (fb.get("text") or "(без текста)")[:1500]
    if source == "ozon":
        product = (fb.get("product_details") or {}).get("title") or fb.get("sku") or "—"
        author = fb.get("author") or fb.get("author_name") or "Покупатель"
        rating = fb.get("rating") or 0
        platform = "🟦 Ozon"
    else:
        product = (fb.get("productDetails") or {}).get("productName") or "—"
        author = fb.get("userName") or "Покупатель"
        rating = fb.get("productValuation") or 0
        platform = "🟣 WB"

    if kind == "question":
        header = f"{platform} ❓ <b>Вопрос</b>"
        rating_line = ""
    else:
        stars = "⭐" * rating + "☆" * (5 - rating) if rating else ""
        header = f"{platform} 📬 <b>Отзыв</b>"
        rating_line = f"{stars} ({rating}/5)\n" if rating else ""
    return (
        f"{header}\n"
        f"{rating_line}"
        f"👤 <i>{html.escape(str(author))}</i>\n"
        f"📦 <i>{html.escape(str(product))}</i>\n\n"
        f"<b>Текст:</b>\n{html.escape(text)}\n\n"
        f"<b>🤖 Ответ:</b>\n{html.escape(answer)}"
    )


async def _menu_text(db: DB, user_id: int) -> str:
    u = await db.get_user(user_id)
    answered = await db.count_answered(user_id)
    counts = await db.count_notified_by_kind(user_id)
    pending_total = sum(counts.values())
    info = last_check.get(user_id)

    lines = ["🏠 <b>Бот-автоответчик: WB + Ozon</b>", ""]
    if info:
        ago_sec = (datetime.now(timezone.utc) - info["ts"]).total_seconds()
        ago = f"{int(ago_sec // 60)} мин назад" if ago_sec >= 60 else f"{int(ago_sec)} сек назад"
        if _has_wb(u):
            lines.append(f"🟣 WB: отзывов {info.get('wb_feedbacks', '?')}, вопросов {info.get('wb_questions', '?')}")
        if _has_ozon(u):
            lines.append(f"🟦 Ozon: отзывов {info.get('ozon_reviews', '?')}")
        lines.append(f"   <i>обновлено {ago}</i>")
    else:
        lines.append("Ещё не обновлял. Нажми «🔄 Обновить».")
    lines.append("")
    lines.append(f"📂 В буфере: <b>{pending_total}</b>")
    if pending_total > 0:
        parts = []
        for (src, kind), c in counts.items():
            label = {"wb": "WB", "ozon": "Ozon"}.get(src, src)
            kind_label = {"feedback": "отз.", "question": "вопр.", "review": "отз."}.get(kind, kind)
            parts.append(f"{label}: {c} {kind_label}")
        lines.append(f"   <i>{', '.join(parts)}</i>")
    lines.append(f"✅ Отправлено через бота: <b>{answered}</b>")

    cooldown_lines = []
    if _has_wb(u):
        left = WBClient(u["wb_token"]).cooldown_left()
        if left:
            cooldown_lines.append(f"⏳ WB cooldown ~{left // 60 + 1} мин")
    if _has_ozon(u):
        left = OzonClient(u["ozon_client_id"], u["ozon_api_key"]).cooldown_left()
        if left:
            cooldown_lines.append(f"⏳ Ozon cooldown ~{left // 60 + 1} мин")
    if cooldown_lines:
        lines.append("")
        lines.extend(cooldown_lines)
    if not _has_wb(u) and not _has_ozon(u):
        lines.append("")
        lines.append("⚠️ Задай WB-токен или Ozon-ключи в «⚙️ Настройки»")
    return "\n".join(lines)


async def _build_kb(db: DB, user_id: int):
    u = await db.get_user(user_id)
    pending = await db.count_notified(user_id)
    return main_menu(_has_wb(u), _has_ozon(u), pending, bool(u["auto_send"]))


async def _show_main(target, db: DB, user_id: int) -> None:
    text = await _menu_text(db, user_id)
    kb = await _build_kb(db, user_id)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=kb)


# --------------------- /start ---------------------

@router.message(Command("start"))
async def cmd_start(msg: Message, db: DB) -> None:
    await db.ensure_user(msg.from_user.id)
    await _show_main(msg, db, msg.from_user.id)


@router.callback_query(F.data == "back_main")
async def cb_back_main(cq: CallbackQuery, db: DB, state: FSMContext) -> None:
    await state.clear()
    await _show_main(cq, db, cq.from_user.id)
    await cq.answer()


# --------------------- Настройки ---------------------

@router.callback_query(F.data == "menu:settings")
async def cb_menu_settings(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    pending = await db.count_notified(cq.from_user.id)
    await cq.message.edit_text(
        "⚙️ <b>Настройки</b>\n\nВыбери раздел:",
        parse_mode="HTML",
        reply_markup=settings_menu(_has_wb(u), _has_ozon(u), pending),
    )
    await cq.answer()


@router.callback_query(F.data == "show_settings")
async def cb_show_settings(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    tone = dict(TONES).get(u["tone"], u["tone"])
    style = dict(STYLES).get(u["style"], u["style"])
    rating_label = dict(RATING_FILTERS).get(u["answer_rating"], u["answer_rating"])
    pending = await db.count_notified(cq.from_user.id)
    text = (
        "ℹ️ <b>Текущие настройки:</b>\n\n"
        f"🎭 Тон: {html.escape(tone)}\n"
        f"🪶 Стиль: {html.escape(style)}\n"
        f"🔍 Фильтр: {html.escape(rating_label)}\n"
        f"✍️ Подпись: <i>{html.escape(u['signature'] or '—')}</i>\n"
        f"🟣 WB: {'✅' if _has_wb(u) else '❌'}\n"
        f"🟦 Ozon: {'✅' if _has_ozon(u) else '❌'}\n"
        f"⚡ Авто-отправка: {'ВКЛ' if u['auto_send'] else 'ВЫКЛ'}"
    )
    await cq.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=settings_menu(_has_wb(u), _has_ozon(u), pending),
    )
    await cq.answer()


# --------------------- Тон / Стиль / Фильтр ---------------------

@router.callback_query(F.data == "menu:tone")
async def cb_menu_tone(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🎭 Выбери тон ответа:", reply_markup=tone_kb(u["tone"])
    )
    await cq.answer()


@router.callback_query(F.data == "menu:style")
async def cb_menu_style(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🪶 Выбери стиль ответа:", reply_markup=style_kb(u["style"])
    )
    await cq.answer()


@router.callback_query(F.data == "menu:rating")
async def cb_menu_rating(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    await cq.message.edit_text(
        "🔍 На какие отзывы отвечать?", reply_markup=rating_kb(u["answer_rating"])
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


# --------------------- WB-токен ---------------------

@router.callback_query(F.data == "set_wb_token")
async def cb_set_wb_token(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TokenInput.waiting_wb)
    await cq.message.edit_text(
        "🟣 Пришли WB API-токен (категория «Отзывы и вопросы»).\n\n"
        "<b>Где взять:</b> ЛК продавца → Настройки → Доступ к API → Создать токен.",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@router.message(TokenInput.waiting_wb)
async def msg_wb_token(msg: Message, state: FSMContext, db: DB) -> None:
    token = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if len(token) < 20:
        await msg.answer("❌ Похоже, это не токен. Попробуй ещё раз.",
                         reply_markup=cancel_kb())
        return
    await db.update_field(msg.from_user.id, "wb_token", token)
    await state.clear()
    await msg.answer("✅ WB-токен сохранён.")
    await _show_main(msg, db, msg.from_user.id)


# --------------------- Ozon-ключи ---------------------

@router.callback_query(F.data == "set_ozon_keys")
async def cb_set_ozon_keys(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TokenInput.waiting_ozon)
    await cq.message.edit_text(
        "🟦 Пришли Ozon-ключи в формате <b>ClientId:ApiKey</b>\n\n"
        "Например: <code>1234567:abcdef-12345-...</code>\n\n"
        "<b>Где взять:</b> Личный кабинет Ozon Seller → Настройки → "
        "API-ключи → Создать ключ. Нужна подписка Premium Plus для доступа к API отзывов.",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@router.message(TokenInput.waiting_ozon)
async def msg_ozon_keys(msg: Message, state: FSMContext, db: DB) -> None:
    raw = (msg.text or "").strip()
    try:
        await msg.delete()
    except Exception:
        pass
    if ":" not in raw:
        await msg.answer("❌ Формат: ClientId:ApiKey. Попробуй снова.",
                         reply_markup=cancel_kb())
        return
    client_id, _, api_key = raw.partition(":")
    client_id = client_id.strip()
    api_key = api_key.strip()
    if not client_id.isdigit() or len(api_key) < 10:
        await msg.answer("❌ Подозрительные значения. ClientId — число, ApiKey — длинная строка.",
                         reply_markup=cancel_kb())
        return
    await db.update_field(msg.from_user.id, "ozon_client_id", client_id)
    await db.update_field(msg.from_user.id, "ozon_api_key", api_key)
    await state.clear()
    await msg.answer("✅ Ozon-ключи сохранены.")
    await _show_main(msg, db, msg.from_user.id)


# --------------------- Проверка ключей ---------------------

@router.callback_query(F.data == "check_keys")
async def cb_check_keys(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    if not (_has_wb(u) or _has_ozon(u)):
        await cq.answer("Сначала задай хотя бы один набор ключей.", show_alert=True)
        return
    await cq.answer("Проверяю...")

    lines = ["🔬 <b>Проверка ключей</b>", ""]
    icons = {"ok": "✅", "rate_limited": "⏳", "unauthorized": "❌",
             "premium_required": "💰", "network": "📡", "error": "⚠️"}

    if _has_wb(u):
        wb = WBClient(u["wb_token"])
        status, info = await wb.check()
        lines.append(f"🟣 WB: {icons.get(status, '❓')} {html.escape(info)}")
    if _has_ozon(u):
        oz = OzonClient(u["ozon_client_id"], u["ozon_api_key"])
        status, info = await oz.check()
        lines.append(f"🟦 Ozon: {icons.get(status, '❓')} {html.escape(info)}")

    await cq.bot.send_message(
        cq.from_user.id, "\n".join(lines), parse_mode="HTML"
    )


# --------------------- Подпись ---------------------

@router.callback_query(F.data == "set_signature")
async def cb_set_signature(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SignatureInput.waiting)
    await cq.message.edit_text(
        "✍️ Пришли подпись магазина (добавится в конец ответов).\n"
        "Например: <i>«С уважением, команда Магазина»</i>\n\n"
        "Отправь «-» чтобы убрать подпись.",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@router.message(SignatureInput.waiting)
async def msg_signature(msg: Message, state: FSMContext, db: DB) -> None:
    sig = (msg.text or "").strip()
    value = None if sig == "-" else sig[:200]
    await db.update_field(msg.from_user.id, "signature", value)
    await state.clear()
    await msg.answer("✅ Подпись " + ("удалена." if value is None else "сохранена."))
    await _show_main(msg, db, msg.from_user.id)


# --------------------- Авто-отправка ---------------------

@router.callback_query(F.data == "toggle_send")
async def cb_toggle_send(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    if not (_has_wb(u) or _has_ozon(u)):
        await cq.answer("Сначала задай ключи.", show_alert=True)
        return
    new_val = 0 if u["auto_send"] else 1
    await db.update_field(cq.from_user.id, "auto_send", new_val)
    await _show_main(cq, db, cq.from_user.id)
    if new_val:
        await cq.answer(
            "⚡ Авто-отправка ВКЛЮЧЕНА. После «Обновить» бот сам отправит "
            "ответы из буфера. Проверь тон/подпись!",
            show_alert=True,
        )
    else:
        await cq.answer("Авто-отправка выключена")


# --------------------- Генерация ---------------------

async def _gen_for_item(gemini: GeminiClient, u: dict, item: dict, kind: str, source: str) -> str:
    try:
        if kind == "question":
            return await gemini.generate_question_answer(
                question_text=item.get("text") or "",
                product_name=(item.get("productDetails") or {}).get("productName"),
                tone=u["tone"], style=u["style"], signature=u["signature"],
            )
        if source == "ozon":
            text = item.get("text") or ""
            rating = item.get("rating") or 5
            product = (item.get("product_details") or {}).get("title") or str(item.get("sku") or "")
        else:
            text = item.get("text") or ""
            rating = item.get("productValuation") or 5
            product = (item.get("productDetails") or {}).get("productName")
        return await gemini.generate_answer(
            review_text=text, rating=rating, product_name=product,
            tone=u["tone"], style=u["style"], signature=u["signature"],
        )
    except GeminiError as e:
        log.warning("Gemini failed: %s", e)
        return f"[Ошибка генерации: {e}]"


async def _process_items(
    db: DB, gemini: GeminiClient, u: dict, items: list[dict], kind: str, source: str
) -> int:
    user_id = u["user_id"]
    added = 0
    for item in items:
        iid = str(item.get("id") or item.get("uuid") or "")
        if not iid:
            continue
        if await db.is_answered(user_id, iid) or await db.is_notified(user_id, iid, source):
            continue
        ans = await _gen_for_item(gemini, u, item, kind, source)
        await db.add_notified(
            user_id, iid, ans, json.dumps(item, ensure_ascii=False), kind, source
        )
        added += 1
    return added


async def _auto_send_buffer(db: DB, wb: WBClient | None, oz: OzonClient | None, user_id: int) -> tuple[int, str | None]:
    rows = await db.list_notified(user_id)
    sent = 0
    for r in rows:
        src = r["source"]
        kind = r["kind"]
        iid = r["feedback_id"]
        try:
            if src == "ozon":
                if not oz:
                    continue
                await oz.answer_review(iid, r["answer"])
            else:
                if not wb:
                    continue
                if kind == "question":
                    await wb.answer_question(iid, r["answer"])
                else:
                    await wb.answer(iid, r["answer"])
        except (WBRateLimited, OzonRateLimited) as e:
            return sent, f"{src.upper()} лимит: {e}"
        except (WBError, OzonError) as e:
            log.warning("Send failed [%s/%s]: %s", src, kind, e)
            continue
        await db.mark_answered(user_id, iid)
        await db.delete_notified(user_id, iid, src)
        sent += 1
        await asyncio.sleep(1.5)
    return sent, None


# --------------------- Обновление ---------------------

@router.callback_query(F.data == "refresh")
async def cb_refresh(
    cq: CallbackQuery, db: DB, gemini: GeminiClient, settings: Settings
) -> None:
    u = await db.get_user(cq.from_user.id)
    if not (_has_wb(u) or _has_ozon(u)):
        await cq.answer("Сначала задай ключи в Настройках.", show_alert=True)
        return

    await cq.answer("Обновляю...")
    progress = await cq.bot.send_message(cq.from_user.id, "⏳ Идёт обновление...")

    summary = ["📊 <b>Готово</b>"]
    cached = last_check.get(cq.from_user.id, {}).copy()
    cached["ts"] = datetime.now(timezone.utc)

    wb_client = WBClient(u["wb_token"]) if _has_wb(u) else None
    oz_client = OzonClient(u["ozon_client_id"], u["ozon_api_key"]) if _has_ozon(u) else None

    # ---- WB отзывы ----
    if wb_client:
        if wb_client.cooldown_left() > 0:
            summary.append(f"🟣 WB отзывы: ⏳ cooldown")
        else:
            try:
                await progress.edit_text("⏳ WB: загружаю отзывы...")
                feedbacks = await wb_client.get_unanswered(take=settings.batch_size)
                cached["wb_feedbacks"] = len(feedbacks)
                feedbacks = _filter_by_rating(feedbacks, u["answer_rating"])
                added = await _process_items(db, gemini, u, feedbacks, "feedback", "wb")
                summary.append(f"🟣 WB отзывы: {cached['wb_feedbacks']} в WB, {added} новых")
            except WBRateLimited as e:
                summary.append(f"🟣 WB отзывы: ⏳ {html.escape(str(e))}")
            except WBError as e:
                summary.append(f"🟣 WB отзывы: ❌ {html.escape(str(e))}")
            await asyncio.sleep(5)

        # ---- WB вопросы ----
        if wb_client.cooldown_left() > 0:
            summary.append("🟣 WB вопросы: ⏳ cooldown")
        else:
            try:
                await progress.edit_text("⏳ WB: загружаю вопросы...")
                questions = await wb_client.get_questions(take=settings.batch_size)
                cached["wb_questions"] = len(questions)
                added = await _process_items(db, gemini, u, questions, "question", "wb")
                summary.append(f"🟣 WB вопросы: {cached['wb_questions']} в WB, {added} новых")
            except WBRateLimited as e:
                summary.append(f"🟣 WB вопросы: ⏳ {html.escape(str(e))}")
            except WBError as e:
                summary.append(f"🟣 WB вопросы: ❌ {html.escape(str(e))}")
            await asyncio.sleep(5)

    # ---- Ozon отзывы ----
    if oz_client:
        if oz_client.cooldown_left() > 0:
            summary.append("🟦 Ozon отзывы: ⏳ cooldown")
        else:
            try:
                await progress.edit_text("⏳ Ozon: загружаю отзывы...")
                reviews = await oz_client.get_reviews(limit=settings.batch_size)
                cached["ozon_reviews"] = len(reviews)
                reviews = _filter_by_rating(reviews, u["answer_rating"])
                added = await _process_items(db, gemini, u, reviews, "review", "ozon")
                summary.append(f"🟦 Ozon отзывы: {cached['ozon_reviews']} в Ozon, {added} новых")
            except OzonRateLimited as e:
                summary.append(f"🟦 Ozon отзывы: ⏳ {html.escape(str(e))}")
            except OzonError as e:
                summary.append(f"🟦 Ozon отзывы: ❌ {html.escape(str(e))}")

    last_check[cq.from_user.id] = cached

    # Авто-отправка
    if u["auto_send"]:
        await progress.edit_text("⏳ Отправляю ответы из буфера...")
        sent, err = await _auto_send_buffer(db, wb_client, oz_client, cq.from_user.id)
        summary.append(f"⚡ Отправлено: {sent}")
        if err:
            summary.append(f"   <i>{html.escape(err)}</i>")

    pending = await db.count_notified(cq.from_user.id)
    if pending and not u["auto_send"]:
        summary.append(f"\n📂 В буфере: <b>{pending}</b>. Жми «Открыть буфер».")

    await progress.edit_text("\n".join(summary), parse_mode="HTML")
    await _show_main(cq, db, cq.from_user.id)


# --------------------- Буфер ---------------------

@router.callback_query(F.data == "open_buffer")
async def cb_open_buffer(cq: CallbackQuery, db: DB) -> None:
    rows = await db.list_notified(cq.from_user.id)
    if not rows:
        await cq.answer("Буфер пуст.", show_alert=True)
        return
    await cq.answer(f"Открываю {len(rows)}...")
    for r in rows:
        try:
            item = json.loads(r["fb_json"])
        except Exception:
            continue
        try:
            await cq.bot.send_message(
                cq.from_user.id,
                _format_item(item, r["answer"] or "(не сгенерирован)", r["kind"], r["source"]),
                parse_mode="HTML",
                reply_markup=buffer_item_kb(r["source"], r["kind"], r["feedback_id"]),
            )
        except Exception:
            log.exception("send buffer item failed")


@router.callback_query(F.data == "clear_buffer")
async def cb_clear_buffer(cq: CallbackQuery, db: DB) -> None:
    deleted = await db.clear_notified(cq.from_user.id)
    await cq.answer(f"Удалено: {deleted}", show_alert=True)
    u = await db.get_user(cq.from_user.id)
    pending = await db.count_notified(cq.from_user.id)
    await cq.message.edit_reply_markup(
        reply_markup=settings_menu(_has_wb(u), _has_ozon(u), pending)
    )


# --------------------- Действия с элементом буфера ---------------------

@router.callback_query(F.data.startswith("send:"))
async def cb_send(cq: CallbackQuery, db: DB) -> None:
    _, source, kind, item_id = cq.data.split(":", 3)
    row = await db.get_notified(cq.from_user.id, item_id, source)
    if not row:
        await cq.answer("Элемент уже обработан.", show_alert=True)
        return
    u = await db.get_user(cq.from_user.id)
    try:
        if source == "ozon":
            oz = OzonClient(u["ozon_client_id"], u["ozon_api_key"])
            await oz.answer_review(item_id, row["answer"])
        else:
            wb = WBClient(u["wb_token"])
            if kind == "question":
                await wb.answer_question(item_id, row["answer"])
            else:
                await wb.answer(item_id, row["answer"])
    except (WBRateLimited, OzonRateLimited) as e:
        await cq.answer(f"Лимит: {e}", show_alert=True)
        return
    except (WBError, OzonError) as e:
        await cq.answer(f"Ошибка: {e}", show_alert=True)
        return
    await db.mark_answered(cq.from_user.id, item_id)
    await db.delete_notified(cq.from_user.id, item_id, source)
    await cq.message.edit_text(
        cq.message.html_text + "\n\n<b>✅ Ответ отправлен</b>",
        parse_mode="HTML",
    )
    await cq.answer("Отправлено")


@router.callback_query(F.data.startswith("regen:"))
async def cb_regen(cq: CallbackQuery, db: DB, gemini: GeminiClient) -> None:
    _, source, kind, item_id = cq.data.split(":", 3)
    row = await db.get_notified(cq.from_user.id, item_id, source)
    if not row:
        await cq.answer("Элемент не найден.", show_alert=True)
        return
    item = json.loads(row["fb_json"])
    u = await db.get_user(cq.from_user.id)
    await cq.answer("Перегенерирую...")
    ans = await _gen_for_item(gemini, u, item, kind, source)
    await db.update_notified_answer(cq.from_user.id, item_id, ans, source)
    await cq.message.edit_text(
        _format_item(item, ans, kind, source),
        parse_mode="HTML",
        reply_markup=buffer_item_kb(source, kind, item_id),
    )


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(cq: CallbackQuery, db: DB) -> None:
    _, source, kind, item_id = cq.data.split(":", 3)
    await db.mark_answered(cq.from_user.id, item_id)
    await db.delete_notified(cq.from_user.id, item_id, source)
    await cq.message.edit_text(
        cq.message.html_text + "\n\n<b>⏭ Пропущено</b>",
        parse_mode="HTML",
    )
    await cq.answer("Пропущено")
