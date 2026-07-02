"""Планировщик двух календарей.

- draft_generation_check: за лид-тайм до публикации готовит черновик и шлёт на согласование.
- publish_check: публикует одобренные посты, когда наступило их время.
- reminder_check: напоминает о постах, чьё время пришло, но одобрения нет.

ВАЖНО: планировщик НИКОГДА не публикует неодобренные посты (главное правило ТЗ).
"""

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.bot.flow import prepare_and_send_draft
from app.config.settings import get_settings
from app.database.session import get_session
from app.services import content_tasks, publishing
from app.services.settings_store import get_default_publish_time, get_draft_lead_days

logger = logging.getLogger(__name__)

REMINDER_TEXT = (
    "Пост готов, но ещё не одобрен.\n"
    "Публикация в канал не выполнена.\n"
    "Пожалуйста, одобрите пост или внесите правки."
)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


async def _default_time(session) -> time:
    raw = await get_default_publish_time(session)
    hh, mm = (int(x) for x in raw.split(":"))
    return time(hh, mm)


async def draft_generation_check(bot: Bot) -> None:
    """Готовит черновики для задач, у которых наступил момент подготовки."""
    owner_id = get_settings().owner_telegram_id
    async with get_session() as session:
        lead = await get_draft_lead_days(session)
        due = await content_tasks.tasks_due_for_draft(session, date.today(), lead)
        ids = [t.id for t in due]
    if not ids:
        return
    logger.info("draft_generation_check: генерирую черновики %s", ids)
    for task_id in ids:
        await prepare_and_send_draft(bot, task_id, owner_id)


async def publish_check(bot: Bot) -> None:
    """Публикует одобренные посты, у которых наступило время публикации."""
    now = datetime.now(_tz())
    async with get_session() as session:
        default_time = await _default_time(session)
        due = await content_tasks.tasks_due_for_publish(session, now, _tz(), default_time)
        ids = [t.id for t in due]
    for task_id in ids:
        async with get_session() as session:
            task = await content_tasks.get_task(session, task_id)
            if task is None:
                continue
            result = await publishing.publish_task(bot, session, task)
        try:
            await bot.send_message(
                get_settings().owner_telegram_id,
                f"Задача #{task_id}: {result.message}",
            )
        except Exception:
            logger.exception("Не удалось уведомить о публикации задачи #%s", task_id)


async def reminder_check(bot: Bot) -> None:
    """Напоминает о постах, чьё время пришло, но одобрения нет."""
    from datetime import timedelta

    owner_id = get_settings().owner_telegram_id
    now = datetime.now(_tz())
    async with get_session() as session:
        default_time = await _default_time(session)
        pending = await content_tasks.tasks_awaiting_approval(session)
        to_remind = []
        for task in pending:
            due = content_tasks.publish_datetime(task, _tz(), default_time)
            if now < due:
                continue
            if task.last_reminded_at and (now - task.last_reminded_at.astimezone(_tz())) < timedelta(hours=3):
                continue
            task.last_reminded_at = now
            to_remind.append(task.id)
        if to_remind:
            await session.commit()
    for task_id in to_remind:
        try:
            await bot.send_message(owner_id, f"Задача #{task_id}. {REMINDER_TEXT}")
        except Exception:
            logger.exception("Не удалось отправить напоминание по задаче #%s", task_id)


def build_scheduler(bot: Bot) -> AsyncIOScheduler:
    settings = get_settings()
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    hour, minute = (int(x) for x in settings.daily_check_time.split(":"))
    scheduler.add_job(
        draft_generation_check,
        CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot],
        id="draft_generation_check",
        replace_existing=True,
    )
    scheduler.add_job(
        publish_check,
        CronTrigger(minute="*/5", timezone=settings.timezone),
        args=[bot],
        id="publish_check",
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
