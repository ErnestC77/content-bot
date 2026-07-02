"""Генерация черновика и одобрение с отложенной публикацией.

Весь рабочий процесс живёт в Mini App. Бот в чат шлёт только короткие
уведомления со ссылкой на панель — без текста черновика и кнопок согласования.
"""

import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from aiogram import Bot

from app.config.settings import get_settings
from app.database.models import ApprovalAction, TaskStatus
from app.database.session import get_session
from app.services import approval, content_tasks, publishing
from app.services.notify import broadcast
from app.services.settings_store import get_default_publish_time

logger = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


async def _default_time(session) -> time:
    raw = await get_default_publish_time(session)
    hh, mm = (int(x) for x in raw.split(":"))
    return time(hh, mm)


async def ask_questions(bot: Bot, task_id: int, owner_id: int) -> bool:
    """Готовит 1–3 наводящих вопроса по теме и переводит задачу в ожидание ответов.

    Вопросы помогают AI написать более точный черновик — владелец отвечает
    в Mini App, после чего вызывается generate_from_answers().
    """
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None or not task.is_active or task.status != TaskStatus.SCHEDULED.value:
            return False
        topic = task.topic

        questions = await content_tasks.generate_questions(session, task)
        task.pending_questions = "\n".join(questions)
        await approval.change_status(session, task, TaskStatus.WAITING_FOR_ANSWERS)
        await session.commit()

    await broadcast(bot, f"❓ Есть вопросы по посту «{topic}» — ответьте в панели, чтобы я подготовил черновик.")
    return True


async def generate_from_answers(bot: Bot, task_id: int, owner_id: int) -> bool:
    """Генерирует черновик с учётом ответов владельца (или без них, если пропущено)."""
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
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
            await broadcast(bot, f"⚠️ AI недоступен — черновик по теме «{topic}» пока не создан.")
            return False
        task.pending_questions = None
        await approval.change_status(
            session, task, TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()

    await broadcast(bot, f"🔔 Готов черновик к посту на {pub_dt:%d.%m %H:%M} — «{topic}». Откройте панель для согласования.")
    return True


async def prepare_and_send_draft(bot: Bot, task_id: int, owner_id: int) -> bool:
    """Обратная совместимость: сразу спрашивает наводящие вопросы (не генерирует)."""
    return await ask_questions(bot, task_id, owner_id)


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
            await broadcast(bot, "⚠️ AI недоступен — новую версию сделать не удалось.")
            return
        await approval.change_status(
            session, task, TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()
    await broadcast(bot, "🔁 Новая версия готова — откройте панель.")


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
