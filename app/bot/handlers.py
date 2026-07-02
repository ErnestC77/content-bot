"""Хендлеры Telegram-бота (aiogram 3)."""

import logging
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import keyboards as kb
from app.bot.flow import begin_task_flow
from app.bot.states import TaskFlow
from app.config.settings import get_settings
from app.database.models import (
    ApprovalAction,
    ContentTask,
    MediaType,
    TaskAnswer,
    TaskStatus,
    User,
    UserRole,
)
from app.database.session import get_session
from app.services import approval, audit, content_tasks, media, publishing
from app.services.settings_store import get_channel_id

logger = logging.getLogger(__name__)
router = Router()


async def _ensure_owner_user(session, telegram_id: int, name: str) -> User:
    from sqlalchemy import select

    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, name=name, role=UserRole.OWNER.value)
        session.add(user)
        await session.flush()
    return user


# ---------- Команды ----------


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    async with get_session() as session:
        await _ensure_owner_user(session, message.from_user.id, message.from_user.full_name)
        await session.commit()
    await message.answer(
        "Привет! Я AI-редактор вашего Telegram-канала.\n"
        "Я веду контент по календарю, готовлю черновики постов и публикую их "
        "в канал только после вашего явного одобрения.\n\n"
        "Используйте меню ниже или команды: /today, /tasks, /settings, /admin.",
        reply_markup=kb.owner_menu(),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    s = get_settings()
    base = s.webhook_url or f"http://{s.app_host}:{s.app_port}"
    await message.answer(f"Админ-панель: {base}/admin\nВход по логину и паролю из .env.")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Нет активного сценария.")
        return
    await state.clear()
    await message.answer("Текущий сценарий отменён.", reply_markup=kb.owner_menu())


@router.message(Command("tasks"))
@router.message(F.text == "📅 Календарь")
async def cmd_tasks(message: Message) -> None:
    async with get_session() as session:
        tasks = await content_tasks.upcoming_tasks(session)
    if not tasks:
        await message.answer("Ближайших задач нет.")
        return
    lines = ["Ближайшие задачи:"]
    for t in tasks:
        lines.append(
            f"#{t.id} {t.publish_date} — {t.rubric or 'без рубрики'}: "
            f"{t.topic or '—'} [{t.status}]"
        )
    await message.answer("\n".join(lines))


@router.message(Command("today"))
@router.message(F.text == "📝 Задача на сегодня")
async def cmd_today(message: Message, state: FSMContext) -> None:
    async with get_session() as session:
        tasks = await content_tasks.tasks_for_date(session, date.today())
    active = [t for t in tasks if t.status in (TaskStatus.SCHEDULED.value, TaskStatus.DRAFT.value)]
    if not active:
        await message.answer("На сегодня нет активных задач для запуска.")
        return
    task = active[0]
    await message.answer(f"Запускаю задачу #{task.id}: {task.rubric} — {task.topic}")
    await begin_task_flow(message.bot, state.storage, task.id, message.from_user.id)


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message) -> None:
    async with get_session() as session:
        channel = await get_channel_id(session)
    s = get_settings()
    base = s.webhook_url or f"http://{s.app_host}:{s.app_port}"
    await message.answer(
        "Настройки:\n"
        f"— Владелец (Telegram ID): {s.owner_telegram_id}\n"
        f"— Канал: {channel or 'не задан'}\n"
        f"— AI-модель: {s.ai_model}\n\n"
        f"Изменить настройки, системный промт и календарь можно в админ-панели: {base}/admin"
    )


@router.message(F.text == "📢 Канал")
async def cmd_channel(message: Message) -> None:
    async with get_session() as session:
        channel = await get_channel_id(session)
    if channel:
        await message.answer(f"Текущий канал для публикации: {channel}")
    else:
        await message.answer(
            "Канал не задан. Укажите его в админ-панели и добавьте бота "
            "в канал администратором с правом публикации."
        )


