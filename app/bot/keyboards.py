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
CB_RUBRIC = "rubric"          # rubric:<name>
CB_DATE_TODAY = "date_today"
CB_SKIP = "skip"             # пропустить необязательный шаг
CB_PICKTASK = "picktask"     # picktask:<task_id>
CB_ADDMEDIA_DONE = "addmedia_done"

RUBRICS = [
    "Новинка", "Закулисье", "Полезный пост", "Отзыв",
    "Вопрос подписчикам", "Специальное предложение", "Личный пост владельца",
]


def rubrics_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=r, callback_data=f"{CB_RUBRIC}:{i}")]
            for i, r in enumerate(RUBRICS)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def date_today_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📅 Сегодня", callback_data=CB_DATE_TODAY)]]
    )


def skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⏭ Пропустить", callback_data=CB_SKIP)]]
    )


def pick_task_kb(tasks) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        label = f"#{t.id} {t.publish_date} · {t.rubric or 'без рубрики'} · {t.topic or '—'}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"{CB_PICKTASK}:{t.id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def addmedia_done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data=CB_ADDMEDIA_DONE)]]
    )


def owner_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Календарь"), KeyboardButton(text="📝 Сгенерировать сейчас")],
            [KeyboardButton(text="➕ Добавить посты"), KeyboardButton(text="📎 Добавить медиа")],
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
