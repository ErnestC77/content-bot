"""Публикация утверждённого поста в канал.

Защита от двойной публикации построена на атомарном compare-and-swap:
    UPDATE content_tasks SET status='publishing'
    WHERE id=:id AND status='approved' AND published_at IS NULL
Если обновлено 0 строк — публикация уже идёт или выполнена, выходим.
"""

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputMediaPhoto, InputMediaVideo
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ApprovalAction,
    ContentTask,
    MediaType,
    TaskStatus,
)
from app.services import audit
from app.services.media import TELEGRAM_CAPTION_LIMIT
from app.services.settings_store import get_channel_id

logger = logging.getLogger(__name__)

# Лимит обычного текстового сообщения Telegram
TELEGRAM_MESSAGE_LIMIT = 4096


class PublishError(Exception):
    """Публикация невозможна или не удалась (с понятным сообщением для владельца)."""


class PublishResult:
    def __init__(self, ok: bool, message: str) -> None:
        self.ok = ok
        self.message = message


async def _acquire_publishing_lock(session: AsyncSession, task_id: int) -> bool:
    """Атомарно переводит approved -> publishing. True, если удалось захватить."""
    result = await session.execute(
        update(ContentTask)
        .where(ContentTask.id == task_id)
        .where(ContentTask.status == TaskStatus.APPROVED.value)
        .where(ContentTask.published_at.is_(None))
        .values(status=TaskStatus.PUBLISHING.value)
    )
    return result.rowcount == 1


async def _check_bot_is_admin(bot: Bot, channel_id: str) -> None:
    try:
        me = await bot.me()
        member = await bot.get_chat_member(chat_id=channel_id, user_id=me.id)
    except TelegramAPIError as exc:
        raise PublishError(
            "Не удалось проверить права бота в канале. Убедитесь, что бот добавлен "
            f"в канал администратором. Детали: {exc}"
        ) from exc
    if member.status not in ("administrator", "creator"):
        raise PublishError(
            "Бот не является администратором канала. Добавьте бота в канал как "
            "администратора с правом публикации сообщений."
        )


async def _send_post(bot: Bot, channel_id: str, text: str, task: ContentTask) -> None:
    media = list(task.media)
    if not media:
        await bot.send_message(chat_id=channel_id, text=text)
        return

    if len(media) == 1:
        item = media[0]
        caption = text if len(text) <= TELEGRAM_CAPTION_LIMIT else None
        if item.media_type == MediaType.PHOTO.value:
            await bot.send_photo(chat_id=channel_id, photo=item.telegram_file_id, caption=caption)
        elif item.media_type == MediaType.VIDEO.value:
            await bot.send_video(chat_id=channel_id, video=item.telegram_file_id, caption=caption)
        else:
            await bot.send_document(chat_id=channel_id, document=item.telegram_file_id, caption=caption)
        if caption is None:
            await _send_long_text(bot, channel_id, text)
        return

    # Несколько медиа -> media group. Caption у первого, если помещается.
    caption = text if len(text) <= TELEGRAM_CAPTION_LIMIT else None
    group = []
    for idx, item in enumerate(media):
        item_caption = caption if idx == 0 else None
        if item.media_type == MediaType.VIDEO.value:
            group.append(InputMediaVideo(media=item.telegram_file_id, caption=item_caption))
        else:
            group.append(InputMediaPhoto(media=item.telegram_file_id, caption=item_caption))
    await bot.send_media_group(chat_id=channel_id, media=group)
    if caption is None:
        await _send_long_text(bot, channel_id, text)


async def _send_long_text(bot: Bot, channel_id: str, text: str) -> None:
    for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
        await bot.send_message(chat_id=channel_id, text=text[i : i + TELEGRAM_MESSAGE_LIMIT])


async def publish_task(bot: Bot, session: AsyncSession, task: ContentTask) -> PublishResult:
    """Публикует утверждённый пост. Задача уже должна быть в статусе approved.

    Возвращает PublishResult. Сам управляет транзакцией (commit внутри).
    """
    # Предварительные проверки (по ТЗ)
    channel_id = await get_channel_id(session)
    if not channel_id:
        return PublishResult(False, "Не указан ID канала. Задайте его в настройках.")

    if not task.posts:
        return PublishResult(False, "Нет сгенерированной версии поста для публикации.")

    try:
        await _check_bot_is_admin(bot, channel_id)
    except PublishError as exc:
        return PublishResult(False, str(exc))

    # Атомарный захват: approved -> publishing
    locked = await _acquire_publishing_lock(session, task.id)
    if not locked:
        await session.rollback()
        return PublishResult(False, "Публикация уже выполняется или пост уже опубликован.")

    await audit.log_action(
        session,
        task.id,
        ApprovalAction.PUBLISH_STARTED.value,
        old_status=TaskStatus.APPROVED.value,
        new_status=TaskStatus.PUBLISHING.value,
    )
    await session.commit()
    await session.refresh(task)

    text = task.posts[-1].text
    try:
        await _send_post(bot, channel_id, text, task)
    except TelegramAPIError as exc:
        logger.exception("Ошибка публикации в канал")
        task.status = TaskStatus.PUBLISH_FAILED.value
        await audit.log_action(
            session,
            task.id,
            ApprovalAction.PUBLISH_FAILED.value,
            old_status=TaskStatus.PUBLISHING.value,
            new_status=TaskStatus.PUBLISH_FAILED.value,
            comment=str(exc),
        )
        await session.commit()
        return PublishResult(False, f"Не удалось опубликовать пост: {exc}")

    from datetime import datetime, timezone

    task.status = TaskStatus.PUBLISHED.value
    task.published_at = datetime.now(timezone.utc)
    task.final_text = text
    task.approved_post_version_id = task.posts[-1].id
    await audit.log_action(
        session,
        task.id,
        ApprovalAction.PUBLISHED.value,
        old_status=TaskStatus.PUBLISHING.value,
        new_status=TaskStatus.PUBLISHED.value,
    )
    await session.commit()
    return PublishResult(True, "Пост опубликован в канал ✅")
