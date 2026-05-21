from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

TONES = [
    ("friendly",   "😊 Дружелюбный"),
    ("formal",     "🎩 Формальный"),
    ("neutral",    "📄 Нейтральный"),
    ("apologetic", "🙏 Извиняющийся"),
    ("fun",        "✨ С юмором"),
]

STYLES = [
    ("short",  "✂️ Коротко"),
    ("medium", "📝 Средне"),
    ("long",   "📰 Развёрнуто"),
    ("emoji",  "🎉 С эмодзи"),
]

RATING_FILTERS = [
    ("all",  "Все"),
    ("neg",  "Только негативные (1-3⭐)"),
    ("pos",  "Только позитивные (4-5⭐)"),
]


def main_menu(
    has_wb: bool, has_ozon: bool, pending: int, auto_send: bool
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    label = "🔄 Обновить"
    if has_wb and has_ozon:
        label = "🔄 Обновить (WB + Ozon)"
    elif has_wb:
        label = "🔄 Обновить с WB"
    elif has_ozon:
        label = "🔄 Обновить с Ozon"
    b.button(text=label, callback_data="refresh")
    if pending > 0:
        b.button(text=f"📂 Открыть буфер ({pending})", callback_data="open_buffer")
    send_label = "⚡ Авто-отправка: ВКЛ" if auto_send else "🚫 Авто-отправка: ВЫКЛ"
    b.button(text=send_label, callback_data="toggle_send")
    b.button(text="⚙️ Настройки", callback_data="menu:settings")
    if has_wb or has_ozon:
        b.button(text="🔬 Проверить ключи", callback_data="check_keys")
    has_keys = has_wb or has_ozon
    if has_keys and pending > 0:
        b.adjust(1, 1, 1, 2)
    elif has_keys:
        b.adjust(1, 1, 2)
    elif pending > 0:
        b.adjust(1, 1, 1, 1)
    else:
        b.adjust(1, 1, 1)
    return b.as_markup()


def settings_menu(
    has_wb: bool, has_ozon: bool, pending: int
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎭 Тон ответа", callback_data="menu:tone")
    b.button(text="🪶 Стиль ответа", callback_data="menu:style")
    b.button(text="🔍 Фильтр оценок", callback_data="menu:rating")
    b.button(text="✍️ Подпись магазина", callback_data="set_signature")
    wb_label = "🟣 Сменить WB-токен" if has_wb else "🟣 Задать WB-токен"
    b.button(text=wb_label, callback_data="set_wb_token")
    ozon_label = "🟦 Сменить Ozon-ключи" if has_ozon else "🟦 Задать Ozon-ключи"
    b.button(text=ozon_label, callback_data="set_ozon_keys")
    if pending > 0:
        b.button(text=f"🗑 Очистить буфер ({pending})", callback_data="clear_buffer")
    b.button(text="ℹ️ Текущие настройки", callback_data="show_settings")
    b.button(text="⬅️ В главное меню", callback_data="back_main")
    b.adjust(2, 2, 2, 1, 1, 1)
    return b.as_markup()


def choices_kb(options: list[tuple[str, str]], prefix: str, current: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in options:
        mark = "✅ " if code == current else ""
        b.button(text=f"{mark}{label}", callback_data=f"{prefix}:{code}")
    b.button(text="⬅️ Назад", callback_data="menu:settings")
    b.adjust(1)
    return b.as_markup()


def tone_kb(current: str) -> InlineKeyboardMarkup:
    return choices_kb(TONES, "tone", current)


def style_kb(current: str) -> InlineKeyboardMarkup:
    return choices_kb(STYLES, "style", current)


def rating_kb(current: str) -> InlineKeyboardMarkup:
    return choices_kb(RATING_FILTERS, "rating", current)


def buffer_item_kb(source: str, kind: str, item_id: str) -> InlineKeyboardMarkup:
    """Кнопки для одного элемента буфера. source: wb|ozon, kind: feedback|question|review."""
    b = InlineKeyboardBuilder()
    payload = f"{source}:{kind}:{item_id}"
    b.button(text="✅ Отправить", callback_data=f"send:{payload}")
    b.button(text="🔄 Перегенерировать", callback_data=f"regen:{payload}")
    b.button(text="⏭ Пропустить", callback_data=f"skip:{payload}")
    b.adjust(2, 1)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")
    ]])
