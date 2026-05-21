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
from .wb_api import WBClient, WBError, WBRateLimited

log = logging.getLogger(__name__)
router = Router()


# user_id -> {"feedbacks": int, "questions": int, "ts": datetime}
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
    waiting = State()


class SignatureInput(StatesGroup):
    waiting = State()


# --------------------- Тексты ---------------------

def _filter_by_rating(feedbacks: list[dict], rating_filter: str) -> list[dict]:
    if rating_filter == "neg":
        return [f for f in feedbacks if (f.get("productValuation") or 0) <= 3]
    if rating_filter == "pos":
        return [f for f in feedbacks if (f.get("productValuation") or 0) >= 4]
    return feedbacks


def _format_item(fb: dict, answer: str, kind: str) -> str:
    product = (fb.get("productDetails") or {}).get("productName") or "—"
    text = (fb.get("text") or "(без текста)")[:1500]
    author = fb.get("userName") or "Покупатель"
    if kind == "question":
        header = "❓ <b>Вопрос</b>"
        rating_line = ""
    else:
        rating = fb.get("productValuation") or 0
        stars = "⭐" * rating + "☆" * (5 - rating)
        header = "📬 <b>Отзыв</b>"
        rating_line = f"{stars} ({rating}/5)\n"
    return (
        f"{header}\n"
        f"{rating_line}"
        f"👤 <i>{html.escape(author)}</i>\n"
        f"📦 <i>{html.escape(product)}</i>\n\n"
        f"<b>Текст:</b>\n{html.escape(text)}\n\n"
        f"<b>🤖 Ответ:</b>\n{html.escape(answer)}"
    )


async def _menu_text(db: DB, user_id: int) -> str:
    u = await db.get_user(user_id)
    answered = await db.count_answered(user_id)
    counts = await db.count_notified_by_kind(user_id)
    pending_total = sum(counts.values())
    info = last_check.get(user_id)

    lines = ["🏠 <b>WB Бот-автоответчик</b>", ""]
    if info:
        ago_sec = (datetime.now(timezone.utc) - info["ts"]).total_seconds()
        ago = f"{int(ago_sec // 60)} мин назад" if ago_sec >= 60 else f"{int(ago_sec)} сек назад"
        lines.append(f"📬 Отзывов в WB: <b>{info.get('feedbacks', '?')}</b>")
        lines.append(f"❓ Вопросов в WB: <b>{info.get('questions', '?')}</b>")
        lines.append(f"   <i>обновлено {ago}</i>")
    else:
        lines.append("📬 Ещё не обновлял. Нажми «🔄 Обновить с WB».")
    lines.append("")
    lines.append(f"📂 В буфере: <b>{pending_total}</b> (готовы к отправке)")
    if pending_total > 0:
        parts = []
        if counts.get("feedback"):
            parts.append(f"{counts['feedback']} отз.")
        if counts.get("question"):
            parts.append(f"{counts['question']} вопр.")
        lines.append(f"   <i>{', '.join(parts)}</i>")
    lines.append(f"✅ Отправлено через бота: <b>{answered}</b>")

    if u and u["wb_token"]:
        wb = WBClient(u["wb_token"])
        left = wb.cooldown_left()
        if left:
            lines.append("")
            lines.append(f"⏳ WB на cooldown ещё ~{left // 60 + 1} мин")
    if not u or not u["wb_token"]:
        lines.append("")
        lines.append("⚠️ Сначала задай WB-токен в «⚙️ Настройки»")
    return "\n".join(lines)


async def _build_kb(db: DB, user_id: int):
    u = await db.get_user(user_id)
    pending = await db.count_notified(user_id)
    return main_menu(bool(u["wb_token"]), pending, bool(u["auto_send"]))


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
        reply_markup=settings_menu(bool(u["wb_token"]), pending),
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
        f"🔑 WB-токен: {'задан ✅' if u['wb_token'] else 'не задан ❌'}\n"
        f"⚡ Авто-отправка: {'ВКЛ' if u['auto_send'] else 'ВЫКЛ'}"
    )
    await cq.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=settings_menu(bool(u["wb_token"]), pending),
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


