"""Сохранение медиа задачи (Telegram file_id) и подготовка к публикации."""

import mimetypes

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ContentTask, MediaType, TaskMedia

TELEGRAM_CAPTION_LIMIT = 1024


async def _next_order(session: AsyncSession, task_id: int) -> int:
    return await session.scalar(
        select(func.coalesce(func.max(TaskMedia.sort_order), -1) + 1).where(
            TaskMedia.task_id == task_id
        )
    ) or 0


async def add_media(
    session: AsyncSession,
    task: ContentTask,
    telegram_file_id: str,
    media_type: MediaType,
    caption: str | None = None,
) -> TaskMedia:
    media = TaskMedia(
        task_id=task.id,
        telegram_file_id=telegram_file_id,
        media_type=media_type.value,
        caption=caption,
        sort_order=await _next_order(session, task.id),
    )
    # Пишем через task_id, а не task.media.append(): для только что созданного
    # (ещё не запрошенного из БД) task обращение к relationship синхронно
    # может потребовать неявной ленивой загрузки, что ломается под AsyncSession
    # (MissingGreenlet). Актуальный count после добавления считаем отдельным
    # запросом — count_media(), а не через (потенциально несвежую) task.media.
    session.add(media)
    await session.flush()
    return media


async def add_media_bytes(
    session: AsyncSession,
    task: ContentTask,
    content: bytes,
    mime_type: str,
    media_type: MediaType,
    caption: str | None = None,
) -> TaskMedia:
    """Сохраняет загруженный из Mini App файл (байты) — публикуется из БД."""
    media = TaskMedia(
        task_id=task.id,
        content=content,
        mime_type=mime_type,
        media_type=media_type.value,
        caption=caption,
        sort_order=await _next_order(session, task.id),
    )
    session.add(media)
    await session.flush()
    return media


async def count_media(session: AsyncSession, task_id: int) -> int:
    """Актуальное число медиа задачи — прямой запрос, без риска устаревшей коллекции."""
    return await session.scalar(
        select(func.count()).select_from(TaskMedia).where(TaskMedia.task_id == task_id)
    ) or 0


def media_type_from_mime(mime: str | None, filename: str | None = None) -> MediaType:
    """Определяет тип медиа по MIME, с запасным вариантом по расширению файла

    (некоторые мобильные браузеры шлют video/* как application/octet-stream)."""
    mime = mime or ""
    if mime.startswith("image/"):
        return MediaType.PHOTO
    if mime.startswith("video/"):
        return MediaType.VIDEO
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            if guessed.startswith("image/"):
                return MediaType.PHOTO
            if guessed.startswith("video/"):
                return MediaType.VIDEO
    return MediaType.DOCUMENT


def caption_fits(text: str) -> bool:
    return len(text) <= TELEGRAM_CAPTION_LIMIT