@router.message(F.text == "➕ Добавить пост")
async def cmd_add_post(message: Message) -> None:
    s = get_settings()
    base = s.webhook_url or f"http://{s.app_host}:{s.app_port}"
    await message.answer(f"Добавить задачу в календарь удобнее в админ-панели: {base}/admin")


# ---------- Сценарий: ответы на вопросы ----------


@router.message(TaskFlow.waiting_for_answers, F.text)
async def collect_answer_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data["task_id"]
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        user = await _ensure_owner_user(session, message.from_user.id, message.from_user.full_name)
        session.add(TaskAnswer(task_id=task_id, user_id=user.id, answer_text=message.text))
        await session.commit()
    await message.answer("Записал. Ещё что-то или нажмите «Готово с ответами».", reply_markup=kb.answers_done_kb())


@router.callback_query(F.data == kb.CB_ANSWERS_DONE)
async def answers_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    if task_id is None:
        await callback.answer()
        return
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        await approval.change_status(session, task, TaskStatus.COLLECTING_MEDIA, action=None)
        await session.commit()
    await state.set_state(TaskFlow.collecting_media)
    await callback.message.answer(
        "Пришлите фото или видео для поста (можно несколько) либо продолжите без медиа.",
        reply_markup=kb.media_kb(),
    )
    await callback.answer()


# ---------- Сценарий: приём медиа ----------


@router.message(TaskFlow.collecting_media, F.photo)
async def collect_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, message.photo[-1].file_id, MediaType.PHOTO, message.caption)
        await session.commit()
    await message.answer("Фото сохранено.", reply_markup=kb.media_kb())


@router.message(TaskFlow.collecting_media, F.video)
async def collect_video(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, message.video.file_id, MediaType.VIDEO, message.caption)
        await session.commit()
    await message.answer("Видео сохранено.", reply_markup=kb.media_kb())


@router.message(TaskFlow.collecting_media, F.document)
async def collect_document(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, message.document.file_id, MediaType.DOCUMENT, message.caption)
        await session.commit()
    await message.answer("Файл сохранён.", reply_markup=kb.media_kb())


@router.message(TaskFlow.collecting_media, F.text)
async def collect_media_text(message: Message, state: FSMContext) -> None:
    """Доп. пожелания во время приёма медиа сохраняем как ответ."""
    data = await state.get_data()
    async with get_session() as session:
        user = await _ensure_owner_user(session, message.from_user.id, message.from_user.full_name)
        session.add(TaskAnswer(task_id=data["task_id"], user_id=user.id, answer_text=message.text))
        await session.commit()
    await message.answer("Учту это пожелание.", reply_markup=kb.media_kb())


@router.callback_query(F.data.in_({kb.CB_MEDIA_DONE, kb.CB_MEDIA_SKIP}))
async def media_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    if task_id is None:
        await callback.answer()
        return
    await callback.message.answer("Генерирую черновик поста…")
    await callback.answer()
    await _generate_and_send(callback.message, state, task_id, kind="initial")


# ---------- Генерация и отправка на согласование ----------


async def _generate_and_send(message: Message, state: FSMContext, task_id: int, *, kind: str,
                             revision_comment: str | None = None) -> None:
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        # generating допустим из collecting_media / waiting_for_answers / waiting_for_approval /
        # revision_requested — проверку делает approval.change_status
        try:
            await approval.change_status(session, task, TaskStatus.GENERATING, action=None)
            await session.commit()
        except approval.InvalidTransitionError:
            await session.rollback()

        try:
            await content_tasks.generate_post_version(
                session, task, kind=kind, revision_comment=revision_comment
            )
        except Exception as exc:  # AIError и прочее
            logger.exception("Ошибка генерации поста")
            await session.rollback()
            await message.answer(
                "Не удалось сгенерировать пост: AI-сервис временно недоступен. "
                "Попробуйте ещё раз позже. Статус задачи не изменён."
            )
            return

        version = content_tasks.latest_post(task)
        await approval.change_status(
            session,
            task,
            TaskStatus.WAITING_FOR_APPROVAL,
            action=ApprovalAction.SENT_FOR_APPROVAL.value,
        )
        await session.commit()
        draft = content_tasks.format_draft_for_owner(version.text)

    await message.answer(draft, reply_markup=kb.approval_kb(task_id))
    await state.set_state(TaskFlow.waiting_for_approval)
    await state.update_data(task_id=task_id)


