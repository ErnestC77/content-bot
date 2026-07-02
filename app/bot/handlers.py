"""Хендлеры Telegram-бота (aiogram 3)."""

import logging
from datetime import date

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from app.bot import keyboards as kb
from app.bot.flow import begin_task_flow
from app.bot.states import AddMedia, AddPost, TaskFlow
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
from app.services.settings_store import KEY_CHANNEL_ID, get_channel_id, set_setting

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


# ---------- Авто-определение канала ----------


async def _remember_channel(bot, chat_id: int, title: str) -> None:
    async with get_session() as session:
        await set_setting(session, KEY_CHANNEL_ID, str(chat_id))
        await session.commit()
    try:
        await bot.send_message(
            get_settings().owner_telegram_id,
            f"✅ Канал подключён: {title or chat_id} (id {chat_id}).\n"
            "Теперь я смогу публиковать в него одобренные посты.",
        )
    except Exception:
        logger.exception("Не удалось уведомить владельца о канале")


async def _owner_is_admin(bot, chat_id: int) -> bool:
    """Проверяет, что владелец бота — админ/создатель указанного чата."""
    try:
        member = await bot.get_chat_member(chat_id, get_settings().owner_telegram_id)
    except Exception:
        return False
    return member.status in ("administrator", "creator")


@router.my_chat_member()
async def on_bot_status_changed(event: ChatMemberUpdated) -> None:
    """Бота добавили/повысили в канале — сохраняем ID, но только если это сделал владелец.

    Эти апдейты идут мимо OwnerOnlyMiddleware, поэтому проверяем инициатора вручную,
    иначе кто угодно мог бы добавить бота в свой канал и перехватить настройку.
    """
    if event.chat.type != "channel":
        return
    if event.from_user is None or event.from_user.id != get_settings().owner_telegram_id:
        return
    if event.new_chat_member.status in ("administrator", "creator"):
        await _remember_channel(event.bot, event.chat.id, event.chat.title)


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    """Резервный путь: задаём канал по посту, только если он ещё не задан
    и владелец является админом этого канала (у channel_post нет from_user)."""
    async with get_session() as session:
        if await get_channel_id(session):
            return
    if not await _owner_is_admin(message.bot, message.chat.id):
        return
    await _remember_channel(message.bot, message.chat.id, message.chat.title)


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


