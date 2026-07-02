"""Сохранение медиа задачи (Telegram file_id) и подготовка к публикации."""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ContentTask, MediaType, TaskMedia

TELEGRAM_CAPTION_LIMIT = 1024


async def add_media(
    session: AsyncSession,
    task: ContentTask,
    telegram_file_id: str,
    media_type: MediaType,
    caption: str | None = None,
) -> TaskMedia:
    next_order = await session.scalar(
        select(func.coalesce(func.max(TaskMedia.sort_order), -1) + 1).where(
            TaskMedia.task_id == task.id
        )
    )
    media = TaskMedia(
        task_id=task.id,
        telegram_file_id=telegram_file_id,
        media_type=media_type.value,
        caption=caption,
        sort_order=next_order or 0,
    )
    session.add(media)
    await session.flush()
    return media


def caption_fits(text: str) -> bool:
    return len(text) <= TELEGRAM_CAPTION_LIMIT
