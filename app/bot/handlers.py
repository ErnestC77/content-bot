"""Хендлеры Telegram-бота (aiogram 3) — модель двух календарей."""

import logging
from datetime import date, time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message

from app.bot import keyboards as kb
from app.bot.flow import (
    approve_task,
    prepare_and_send_draft,
    regenerate_and_send,
)
from app.bot.states import AddMedia, AddPosts, Revision
from app.config.settings import get_settings
from app.database.models import ApprovalAction, MediaType, TaskStatus
from app.database.session import get_session
from app.services import approval, audit, content_tasks, media
from app.services.settings_store import (
    KEY_CHANNEL_ID,
    get_channel_id,
    get_default_publish_time,
    get_draft_lead_days,
    set_setting,
)

logger = logging.getLogger(__name__)
router = Router()

STATUS_LABELS = content_tasks.STATUS_LABELS


def _admin_base() -> str:
    s = get_settings()
    return (s.effective_webhook_url or f"http://{s.app_host}:{s.app_port}").rstrip("/")


def _parse_task_id(data: str) -> int:
    return int(data.split(":", 1)[1])


# ---------- Авто-определение канала (только для владельца) ----------


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
    try:
        member = await bot.get_chat_member(chat_id, get_settings().owner_telegram_id)
    except Exception:
        return False
    return member.status in ("administrator", "creator")


@router.my_chat_member()
async def on_bot_status_changed(event: ChatMemberUpdated) -> None:
    if event.chat.type != "channel":
        return
    if event.from_user is None or event.from_user.id != get_settings().owner_telegram_id:
        return
    if event.new_chat_member.status in ("administrator", "creator"):
        await _remember_channel(event.bot, event.chat.id, event.chat.title)


@router.channel_post()
async def on_channel_post(message: Message) -> None:
    async with get_session() as session:
        if await get_channel_id(session):
            return
    if not await _owner_is_admin(message.bot, message.chat.id):
        return
    await _remember_channel(message.bot, message.chat.id, message.chat.title)


# ---------- Базовые команды ----------


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with get_session() as session:
        await content_tasks.ensure_owner_user(session, message.from_user.id, message.from_user.full_name)
        await session.commit()
    await message.answer(
        "Привет! Я AI-редактор вашего Telegram-канала.\n\n"
        "Как это работает:\n"
        "1. Вы задаёте расписание списком «дата — тема» (кнопка «➕ Добавить посты»).\n"
        "2. Заранее я генерирую черновик по теме и присылаю на согласование.\n"
        "3. Вы одобряете или пишете правки — я делаю новые версии, пока не устроит.\n"
        "4. Одобренный пост публикуется в канал автоматически в назначенное время.",
        reply_markup=kb.owner_menu(),
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нет активного действия.", reply_markup=kb.owner_menu())
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=kb.owner_menu())


@router.message(Command("panel"))
@router.message(Command("admin"))
@router.message(F.text == "🗂 Панель")
async def cmd_panel(message: Message) -> None:
    base = _admin_base()
    if base.startswith("https://"):
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

        kb_open = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🗂 Открыть панель", web_app=WebAppInfo(url=f"{base}/webapp"))
        ]])
        await message.answer("Управление контентом — в панели:", reply_markup=kb_open)
    else:
        await message.answer(
            "Mini App доступен только по https. Локально используйте бота напрямую."
        )


@router.message(Command("settings"))
@router.message(F.text == "⚙️ Настройки")
async def cmd_settings(message: Message) -> None:
    async with get_session() as session:
        channel = await get_channel_id(session)
        lead = await get_draft_lead_days(session)
        def_time = await get_default_publish_time(session)
    s = get_settings()
    await message.answer(
        "Настройки:\n"
        f"— Владелец (Telegram ID): {s.owner_telegram_id}\n"
        f"— Канал: {channel or 'не задан'}\n"
        f"— Черновик готовится за {lead} дн. до публикации\n"
        f"— Время публикации по умолчанию: {def_time}\n"
        f"— AI-модель: {s.ai_model}\n\n"
        "Изменить всё это можно в панели — команда /panel."
    )


@router.message(F.text == "📢 Канал")
async def cmd_channel(message: Message) -> None:
    async with get_session() as session:
        channel = await get_channel_id(session)
    if channel:
        await message.answer(f"Текущий канал для публикации: {channel}")
    else:
        await message.answer(
            "Канал не задан. Добавьте бота администратором в закрытый канал "
            "и опубликуйте там любое сообщение — я сохраню канал автоматически."
        )


# ---------- Календарь ----------


