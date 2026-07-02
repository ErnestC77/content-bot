"""Рассылка уведомлений владельцу и всем активным админам."""

import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config.settings import get_settings
from app.database.session import get_session
from app.services import access

logger = logging.getLogger(__name__)


def panel_kb() -> InlineKeyboardMarkup | None:
    base = get_settings().effective_webhook_url.rstrip("/")
    if not base.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗂 Открыть панель", web_app=WebAppInfo(url=f"{base}/webapp"))
    ]])


async def broadcast(bot: Bot, text: str, with_panel: bool = True) -> None:
    """Шлёт сообщение владельцу и всем активным админам (кто не начал бота — пропускается)."""
    async with get_session() as session:
        ids = await access.recipient_ids(session)
    kb = panel_kb() if with_panel else None
    for uid in ids:
        try:
            await bot.send_message(uid, text, reply_markup=kb)
        except Exception:
            logger.warning("Не доставлено уведомление user_id=%s (не начал бота?)", uid)