def _admin_base() -> str:
    s = get_settings()
    return (s.effective_webhook_url or f"http://{s.app_host}:{s.app_port}").rstrip("/")


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    await message.answer(
        f"Админ-панель: {_admin_base()}/admin\nВход по логину и паролю из настроек."
    )


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
    await message.answer(
        "Настройки:\n"
        f"— Владелец (Telegram ID): {s.owner_telegram_id}\n"
        f"— Канал: {channel or 'не задан'}\n"
        f"— AI-модель: {s.ai_model}\n\n"
        f"Изменить настройки, системный промт и календарь можно в админ-панели: {_admin_base()}/admin"
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


# ---------- Сценарий: создание поста прямо в боте ----------


@router.message(Command("add"))
@router.message(F.text == "➕ Добавить пост")
async def cmd_add_post(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddPost.date)
    await message.answer(
        "Создаём новую задачу в календаре.\n\n"
        "Шаг 1/5. На какую дату? Отправьте дату в формате ГГГГ-ММ-ДД "
        "или нажмите «Сегодня».",
        reply_markup=kb.date_today_kb(),
    )


async def _addpost_ask_rubric(message: Message, state: FSMContext) -> None:
    await state.set_state(AddPost.rubric)
    await message.answer("Шаг 2/5. Выберите рубрику:", reply_markup=kb.rubrics_kb())


@router.callback_query(AddPost.date, F.data == kb.CB_DATE_TODAY)
async def addpost_date_today(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(publish_date=date.today().isoformat())
    await _addpost_ask_rubric(callback.message, state)
    await callback.answer()


@router.message(AddPost.date, F.text)
async def addpost_date_text(message: Message, state: FSMContext) -> None:
    try:
        d = date.fromisoformat(message.text.strip())
    except ValueError:
        await message.answer("Не понял дату. Формат: ГГГГ-ММ-ДД, например 2026-07-05.",
                             reply_markup=kb.date_today_kb())
        return
    await state.update_data(publish_date=d.isoformat())
    await _addpost_ask_rubric(message, state)


@router.callback_query(AddPost.rubric, F.data.startswith(f"{kb.CB_RUBRIC}:"))
async def addpost_rubric(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    rubric = kb.RUBRICS[idx] if 0 <= idx < len(kb.RUBRICS) else ""
    await state.update_data(rubric=rubric)
    await state.set_state(AddPost.topic)
    await callback.message.answer(f"Рубрика: {rubric}\n\nШаг 3/5. Напишите тему поста.")
    await callback.answer()


@router.message(AddPost.topic, F.text)
async def addpost_topic(message: Message, state: FSMContext) -> None:
    await state.update_data(topic=message.text.strip())
    await state.set_state(AddPost.goal)
    await message.answer("Шаг 4/5. Какая цель поста? (что он должен дать читателю)")


@router.message(AddPost.goal, F.text)
async def addpost_goal(message: Message, state: FSMContext) -> None:
    await state.update_data(goal=message.text.strip())
    await state.set_state(AddPost.description)
    await message.answer(
        "Шаг 5/5. Добавьте описание/детали задачи или нажмите «Пропустить».",
        reply_markup=kb.skip_kb(),
    )


async def _addpost_finish(message: Message, state: FSMContext, description: str) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = ContentTask(
            publish_date=date.fromisoformat(data["publish_date"]),
            rubric=data.get("rubric", ""),
            topic=data.get("topic", ""),
            goal=data.get("goal", ""),
            description=description,
            status=TaskStatus.SCHEDULED.value,
        )
        session.add(task)
        await session.commit()
        task_id = task.id
    await state.clear()
    await message.answer(
        f"✅ Задача #{task_id} создана на {data['publish_date']}.\n"
        f"Рубрика: {data.get('rubric') or '—'} · Тема: {data.get('topic') or '—'}\n\n"
        "Запустить подготовку поста можно кнопкой «📝 Задача на сегодня» (если дата сегодня) "
        "или из админки. Прикрепить медиа — кнопкой «📎 Добавить медиа».",
        reply_markup=kb.owner_menu(),
    )


@router.callback_query(AddPost.description, F.data == kb.CB_SKIP)
async def addpost_desc_skip(callback: CallbackQuery, state: FSMContext) -> None:
    await _addpost_finish(callback.message, state, "")
    await callback.answer()


@router.message(AddPost.description, F.text)
async def addpost_desc_text(message: Message, state: FSMContext) -> None:
    await _addpost_finish(message, state, message.text.strip())


# ---------- Сценарий: добавить медиа к задаче ----------


@router.message(F.text == "📎 Добавить медиа")
async def cmd_add_media(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with get_session() as session:
        tasks = await content_tasks.upcoming_tasks(session, limit=10)
    # можно прикреплять медиа к задачам, которые ещё не опубликованы/не отменены
    tasks = [t for t in tasks if t.status not in (TaskStatus.PUBLISHED.value, TaskStatus.CANCELLED.value)]
    if not tasks:
        await message.answer(
            "Нет подходящих задач. Сначала создайте пост кнопкой «➕ Добавить пост».",
            reply_markup=kb.owner_menu(),
        )
        return
    await state.set_state(AddMedia.choosing_task)
    await message.answer("К какой задаче добавить медиа?", reply_markup=kb.pick_task_kb(tasks))


@router.callback_query(AddMedia.choosing_task, F.data.startswith(f"{kb.CB_PICKTASK}:"))
async def addmedia_pick(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    await state.update_data(task_id=task_id)
    await state.set_state(AddMedia.receiving)
    await callback.message.answer(
        f"Задача #{task_id}. Пришлите фото, видео или файл (можно несколько). "
        "Когда закончите — нажмите «Готово».",
        reply_markup=kb.addmedia_done_kb(),
    )
    await callback.answer()


@router.message(AddMedia.receiving, F.photo)
async def addmedia_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, message.photo[-1].file_id, MediaType.PHOTO, message.caption)
        await session.commit()
    await message.answer("Фото сохранено.", reply_markup=kb.addmedia_done_kb())


@router.message(AddMedia.receiving, F.video)
async def addmedia_video(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, message.video.file_id, MediaType.VIDEO, message.caption)
        await session.commit()
    await message.answer("Видео сохранено.", reply_markup=kb.addmedia_done_kb())


@router.message(AddMedia.receiving, F.document)
async def addmedia_document(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, message.document.file_id, MediaType.DOCUMENT, message.caption)
        await session.commit()
    await message.answer("Файл сохранён.", reply_markup=kb.addmedia_done_kb())


@router.callback_query(AddMedia.receiving, F.data == kb.CB_ADDMEDIA_DONE)
async def addmedia_done(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data.get("task_id")
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        count = len(task.media) if task else 0
    await state.clear()
    await callback.message.answer(
        f"Готово. К задаче #{task_id} прикреплено медиа: {count}.",
        reply_markup=kb.owner_menu(),
    )
    await callback.answer()


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
