from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

# Callback data
CB_APPROVE = "approve"
CB_REVISION = "revision"
CB_ALTERNATIVE = "alternative"
CB_CANCEL = "cancel"
CB_ANSWERS_DONE = "answers_done"
CB_MEDIA_DONE = "media_done"
CB_MEDIA_SKIP = "media_skip"


def owner_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Календарь"), KeyboardButton(text="📝 Задача на сегодня")],
            [KeyboardButton(text="➕ Добавить пост"), KeyboardButton(text="📎 Добавить медиа")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📢 Канал")],
        ],
        resize_keyboard=True,
    )


def approval_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Одобряю", callback_data=f"{CB_APPROVE}:{task_id}")],
            [InlineKeyboardButton(text="✏️ Правки", callback_data=f"{CB_REVISION}:{task_id}")],
            [InlineKeyboardButton(text="🔄 Другой вариант", callback_data=f"{CB_ALTERNATIVE}:{task_id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"{CB_CANCEL}:{task_id}")],
        ]
    )


def answers_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Готово с ответами", callback_data=CB_ANSWERS_DONE)]
        ]
    )


def media_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Медиа добавлено, генерируем", callback_data=CB_MEDIA_DONE)],
            [InlineKeyboardButton(text="⏭ Без медиа", callback_data=CB_MEDIA_SKIP)],
        ]
    )
