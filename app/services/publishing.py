"""Публикация утверждённого поста в канал.

Защита от двойной публикации построена на атомарном compare-and-swap:
    UPDATE content_tasks SET status='publishing'
    WHERE id=:id AND status='approved' AND published_at IS NULL
Если обновлено 0 строк — публикация уже идёт или выполнена, выходим.
"""

import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, InputMediaPhoto, InputMediaVideo
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    ApprovalAction,
    ContentTask,
    MediaType,
    TaskStatus,
    TaskType,
)
from app.services import audit, content_tasks
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


def _wrap_quote(text: str) -> str:
    """Оборачивает текст в нативную цитату Telegram (HTML blockquote)."""
    return f"<blockquote>{html.escape(text)}</blockquote>"


def _wrap_fragments(text: str, fragments: list[str]) -> str:
    """Оборачивает несколько (в т.ч. несмежных) строк текста в отдельные
    Telegram blockquote, остальной текст остаётся обычным.

    Каждый фрагмент ищется независимо через str.find — если строка
    встречается в посте несколько раз, берётся первое вхождение. Если два
    найденных фрагмента пересекаются (частный случай задвоенных строк),
    второй пропускается, чтобы не сломать HTML.
    """
    spans: list[tuple[int, int]] = []
    for fragment in fragments:
        idx = text.find(fragment)
        if idx == -1:
            continue
        end = idx + len(fragment)
        if any(idx < e and s < end for s, e in spans):
            continue
        spans.append((idx, end))
    spans.sort()

    parts = []
    cursor = 0
    for start, end in spans:
        parts.append(html.escape(text[cursor:start]))
        parts.append(_wrap_quote(text[start:end]))
        cursor = end
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)


def _render_quote(text: str, task: ContentTask) -> tuple[str, bool]:
    """Строит текст для отправки с учётом цитаты. Возвращает (display, use_html).

    Приоритет: quote_text (одна или несколько выделенных строк, каждая — если
    ещё встречается в текущем тексте) > is_quote (весь текст целиком) >
    обычный текст без разметки.
    """
    raw = (task.quote_text or "").strip()
    fragments = [line.strip() for line in raw.split("\n") if line.strip()]
    fragments = [f for f in fragments if f in text]
    if fragments:
        return _wrap_fragments(text, fragments), True
    if task.is_quote:
        return _wrap_quote(text), True
    return text, False


async def _send_post(bot: Bot, channel_id: str, text: str, task: ContentTask) -> None:
    media = list(task.media)

    if not media:
        await _send_long_text(bot, channel_id, text, task)
        return

    # Фото/видео с текстом всегда уходят ОДНИМ сообщением: подпись Telegram
    # ограничена 1024 символами (лимит платформы), поэтому длинный текст
    # аккуратно обрезается по границе слова, а не разносится вторым сообщением.
    caption, parse_mode = _build_caption(text, task)

    if len(media) == 1:
        item = media[0]
        src = _media_source(item)
        if item.media_type == MediaType.PHOTO.value:
            await bot.send_photo(chat_id=channel_id, photo=src, caption=caption, parse_mode=parse_mode)
        elif item.media_type == MediaType.VIDEO.value:
            await bot.send_video(chat_id=channel_id, video=src, caption=caption, parse_mode=parse_mode)
        else:
            await bot.send_document(chat_id=channel_id, document=src, caption=caption, parse_mode=parse_mode)
        return

    # Несколько медиа -> media group. Caption у первого.
    group = []
    for idx, item in enumerate(media):
        item_caption = caption if idx == 0 else None
        src = _media_source(item)
        if item.media_type == MediaType.VIDEO.value:
            group.append(InputMediaVideo(media=src, caption=item_caption, parse_mode=parse_mode))
        else:
            group.append(InputMediaPhoto(media=src, caption=item_caption, parse_mode=parse_mode))
    await bot.send_media_group(chat_id=channel_id, media=group)


async def _send_poll(bot: Bot, channel_id: str, text: str, task: ContentTask) -> None:
    """Публикует опрос: text — сериализованный черновик (вопрос + варианты,
    см. content_tasks.parse_poll_draft). task не используется — параметр
    только ради единой сигнатуры с _send_post (общая точка вызова в publish_task)."""
    question, options = content_tasks.parse_poll_draft(text)
    await bot.send_poll(
        chat_id=channel_id,
        question=question,
        options=options,
        is_anonymous=True,
        allows_multiple_answers=False,
    )


def _truncate_at_word(text: str, limit: int) -> str:
    """Обрезает текст до limit символов по границе слова (не рвёт на полуслове)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.6:
        cut = cut[:last_space]
    return cut.rstrip() + "…"


def _build_caption(text: str, task: ContentTask) -> tuple[str, str | None]:
    """Подпись к медиа: весь текст (с цитатой, если задана), если помещается
    в лимит Telegram (1024 симв.). Иначе — безопасный фолбэк: обычная обрезка
    по слову БЕЗ разметки цитаты (чтобы не оборвать HTML-тег на полуслове).
    Полный текст поста при этом хранится в БД и виден в Mini App без обрезки —
    обрезается только то, что уходит в канал.
    """
    display, use_html = _render_quote(text, task)
    if len(display) <= TELEGRAM_CAPTION_LIMIT:
        return display, ("HTML" if use_html else None)
    return _truncate_at_word(text, TELEGRAM_CAPTION_LIMIT), None


def _media_source(item):
    """Источник для отправки: telegram_file_id или загруженные байты."""
    if item.telegram_file_id:
        return item.telegram_file_id
    ext = {"image/jpeg": "jpg", "image/png": "png", "video/mp4": "mp4"}.get(item.mime_type or "", "bin")
    return BufferedInputFile(item.content or b"", filename=f"media_{item.id}.{ext}")


async def _send_long_text(bot: Bot, channel_id: str, text: str, task: ContentTask) -> None:
    display, use_html = _render_quote(text, task)
    if len(display) <= TELEGRAM_MESSAGE_LIMIT:
        await bot.send_message(chat_id=channel_id, text=display, parse_mode="HTML" if use_html else None)
        return
    # Не помещается даже в одно сообщение — безопасный фолбэк: обычные куски
    # без разметки цитаты (чтобы не оборвать HTML-тег между сообщениями).
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
    send = _send_poll if task.task_type == TaskType.POLL.value else _send_post
    try:
        await send(bot, channel_id, text, task)
    except (TelegramAPIError, content_tasks.PollValidationError) as exc:
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
