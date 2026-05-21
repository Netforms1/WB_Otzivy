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
    auto_enabled: bool,
    has_token: bool,
    pending: int = 0,
    auto_send: bool = False,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if pending > 0:
        b.button(text=f"📂 Открыть сохранённые ({pending})", callback_data="open_saved")
    b.button(text="📋 Показать новые отзывы", callback_data="show:0")
    b.button(text="❓ Показать новые вопросы", callback_data="show_questions")
    b.button(text="🔄 Проверить отзывы сейчас", callback_data="check_now")
    auto_label = "🟢 Авто-показ в чат: ВКЛ" if auto_enabled else "⚪️ Авто-показ в чат: ВЫКЛ"
    b.button(text=auto_label, callback_data="toggle_auto")
    send_label = "⚡ Авто-отправка на WB: ВКЛ" if auto_send else "🚫 Авто-отправка на WB: ВЫКЛ"
    b.button(text=send_label, callback_data="toggle_send")
    b.button(text="🎭 Тон ответа", callback_data="menu:tone")
    b.button(text="🪶 Стиль ответа", callback_data="menu:style")
    b.button(text="🔍 Фильтр оценок", callback_data="menu:rating")
    b.button(text="✍️ Подпись магазина", callback_data="set_signature")
    token_label = "🔑 WB-токен: задан ✅" if has_token else "🔑 Задать WB-токен"
    b.button(text=token_label, callback_data="set_token")
    if has_token:
        b.button(text="🔬 Проверить WB-токен", callback_data="check_token")
    b.button(text="ℹ️ Текущие настройки", callback_data="show_settings")
    b.button(text="🗑 Сбросить историю показов", callback_data="reset_notified")
    # Лейаут: каждая кнопка на свою строку, кроме явно парных
    if has_token:
        if pending > 0:
            b.adjust(1, 1, 1, 1, 2, 2, 1, 1, 1, 2, 1, 1)
        else:
            b.adjust(1, 1, 1, 2, 2, 1, 1, 1, 2, 1, 1)
    else:
        if pending > 0:
            b.adjust(1, 1, 1, 1, 2, 2, 1, 1, 1, 1, 1)
        else:
            b.adjust(1, 1, 1, 2, 2, 1, 1, 1, 1, 1)
    return b.as_markup()


def choices_kb(options: list[tuple[str, str]], prefix: str, current: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for code, label in options:
        mark = "✅ " if code == current else ""
        b.button(text=f"{mark}{label}", callback_data=f"{prefix}:{code}")
    b.button(text="⬅️ Назад", callback_data="back_main")
    b.adjust(1)
    return b.as_markup()


def tone_kb(current: str) -> InlineKeyboardMarkup:
    return choices_kb(TONES, "tone", current)


def style_kb(current: str) -> InlineKeyboardMarkup:
    return choices_kb(STYLES, "style", current)


def rating_kb(current: str) -> InlineKeyboardMarkup:
    return choices_kb(RATING_FILTERS, "rating", current)


def feedback_kb(feedback_id: str, idx: int, total: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить", callback_data=f"send:{feedback_id}")
    b.button(text="🔄 Перегенерировать", callback_data=f"regen:{feedback_id}")
    b.button(text="⏭ Пропустить", callback_data=f"skip:{idx}")
    if idx + 1 < total:
        b.button(text=f"➡️ Следующий ({idx + 2}/{total})", callback_data=f"show:{idx + 1}")
    b.button(text="🏠 В меню", callback_data="back_main")
    b.adjust(2, 1, 1, 1)
    return b.as_markup()


def question_kb(question_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить", callback_data=f"qsend:{question_id}")
    b.button(text="🔄 Перегенерировать", callback_data=f"qregen:{question_id}")
    b.button(text="⏭ Пропустить", callback_data=f"qskip:{question_id}")
    b.adjust(2, 1)
    return b.as_markup()


def push_feedback_kb(feedback_id: str) -> InlineKeyboardMarkup:
    """Кнопки для отзыва, который пришёл сам (авто-показ)."""
    b = InlineKeyboardBuilder()
    b.button(text="✅ Отправить", callback_data=f"psend:{feedback_id}")
    b.button(text="🔄 Перегенерировать", callback_data=f"pregen:{feedback_id}")
    b.button(text="⏭ Пропустить", callback_data=f"pskip:{feedback_id}")
    b.adjust(2, 1)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")
    ]])
