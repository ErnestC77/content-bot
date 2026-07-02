"""Общая логика: генерация черновика по теме и одобрение с отложенной публикацией.

Используется и планировщиком (авто), и хендлерами (ручной запуск/кнопки).
"""

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.bot.keyboards import approval_kb
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


def _draft_header(topic: str, pub_dt: datetime) -> str:
    return (
        f"📝 Черновик поста\n"
        f"🗓 Публикация: {pub_dt:%d.%m.%Y %H:%M}\n"
        f"Тема: {topic}\n\n"
    )


async def prepare_and_send_draft(bot: Bot, task_id: int, owner_id: int) -> bool:
    """Генерирует черновик из темы задачи и отправляет владельцу на согласование.

    Возвращает False, если задача не в статусе scheduled или генерация не удалась.
    Статус при неудаче остаётся scheduled — планировщик попробует снова.
    """
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None or not task.is_active or task.status != TaskStatus.SCHEDULED.value:
            return False

        default_time = await _default_time(session)
        pub_dt = content_tasks.publish_datetime(task, _tz(), default_time)
        topic = task.topic

        await approval.change_status(session, task, TaskStatus.GENERATING)
        try:
            version = await content_tasks.generate_post_version(session, task, kind="initial")
        except Exception:
            logger.exception("Не удалось сгенерировать черновик задачи #%s", task_id)
            await session.rollback()
            try:
                await bot.send_message(
                    owner_id,
                    f"⚠️ AI недоступен — черновик по теме «{topic}» пока не создан, "
                    "попробую ещё раз позже.",
                )
            except Exception:
                logger.exception("Не удалось уведомить владельца об ошибке AI")
            return False

        await approval.change_status(
            session, task, TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()
        text = content_tasks.format_draft_for_owner(version.text)

    await bot.send_message(owner_id, _draft_header(topic, pub_dt) + text, reply_markup=approval_kb(task_id))
    return True


async def resend_draft(bot: Bot, task_id: int, owner_id: int) -> None:
    """Отправляет владельцу последнюю версию черновика с кнопками согласования."""
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            return
        version = content_tasks.latest_post(task)
        if version is None:
            return
        default_time = await _default_time(session)
        pub_dt = content_tasks.publish_datetime(task, _tz(), default_time)
        topic = task.topic
        text = content_tasks.format_draft_for_owner(version.text)
    await bot.send_message(owner_id, _draft_header(topic, pub_dt) + text, reply_markup=approval_kb(task_id))


async def regenerate_and_send(
    bot: Bot, task_id: int, owner_id: int, kind: str, revision_comment: str | None = None
) -> None:
    """Делает новую версию (правки/другой вариант) и снова шлёт на согласование."""
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
            await bot.send_message(
                owner_id, "⚠️ AI недоступен — новую версию сделать не удалось, попробуйте ещё раз."
            )
            return
        await approval.change_status(
            session, task, TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()
    await resend_draft(bot, task_id, owner_id)


async def approve_task(bot: Bot, task_id: int, user_tg_id: int, user_name: str) -> str:
    """Одобряет задачу. Публикует сразу, если время уже наступило, иначе планирует.

    Возвращает текст ответа владельцу.
    """
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            return "Задача не найдена."
        if task.status == TaskStatus.PUBLISHED.value:
            return "Этот пост уже опубликован."
        if task.status != TaskStatus.WAITING_FOR_APPROVAL.value:
            return (
                f"Одобрить сейчас нельзя: статус «{task.status}». "
                "Одобрение доступно только для черновика, ожидающего согласования."
            )
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
        return f"✅ Одобрено. Время публикации уже наступило — {result.message}"
    return f"✅ Одобрено. Опубликую автоматически {pub_dt:%d.%m.%Y в %H:%M}."
