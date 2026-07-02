"""Хендлеры бота — минимум. Весь рабочий процесс в Mini App.

Бот отвечает за: /start, открытие панели, авто-определение канала и подсказки.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.bot import keyboards as kb
from app.config.settings import get_settings
from app.database.session import get_session
from app.services.settings_store import KEY_CHANNEL_ID, get_channel_id, set_setting

logger = logging.getLogger(__name__)
router = Router()


def _panel_url() -> str:
    base = get_settings().effective_webhook_url.rstrip("/")
    return f"{base}/webapp" if base.startswith("https://") else ""


def _panel_kb() -> InlineKeyboardMarkup | None:
    url = _panel_url()
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗂 Открыть панель", web_app=WebAppInfo(url=url))
    ]])


# ---------- Авто-определение канала (только для владельца) ----------


async def _remember_channel(bot, chat_id: int, title: str) -> None:
    async with get_session() as session:
        await set_setting(session, KEY_CHANNEL_ID, str(chat_id))
        await session.commit()
    try:
        await bot.send_message(
            get_settings().owner_telegram_id,
            f"✅ Канал подключён: {title or chat_id} (id {chat_id}).",
        )
    except Exception:
        logger.exception("Не удалось уведомить владельца о канале")


async def _owner_is_admin(bot, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, get_settings().owner_telegram_id)
    except Exception:
        return False
    return member.status in ("administrator", "creator")


@router.my_chat_member()
async def on_bot_status_changed(event: ChatMemberUpdated) -> None:
    if event.chat.type != "channel":
        return
    if event.from_user is None or event.from_user.id != get_settings().owner_telegram_id:
        return
    if event.new_chat_member.status in ("administrator", "creator"):
        await _remember_channel(event.bot, event.chat.id, event.chat.title)


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    async with get_session() as session:
        if await get_channel_id(session):
            return
    if not await _owner_is_admin(message.bot, message.chat.id):
        return
    await _remember_channel(message.bot, message.chat.id, message.chat.title)


# ---------- Команды ----------


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я AI-редактор вашего Telegram-канала.\n\n"
        "Всё управление — в панели: расписание постов, черновики, согласование, "
        "медиа и публикация. Откройте её кнопкой ниже или в меню Telegram.",
        reply_markup=kb.owner_menu(),
    )
    await cmd_panel(message)


@router.message(Command("panel"))
@router.message(Command("admin"))
@router.message(F.text == "🗂 Панель")
async def cmd_panel(message: Message) -> None:
    panel = _panel_kb()
    if panel is not None:
        await message.answer("Панель управления контентом:", reply_markup=panel)
    else:
        await message.answer("Панель доступна только по https (на проде). Локально Mini App недоступен.")


@router.message()
async def fallback(message: Message) -> None:
    """Любое другое сообщение — направляем в панель."""
    await message.answer("Всё управление — в панели 👇", reply_markup=kb.owner_menu())
    panel = _panel_kb()
    if panel is not None:
        await message.answer("Открыть:", reply_markup=panel)