# --------------------- Токен ---------------------

@router.callback_query(F.data == "set_token")
async def cb_set_token(cq: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(TokenInput.waiting)
    await cq.message.edit_text(
        "🔑 Пришли WB API-токен (категория «Отзывы и вопросы»).\n\n"
        "<b>Где взять:</b> ЛК продавца → Настройки → Доступ к API → Создать токен.",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    await cq.answer()


@router.message(TokenInput.waiting)
async def msg_token(msg: Message, state: FSMContext, db: DB) -> None:
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
    await msg.answer(
        "✅ Токен сохранён. Жми «🔬 Проверить токен» чтобы убедиться, что он валиден."
    )
    await _show_main(msg, db, msg.from_user.id)


@router.callback_query(F.data == "check_token")
async def cb_check_token(cq: CallbackQuery, db: DB) -> None:
    u = await db.get_user(cq.from_user.id)
    if not u["wb_token"]:
        await cq.answer("Токен не задан.", show_alert=True)
        return
    await cq.answer("Проверяю...")
    wb = WBClient(u["wb_token"])
    status, info = await wb.check()
    icons = {"ok": "✅", "rate_limited": "⏳", "unauthorized": "❌",
             "network": "📡", "error": "⚠️"}
    titles = {
        "ok": "Токен валиден",
        "rate_limited": "Токен валиден, но WB на cooldown",
        "unauthorized": "Токен невалиден",
        "network": "Сетевая ошибка",
        "error": "Ошибка WB",
    }
    await cq.bot.send_message(
        cq.from_user.id,
        f"{icons.get(status, '❓')} <b>{titles.get(status, status)}</b>\n{html.escape(info)}",
        parse_mode="HTML",
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
    if not u["wb_token"]:
        await cq.answer("Сначала задай WB-токен.", show_alert=True)
        return
    new_val = 0 if u["auto_send"] else 1
    await db.update_field(cq.from_user.id, "auto_send", new_val)
    await _show_main(cq, db, cq.from_user.id)
    if new_val:
        await cq.answer(
            "⚡ Авто-отправка ВКЛЮЧЕНА. После каждого «Обновить» бот будет САМ "
            "отправлять ответы из буфера на WB. Проверь тон/подпись!",
            show_alert=True,
        )
    else:
        await cq.answer("Авто-отправка выключена")


# --------------------- Обновление с WB ---------------------

async def _gen_feedback_answer(gemini: GeminiClient, u: dict, fb: dict) -> str:
    return await gemini.generate_answer(
        review_text=fb.get("text") or "",
        rating=fb.get("productValuation") or 5,
        product_name=(fb.get("productDetails") or {}).get("productName"),
        tone=u["tone"], style=u["style"], signature=u["signature"],
    )


async def _gen_question_answer(gemini: GeminiClient, u: dict, q: dict) -> str:
    return await gemini.generate_question_answer(
        question_text=q.get("text") or "",
        product_name=(q.get("productDetails") or {}).get("productName"),
        tone=u["tone"], style=u["style"], signature=u["signature"],
    )


async def _process_items(
    db: DB, gemini: GeminiClient, u: dict, items: list[dict], kind: str
) -> int:
    """Сгенерить ответы и сохранить в буфер. Возвращает число добавленных."""
    user_id = u["user_id"]
    added = 0
    for item in items:
        iid = item["id"]
        if await db.is_answered(user_id, iid) or await db.is_notified(user_id, iid):
            continue
        try:
            if kind == "feedback":
                ans = await _gen_feedback_answer(gemini, u, item)
            else:
                ans = await _gen_question_answer(gemini, u, item)
        except GeminiError as e:
            log.warning("Gemini failed: %s", e)
            ans = f"[Ошибка генерации: {e}]"
        await db.add_notified(user_id, iid, ans, json.dumps(item, ensure_ascii=False), kind)
        added += 1
    return added


async def _auto_send_buffer(db: DB, wb: WBClient, user_id: int) -> tuple[int, str | None]:
    """Отправить всё из буфера на WB. (sent, error_msg)."""
    rows = await db.list_notified(user_id)
    sent = 0
    for r in rows:
        try:
            if r["kind"] == "question":
                await wb.answer_question(r["feedback_id"], r["answer"])
            else:
                await wb.answer(r["feedback_id"], r["answer"])
        except WBRateLimited as e:
            return sent, f"WB лимит: {e}"
        except WBError as e:
            log.warning("WB send failed: %s", e)
            continue
        await db.mark_answered(user_id, r["feedback_id"])
        await db.delete_notified(user_id, r["feedback_id"])
        sent += 1
        await asyncio.sleep(1.5)
    return sent, None


@router.callback_query(F.data == "refresh")
async def cb_refresh(
    cq: CallbackQuery, db: DB, gemini: GeminiClient, settings: Settings
) -> None:
    u = await db.get_user(cq.from_user.id)
    if not u["wb_token"]:
        await cq.answer("Сначала задай WB-токен.", show_alert=True)
        return
    wb = WBClient(u["wb_token"])
    left = wb.cooldown_left()
    if left > 0:
        await cq.answer(
            f"WB на cooldown ещё ~{left // 60 + 1} мин. Подожди.",
            show_alert=True,
        )
        return

    await cq.answer("Обновляю...")
    progress = await cq.bot.send_message(
        cq.from_user.id, "⏳ Загружаю отзывы с WB..."
    )

    # 1. Отзывы
    f_count = 0
    f_added = 0
    f_err: str | None = None
    try:
        feedbacks = await wb.get_unanswered(take=settings.batch_size)
        f_count = len(feedbacks)
        feedbacks = _filter_by_rating(feedbacks, u["answer_rating"])
        await progress.edit_text(
            f"⏳ Отзывов получено: {f_count}, генерирую ответы..."
        )
        f_added = await _process_items(db, gemini, u, feedbacks, "feedback")
    except WBRateLimited as e:
        f_err = f"лимит ({e})"
    except WBError as e:
        f_err = str(e)

    # Пауза между WB-вызовами
    await progress.edit_text("⏳ Загружаю вопросы с WB...")
    await asyncio.sleep(5)

    # 2. Вопросы
    q_count = 0
    q_added = 0
    q_err: str | None = None
    left = wb.cooldown_left()
    if left > 0:
        q_err = "WB cooldown после отзывов"
    else:
        try:
            questions = await wb.get_questions(take=settings.batch_size)
            q_count = len(questions)
            q_added = await _process_items(db, gemini, u, questions, "question")
        except WBRateLimited as e:
            q_err = f"лимит ({e})"
        except WBError as e:
            q_err = str(e)

    # Сохраняем cache счётчиков
    last_check[cq.from_user.id] = {
        "feedbacks": f_count if f_err is None else last_check.get(cq.from_user.id, {}).get("feedbacks", "?"),
        "questions": q_count if q_err is None else last_check.get(cq.from_user.id, {}).get("questions", "?"),
        "ts": datetime.now(timezone.utc),
    }

    # Сводка
    summary_lines = ["📊 <b>Готово</b>"]
    if f_err:
        summary_lines.append(f"📬 Отзывы: ❌ {html.escape(f_err)}")
    else:
        summary_lines.append(f"📬 Отзывы: {f_count} в WB, {f_added} новых в буфер")
    if q_err:
        summary_lines.append(f"❓ Вопросы: ❌ {html.escape(q_err)}")
    else:
        summary_lines.append(f"❓ Вопросы: {q_count} в WB, {q_added} новых в буфер")

    # Авто-отправка
    if u["auto_send"] and (f_added or q_added or await db.count_notified(cq.from_user.id)):
        sent, send_err = await _auto_send_buffer(db, wb, cq.from_user.id)
        summary_lines.append(f"⚡ Отправлено на WB: {sent}")
        if send_err:
            summary_lines.append(f"   <i>{html.escape(send_err)}</i>")

    pending = await db.count_notified(cq.from_user.id)
    if pending and not u["auto_send"]:
        summary_lines.append(f"\n📂 В буфере: <b>{pending}</b>. Жми «Открыть буфер».")

    await progress.edit_text("\n".join(summary_lines), parse_mode="HTML")
    await _show_main(cq, db, cq.from_user.id)


# --------------------- Открыть буфер ---------------------

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
                _format_item(item, r["answer"] or "(не сгенерирован)", r["kind"]),
                parse_mode="HTML",
                reply_markup=buffer_item_kb(r["kind"], r["feedback_id"]),
            )
        except Exception:
            log.exception("send buffer item failed")


# --------------------- Очистка буфера ---------------------

@router.callback_query(F.data == "clear_buffer")
async def cb_clear_buffer(cq: CallbackQuery, db: DB) -> None:
    deleted = await db.clear_notified(cq.from_user.id)
    await cq.answer(f"Удалено из буфера: {deleted}", show_alert=True)
    u = await db.get_user(cq.from_user.id)
    pending = await db.count_notified(cq.from_user.id)
    await cq.message.edit_reply_markup(
        reply_markup=settings_menu(bool(u["wb_token"]), pending)
    )


# --------------------- Действия с элементом буфера ---------------------

@router.callback_query(F.data.startswith("send:"))
async def cb_send(cq: CallbackQuery, db: DB) -> None:
    _, kind, item_id = cq.data.split(":", 2)
    row = await db.get_notified(cq.from_user.id, item_id)
    if not row:
        await cq.answer("Элемент уже обработан.", show_alert=True)
        return
    u = await db.get_user(cq.from_user.id)
    wb = WBClient(u["wb_token"])
    try:
        if kind == "question":
            await wb.answer_question(item_id, row["answer"])
        else:
            await wb.answer(item_id, row["answer"])
    except WBRateLimited as e:
        await cq.answer(f"WB лимит: {e}", show_alert=True)
        return
    except WBError as e:
        await cq.answer(f"Ошибка WB: {e}", show_alert=True)
        return
    await db.mark_answered(cq.from_user.id, item_id)
    await db.delete_notified(cq.from_user.id, item_id)
    await cq.message.edit_text(
        cq.message.html_text + "\n\n<b>✅ Ответ отправлен</b>",
        parse_mode="HTML",
    )
    await cq.answer("Отправлено")


@router.callback_query(F.data.startswith("regen:"))
async def cb_regen(cq: CallbackQuery, db: DB, gemini: GeminiClient) -> None:
    _, kind, item_id = cq.data.split(":", 2)
    row = await db.get_notified(cq.from_user.id, item_id)
    if not row:
        await cq.answer("Элемент не найден.", show_alert=True)
        return
    item = json.loads(row["fb_json"])
    u = await db.get_user(cq.from_user.id)
    await cq.answer("Перегенерирую...")
    try:
        if kind == "question":
            ans = await _gen_question_answer(gemini, u, item)
        else:
            ans = await _gen_feedback_answer(gemini, u, item)
    except GeminiError as e:
        await cq.bot.send_message(cq.from_user.id, f"⚠️ Gemini: {e}")
        return
    await db.update_notified_answer(cq.from_user.id, item_id, ans)
    await cq.message.edit_text(
        _format_item(item, ans, kind),
        parse_mode="HTML",
        reply_markup=buffer_item_kb(kind, item_id),
    )


@router.callback_query(F.data.startswith("skip:"))
async def cb_skip(cq: CallbackQuery, db: DB) -> None:
    _, kind, item_id = cq.data.split(":", 2)
    await db.mark_answered(cq.from_user.id, item_id)
    await db.delete_notified(cq.from_user.id, item_id)
    await cq.message.edit_text(
        cq.message.html_text + "\n\n<b>⏭ Пропущено</b>",
        parse_mode="HTML",
    )
    await cq.answer("Пропущено")