@router.message(Command("calendar"))
@router.message(Command("tasks"))
@router.message(F.text == "📅 Календарь")
async def cmd_calendar(message: Message) -> None:
    async with get_session() as session:
        tasks = await content_tasks.upcoming_tasks(session, limit=20)
    if not tasks:
        await message.answer(
            "Расписание пусто. Добавьте посты кнопкой «➕ Добавить посты».",
            reply_markup=kb.owner_menu(),
        )
        return
    lines = ["🗓 Ближайшие посты:\n"]
    for t in tasks:
        tm = t.publish_time.strftime("%H:%M") if t.publish_time else "—"
        status = STATUS_LABELS.get(t.status, t.status)
        lines.append(f"#{t.id} · {t.publish_date} {tm}\n   {t.topic or '(без темы)'}\n   {status}")
    await message.answer("\n".join(lines))


# ---------- Добавление постов списком ----------


@router.message(Command("add"))
@router.message(F.text == "➕ Добавить посты")
async def cmd_add_posts(message: Message, state: FSMContext) -> None:
    await state.set_state(AddPosts.waiting_list)
    async with get_session() as session:
        def_time = await get_default_publish_time(session)
    await message.answer(
        "Пришлите список постов — по одному на строку в формате:\n\n"
        "<code>ГГГГ-ММ-ДД — тема</code>\n"
        "или\n"
        "<code>ГГГГ-ММ-ДД ЧЧ:ММ — тема</code>\n\n"
        "Пример:\n"
        "2026-07-05 — Как выбрать первый велосипед\n"
        "2026-07-06 10:00 — Разбор ошибок новичков\n\n"
        f"Если время не указать, возьму {def_time}. Для отмены — /cancel.",
        parse_mode="HTML",
    )


@router.message(AddPosts.waiting_list, F.text)
async def receive_posts_list(message: Message, state: FSMContext) -> None:
    async with get_session() as session:
        raw = await get_default_publish_time(session)
        hh, mm = (int(x) for x in raw.split(":"))
        created, errors = await content_tasks.bulk_create_tasks(session, message.text, time(hh, mm))
        await session.commit()
    await state.clear()
    parts = [f"✅ Создано постов: {len(created)}."]
    if created:
        parts.append("\n" + "\n".join(
            f"#{t.id} · {t.publish_date} {t.publish_time.strftime('%H:%M')} — {t.topic}"
            for t in created
        ))
    if errors:
        parts.append("\n⚠️ Не разобрал строки:\n" + "\n".join(f"• {e}" for e in errors))
    parts.append("\nЧерновики я подготовлю заранее и пришлю на согласование.")
    await message.answer("\n".join(parts), reply_markup=kb.owner_menu())


# ---------- Ручной запуск генерации ----------


@router.message(Command("today"))
@router.message(F.text == "📝 Сгенерировать сейчас")
async def cmd_generate_now(message: Message) -> None:
    async with get_session() as session:
        lead = await get_draft_lead_days(session)
        due = await content_tasks.tasks_due_for_draft(session, date.today(), lead)
        ids = [t.id for t in due]
    if not ids:
        await message.answer(
            "Нет постов, готовых к генерации черновика (по лид-тайму). "
            "Добавьте пост на ближайшие дни или уменьшите лид-тайм в настройках."
        )
        return
    await message.answer(f"Генерирую черновики: {len(ids)}…")
    for tid in ids:
        await prepare_and_send_draft(message.bot, tid, message.from_user.id)


# ---------- Добавление медиа к задаче ----------


