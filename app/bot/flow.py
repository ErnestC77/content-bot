"""Общая логика запуска сценария подготовки поста.

Используется и из хендлеров (ручной запуск), и из планировщика (по календарю),
поэтому вынесена отдельно от handlers.py.
"""

import logging

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseStorage, StorageKey

from app.bot.keyboards import answers_done_kb
from app.bot.states import TaskFlow
from app.database.models import ContentTask, TaskStatus
from app.database.session import get_session
from app.services import approval, content_tasks

logger = logging.getLogger(__name__)


def build_fsm_context(bot: Bot, storage: BaseStorage, owner_id: int) -> FSMContext:
    """FSMContext владельца для управления состоянием вне обычного хендлера."""
    key = StorageKey(bot_id=bot.id, chat_id=owner_id, user_id=owner_id)
    return FSMContext(storage=storage, key=key)


async def begin_task_flow(
    bot: Bot,
    storage: BaseStorage,
    task_id: int,
    owner_id: int,
) -> bool:
    """Отправляет владельцу задание и первый уточняющий вопрос, ставит FSM.

    Возвращает False, если задача не найдена/не активна/в неподходящем статусе.
    """
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None or not task.is_active:
            return False
        if task.status not in (TaskStatus.SCHEDULED.value, TaskStatus.DRAFT.value):
            return False

        intro = (
            "Сегодня по контент-плану:\n\n"
            f"Рубрика: {task.rubric or '—'}\n"
            f"Тема: {task.topic or '—'}\n"
            f"Цель поста: {task.goal or '—'}\n\n"
            "Ответьте, пожалуйста, на несколько вопросов, чтобы я подготовил черновик поста."
        )
        await bot.send_message(owner_id, intro)

        questions = await content_tasks.generate_questions(session, task)

        await approval.change_status(
            session,
            task,
            TaskStatus.WAITING_FOR_ANSWERS,
            action=None,
        )
        await session.commit()

    text = "Вопросы:\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    text += "\n\nОтвечайте сообщениями (можно несколькими). Когда закончите — нажмите кнопку ниже."
    await bot.send_message(owner_id, text, reply_markup=answers_done_kb())

    state = build_fsm_context(bot, storage, owner_id)
    await state.set_state(TaskFlow.waiting_for_answers)
    await state.update_data(task_id=task_id)
    return True