# ---------- Кнопки согласования ----------


def _parse_task_id(data: str) -> int:
    return int(data.split(":", 1)[1])


@router.callback_query(F.data.startswith(f"{kb.CB_REVISION}:"))
async def cb_revision(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = _parse_task_id(callback.data)
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        await approval.change_status(
            session, task, TaskStatus.REVISION_REQUESTED,
            action=ApprovalAction.REVISION_REQUESTED.value,
        )
        await session.commit()
    await state.set_state(TaskFlow.waiting_for_revision)
    await state.update_data(task_id=task_id)
    await callback.message.answer("Напишите, что изменить в посте.")
    await callback.answer()


@router.message(TaskFlow.waiting_for_revision, F.text)
async def receive_revision(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await message.answer("Генерирую новую версию с учётом правок…")
    await _generate_and_send(message, state, data["task_id"], kind="revision",
                             revision_comment=message.text)


@router.callback_query(F.data.startswith(f"{kb.CB_ALTERNATIVE}:"))
async def cb_alternative(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = _parse_task_id(callback.data)
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        await audit.log_action(
            session, task_id, ApprovalAction.ALTERNATIVE_REQUESTED.value,
            old_status=task.status,
        )
        await session.commit()
    await callback.message.answer("Готовлю другой вариант…")
    await callback.answer()
    await _generate_and_send(callback.message, state, task_id, kind="alternative")


@router.callback_query(F.data.startswith(f"{kb.CB_CANCEL}:"))
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = _parse_task_id(callback.data)
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        await approval.change_status(
            session, task, TaskStatus.CANCELLED, action=ApprovalAction.CANCELLED.value,
        )
        await session.commit()
    await state.clear()
    await callback.message.answer("Задача отменена. Пост не будет опубликован.")
    await callback.answer()


@router.callback_query(F.data.startswith(f"{kb.CB_APPROVE}:"))
async def cb_approve(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = _parse_task_id(callback.data)
    await _approve_and_publish(callback.message, state, task_id, user_tg_id=callback.from_user.id)
    await callback.answer()


# ---------- Текстовое одобрение в состоянии ожидания согласования ----------


@router.message(TaskFlow.waiting_for_approval, F.text)
async def approval_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    if task_id is None:
        return
    if approval.is_text_approval(message.text):
        await _approve_and_publish(message, state, task_id, user_tg_id=message.from_user.id)
    else:
        await message.answer(
            "Это не считается одобрением. Чтобы опубликовать, нажмите «✅ Одобряю» "
            "или напишите: одобряю / публикуй / можно публиковать / утверждаю.\n"
            "Для правок нажмите «✏️ Правки», для отмены — «❌ Отменить»."
        )


async def _approve_and_publish(message: Message, state: FSMContext, task_id: int, *, user_tg_id: int) -> None:
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            await message.answer("Задача не найдена.")
            return
        if task.status == TaskStatus.PUBLISHED.value:
            await message.answer("Этот пост уже опубликован.")
            return
        if task.status != TaskStatus.WAITING_FOR_APPROVAL.value:
            await message.answer(
                f"Одобрить сейчас нельзя: статус задачи «{task.status}». "
                "Одобрение доступно только для поста, ожидающего согласования."
            )
            return
        user = await _ensure_owner_user(session, user_tg_id, message.chat.full_name or "owner")
        await approval.change_status(
            session, task, TaskStatus.APPROVED,
            action=ApprovalAction.APPROVED.value, user_id=user.id,
        )
        await session.commit()

    await message.answer("Одобрено. Публикую в канал…")
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        result = await publishing.publish_task(message.bot, session, task)
    await message.answer(result.message)
    await state.clear()