@router.message(F.text == "📎 Добавить медиа")
async def cmd_add_media(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with get_session() as session:
        tasks = await content_tasks.upcoming_tasks(session, limit=10)
    tasks = [t for t in tasks if t.status not in (TaskStatus.PUBLISHED.value, TaskStatus.CANCELLED.value)]
    if not tasks:
        await message.answer(
            "Нет подходящих задач. Сначала добавьте пост кнопкой «➕ Добавить посты».",
            reply_markup=kb.owner_menu(),
        )
        return
    await state.set_state(AddMedia.choosing_task)
    await message.answer("К какой задаче добавить медиа?", reply_markup=kb.pick_task_kb(tasks))


@router.callback_query(AddMedia.choosing_task, F.data.startswith(f"{kb.CB_PICKTASK}:"))
async def addmedia_pick(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = _parse_task_id(callback.data)
    await state.update_data(task_id=task_id)
    await state.set_state(AddMedia.receiving)
    await callback.message.answer(
        f"Задача #{task_id}. Пришлите фото, видео или файл (можно несколько). "
        "Когда закончите — нажмите «Готово».",
        reply_markup=kb.addmedia_done_kb(),
    )
    await callback.answer()


async def _save_media(state: FSMContext, file_id: str, mtype: MediaType, caption: str | None) -> None:
    data = await state.get_data()
    async with get_session() as session:
        task = await content_tasks.get_task(session, data["task_id"])
        await media.add_media(session, task, file_id, mtype, caption)
        await session.commit()


@router.message(AddMedia.receiving, F.photo)
async def addmedia_photo(message: Message, state: FSMContext) -> None:
    await _save_media(state, message.photo[-1].file_id, MediaType.PHOTO, message.caption)
    await message.answer("Фото сохранено.", reply_markup=kb.addmedia_done_kb())


@router.message(AddMedia.receiving, F.video)
async def addmedia_video(message: Message, state: FSMContext) -> None:
    await _save_media(state, message.video.file_id, MediaType.VIDEO, message.caption)
    await message.answer("Видео сохранено.", reply_markup=kb.addmedia_done_kb())


@router.message(AddMedia.receiving, F.document)
async def addmedia_document(message: Message, state: FSMContext) -> None:
    await _save_media(state, message.document.file_id, MediaType.DOCUMENT, message.caption)
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


# ---------- Кнопки согласования ----------


@router.callback_query(F.data.startswith(f"{kb.CB_APPROVE}:"))
async def cb_approve(callback: CallbackQuery) -> None:
    task_id = _parse_task_id(callback.data)
    reply = await approve_task(
        callback.bot, task_id, callback.from_user.id, callback.from_user.full_name
    )
    await callback.message.answer(reply)
    await callback.answer()


@router.callback_query(F.data.startswith(f"{kb.CB_REVISION}:"))
async def cb_revision(callback: CallbackQuery, state: FSMContext) -> None:
    task_id = _parse_task_id(callback.data)
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None or task.status != TaskStatus.WAITING_FOR_APPROVAL.value:
            await callback.answer("Этот черновик уже не на согласовании.", show_alert=True)
            return
        await approval.change_status(
            session, task, TaskStatus.REVISION_REQUESTED,
            action=ApprovalAction.REVISION_REQUESTED.value,
        )
        await session.commit()
    await state.set_state(Revision.waiting_text)
    await state.update_data(task_id=task_id)
    await callback.message.answer("Напишите, что изменить в посте.")
    await callback.answer()


@router.message(Revision.waiting_text, F.text)
async def receive_revision(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = data["task_id"]
    await state.clear()
    await message.answer("Готовлю новую версию с учётом правок…")
    await regenerate_and_send(message.bot, task_id, message.from_user.id,
                              kind="revision", revision_comment=message.text)


@router.callback_query(F.data.startswith(f"{kb.CB_ALTERNATIVE}:"))
async def cb_alternative(callback: CallbackQuery) -> None:
    task_id = _parse_task_id(callback.data)
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None or task.status != TaskStatus.WAITING_FOR_APPROVAL.value:
            await callback.answer("Этот черновик уже не на согласовании.", show_alert=True)
            return
        await audit.log_action(
            session, task_id, ApprovalAction.ALTERNATIVE_REQUESTED.value, old_status=task.status
        )
        await session.commit()
    await callback.message.answer("Готовлю другой вариант…")
    await callback.answer()
    await regenerate_and_send(callback.bot, task_id, callback.from_user.id, kind="alternative")


@router.callback_query(F.data.startswith(f"{kb.CB_CANCEL}:"))
async def cb_cancel(callback: CallbackQuery) -> None:
    task_id = _parse_task_id(callback.data)
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            await callback.answer()
            return
        await approval.change_status(
            session, task, TaskStatus.CANCELLED, action=ApprovalAction.CANCELLED.value
        )
        await session.commit()
    await callback.message.answer(f"Задача #{task_id} отменена. Пост не будет опубликован.")
    await callback.answer()


# ---------- Текстовое одобрение (без активного состояния) ----------


@router.message(F.text)
async def text_fallback(message: Message, state: FSMContext) -> None:
    if await state.get_state() is not None:
        return
    if approval.is_text_approval(message.text):
        async with get_session() as session:
            pending = await content_tasks.tasks_awaiting_approval(session)
        if not pending:
            await message.answer("Сейчас нет черновиков, ожидающих одобрения.")
            return
        if len(pending) > 1:
            await message.answer(
                "Несколько черновиков ждут решения — одобрите нужный кнопкой «✅ Одобряю» под ним."
            )
            return
        reply = await approve_task(
            message.bot, pending[0].id, message.from_user.id, message.from_user.full_name
        )
        await message.answer(reply)
    else:
        await message.answer(
            "Не понял. Используйте меню внизу или кнопки под черновиком.",
            reply_markup=kb.owner_menu(),
        )
