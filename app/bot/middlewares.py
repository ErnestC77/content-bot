"""Middleware безопасности: пропускает только владельца.

Проверяет telegram_id пользователя на каждое событие (по ТЗ, раздел БЕЗОПАСНОСТЬ).
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.config.settings import get_settings

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        owner_id = get_settings().owner_telegram_id
        user = data.get("event_from_user")
        if user is None or user.id != owner_id:
            logger.warning("Отклонён доступ для user_id=%s", getattr(user, "id", None))
            if isinstance(event, Message):
                await event.answer("Доступ запрещён. Этот бот доступен только владельцу канала.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён.", show_alert=True)
            return None
        return await handler(event, data)
