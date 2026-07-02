"""Middleware доступа: пропускает владельца и активных админов (таблица users)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.database.session import get_session
from app.services.access import is_authorized

logger = logging.getLogger(__name__)


class AccessMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        allowed = False
        if user is not None:
            async with get_session() as session:
                allowed = await is_authorized(session, user.id)
        if not allowed:
            logger.warning("Отклонён доступ для user_id=%s", getattr(user, "id", None))
            if isinstance(event, Message):
                await event.answer("Доступ запрещён. Обратитесь к владельцу канала.")
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён.", show_alert=True)
            return None
        return await handler(event, data)
