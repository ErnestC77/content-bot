"""Планировщик: ежедневная проверка календаря и напоминания о неодобренных постах.

ВАЖНО: планировщик НИКОГДА не публикует посты. Наступление времени публикации
без одобрения приводит только к напоминанию владельцу (по ТЗ).
"""

import logging
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot
from aiogram.fsm.storage.base import BaseStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.bot.flow import begin_task_flow
from app.config.settings import get_settings
from app.database.models import ContentTask, TaskStatus
from app.database.session import get_session
from app.services import content_tasks

logger = logging.getLogger(__name__)

REMINDER_TEXT = (
    "Пост готов, но ещё не одобрен.\n"
    "Публикация в канал не выполнена.\n"
    "Пожалуйста, одобрите пост или внесите правки."
)


async def daily_check(bot: Bot, storage: BaseStorage) -> None:
    """Находит активные задачи на сегодня и запускает сценарий вопросов."""
    settings = get_settings()
    owner_id = settings.owner_telegram_id
    async with get_session() as session:
        tasks = await content_tasks.tasks_for_date(session, date.today())
    startable = [
        t for t in tasks if t.status in (TaskStatus.SCHEDULED.value, TaskStatus.DRAFT.value)
    ]
    if not startable:
        logger.info("daily_check: активных задач на сегодня нет")
        return
    # Запускаем первую; остальные владелец запустит вручную через /today
    task = startable[0]
    logger.info("daily_check: запускаю задачу #%s", task.id)
    await begin_task_flow(bot, storage, task.id, owner_id)


async def reminder_check(bot: Bot) -> None:
    """Напоминает о постах, чьё время публикации прошло, но одобрения нет."""
    from sqlalchemy import select

    settings = get_settings()
    owner_id = settings.owner_telegram_id
    now = datetime.now(timezone.utc)
    today = date.today()

    async with get_session() as session:
        tasks = list(
            await session.scalars(
                select(ContentTask)
                .where(ContentTask.is_active.is_(True))
                .where(ContentTask.status == TaskStatus.WAITING_FOR_APPROVAL.value)
                .where(ContentTask.publish_date <= today)
            )
        )
        for task in tasks:
            publish_time = task.publish_time or time(23, 59)
            due = datetime.combine(task.publish_date, publish_time, tzinfo=timezone.utc)
            if now < due:
                continue
            # Не спамим: напоминаем не чаще раза в 3 часа
            if task.last_reminded_at and now - task.last_reminded_at < timedelta(hours=3):
                continue
            task.last_reminded_at = now
            await session.commit()
            try:
                await bot.send_message(owner_id, f"Задача #{task.id}. {REMINDER_TEXT}")
            except Exception:
                logger.exception("Не удалось отправить напоминание по задаче #%s", task.id)


def build_scheduler(bot: Bot, storage: BaseStorage) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    hour, minute = (int(x) for x in settings.daily_check_time.split(":"))
    scheduler.add_job(
        daily_check,
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, storage],
        id="daily_check",
        replace_existing=True,
    )
    scheduler.add_job(
        reminder_check,
        CronTrigger(minute="*/30", timezone=settings.timezone),
        args=[bot],
        id="reminder_check",
        replace_existing=True,
    )
    return scheduler
