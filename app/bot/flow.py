"""Генерация черновика и одобрение с отложенной публикацией.

Весь рабочий процесс живёт в Mini App. Бот в чат шлёт только короткие
уведомления со ссылкой на панель — без текста черновика и кнопок согласования.
"""

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from app.config.settings import get_settings
from app.database.models import ApprovalAction, TaskStatus
from app.database.session import get_session
from app.services import approval, content_tasks, publishing
from app.services.settings_store import get_default_publish_time

logger = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


async def _default_time(session) -> time:
    raw = await get_default_publish_time(session)
    hh, mm = (int(x) for x in raw.split(":"))
    return time(hh, mm)


def _panel_kb() -> InlineKeyboardMarkup | None:
    base = get_settings().effective_webhook_url.rstrip("/")
    if not base.startswith("https://"):
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗂 Открыть панель", web_app=WebAppInfo(url=f"{base}/webapp"))
    ]])


async def _notify(bot: Bot, owner_id: int, text: str) -> None:
    try:
        await bot.send_message(owner_id, text, reply_markup=_panel_kb())
    except Exception:
        logger.exception("Не удалось отправить уведомление владельцу")


async def prepare_and_send_draft(bot: Bot, task_id: int, owner_id: int) -> bool:
    """Генерирует черновик из темы и уведомляет владельца (без текста в чат)."""
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None or not task.is_active or task.status != TaskStatus.SCHEDULED.value:
            return False
        default_time = await _default_time(session)
        pub_dt = content_tasks.publish_datetime(task, _tz(), default_time)
        topic = task.topic

        await approval.change_status(session, task, TaskStatus.GENERATING)
        try:
            await content_tasks.generate_post_version(session, task, kind="initial")
        except Exception:
            logger.exception("Не удалось сгенерировать черновик задачи #%s", task_id)
            await session.rollback()
            await _notify(bot, owner_id, f"⚠️ AI недоступен — черновик по теме «{topic}» пока не создан.")
            return False
        await approval.change_status(
            session, task, TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()

    await _notify(bot, owner_id, f"🔔 Готов черновик к посту на {pub_dt:%d.%m %H:%M} — «{topic}». Откройте панель для согласования.")
    return True


async def regenerate_and_send(
    bot: Bot, task_id: int, owner_id: int, kind: str, revision_comment: str | None = None
) -> None:
    """Делает новую версию (правки/другой вариант) и уведомляет владельца."""
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            return
        await approval.change_status(session, task, TaskStatus.GENERATING)
        try:
            await content_tasks.generate_post_version(
                session, task, kind=kind, revision_comment=revision_comment
            )
        except Exception:
            logger.exception("Не удалось сгенерировать новую версию задачи #%s", task_id)
            await session.rollback()
            await _notify(bot, owner_id, "⚠️ AI недоступен — новую версию сделать не удалось.")
            return
        await approval.change_status(
            session, task, TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()
    await _notify(bot, owner_id, "🔁 Новая версия готова — откройте панель.")


async def approve_task(bot: Bot, task_id: int, user_tg_id: int, user_name: str) -> str:
    """Одобряет задачу. Публикует сразу, если время наступило, иначе планирует."""
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            return "Задача не найдена."
        if task.status == TaskStatus.PUBLISHED.value:
            return "Этот пост уже опубликован."
        if task.status != TaskStatus.WAITING_FOR_APPROVAL.value:
            return f"Одобрить нельзя: статус «{task.status}»."
        user = await content_tasks.ensure_owner_user(session, user_tg_id, user_name)
        await approval.change_status(
            session, task, TaskStatus.APPROVED,
            action=ApprovalAction.APPROVED.value, user_id=user.id,
        )
        await session.commit()
        default_time = await _default_time(session)
        pub_dt = content_tasks.publish_datetime(task, _tz(), default_time)

    now = datetime.now(_tz())
    if pub_dt <= now:
        async with get_session() as session:
            task = await content_tasks.get_task(session, task_id)
            result = await publishing.publish_task(bot, session, task)
        return f"✅ Одобрено. Время уже наступило — {result.message}"
    return f"✅ Одобрено. Опубликую автоматически {pub_dt:%d.%m в %H:%M}."
