"""Telegram Mini App: страница + JSON API. Авторизация — по initData (require_owner)."""

import logging
from datetime import date, time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.models import ContentTask, TaskMedia, TaskStatus
from app.database.session import get_session, get_session_dependency
from app.services import approval, content_tasks
from app.services import media as media_service
from app.services.content_tasks import STATUS_LABELS
from app.services.settings_store import (
    KEY_AI_MODEL,
    KEY_AI_PROVIDER,
    KEY_CHANNEL_ID,
    KEY_DAILY_CHECK_TIME,
    KEY_DEFAULT_PUBLISH_TIME,
    KEY_DRAFT_LEAD_DAYS,
    KEY_SYSTEM_PROMPT,
    get_setting,
    set_setting,
)
from app.webapp.auth import require_owner
from app.ai import prompts

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

RUBRICS = [
    "Новинка", "Закулисье", "Полезный пост", "Отзыв",
    "Вопрос подписчикам", "Специальное предложение", "Личный пост владельца",
]

DRAFT_STATUSES = {
    TaskStatus.SCHEDULED.value, TaskStatus.GENERATING.value,
    TaskStatus.WAITING_FOR_APPROVAL.value, TaskStatus.REVISION_REQUESTED.value,
}
READY_STATUSES = {
    TaskStatus.APPROVED.value, TaskStatus.PUBLISHING.value,
    TaskStatus.PUBLISHED.value, TaskStatus.PUBLISH_FAILED.value,
}

page_router = APIRouter()
api = APIRouter(prefix="/api/webapp", dependencies=[Depends(require_owner)])


@page_router.get("/webapp", response_class=HTMLResponse)
async def webapp_page(request: Request):
    return templates.TemplateResponse(request, "app.html", {})


def _task_dict(task: ContentTask, full: bool = False) -> dict:
    latest = content_tasks.latest_post(task)
    data = {
        "id": task.id,
        "publish_date": task.publish_date.isoformat(),
        "publish_time": task.publish_time.strftime("%H:%M") if task.publish_time else "",
        "topic": task.topic or "",
        "rubric": task.rubric or "",
        "goal": task.goal or "",
        "description": task.description or "",
        "status": task.status,
        "status_label": STATUS_LABELS.get(task.status, task.status),
        "is_active": task.is_active,
        "media_count": len(task.media),
        "media": [{"id": m.id, "type": m.media_type} for m in task.media],
        "text": task.final_text or (latest.text if latest else ""),
        "preview": (task.final_text or (latest.text if latest else ""))[:200],
        "can_approve": task.status == TaskStatus.WAITING_FOR_APPROVAL.value,
        "can_generate": task.status == TaskStatus.SCHEDULED.value,
        "can_publish": task.status in (TaskStatus.APPROVED.value, TaskStatus.PUBLISH_FAILED.value),
    }
    if full:
        data["text"] = latest.text if latest else ""
        data["versions"] = [
            {"n": p.version_number, "text": p.text, "model": p.ai_model} for p in task.posts
        ]
        data["logs"] = [
            {"action": l.action, "old": l.old_status, "new": l.new_status,
             "comment": l.comment, "at": l.created_at.strftime("%Y-%m-%d %H:%M")}
            for l in task.logs
        ]
    return data


@api.get("/tasks")
async def list_tasks(session: AsyncSession = Depends(get_session_dependency)):
    tasks = list(await session.scalars(
        select(ContentTask).order_by(ContentTask.publish_date, ContentTask.publish_time)
    ))
    return {
        "drafts": [_task_dict(t) for t in tasks if t.status in DRAFT_STATUSES],
        "ready": [_task_dict(t) for t in tasks if t.status in READY_STATUSES],
        "cancelled": [_task_dict(t) for t in tasks if t.status == TaskStatus.CANCELLED.value],
    }


@api.get("/tasks/{task_id}")
async def task_detail(task_id: int, session: AsyncSession = Depends(get_session_dependency)):
    task = await content_tasks.get_task(session, task_id)
    if task is None:
        return {"error": "not found"}
    return _task_dict(task, full=True)


class BulkBody(BaseModel):
    text: str


@api.post("/tasks/bulk")
async def bulk_add(body: BulkBody, session: AsyncSession = Depends(get_session_dependency)):
    raw = await get_setting(session, KEY_DEFAULT_PUBLISH_TIME, get_settings().default_publish_time)
    hh, mm = (int(x) for x in raw.split(":"))
    created, errors = await content_tasks.bulk_create_tasks(session, body.text, time(hh, mm))
    await session.commit()
    return {"created": len(created), "errors": errors}


class EditBody(BaseModel):
    publish_date: str
    publish_time: str = ""
    topic: str = ""
    rubric: str = ""
    goal: str = ""
    description: str = ""


@api.post("/tasks/{task_id}/edit")
async def edit_task(task_id: int, body: EditBody, session: AsyncSession = Depends(get_session_dependency)):
    task = await content_tasks.get_task(session, task_id)
    if task is None:
        return {"error": "not found"}
    task.publish_date = date.fromisoformat(body.publish_date)
    if body.publish_time:
        hh, mm = (int(x) for x in body.publish_time.split(":"))
        task.publish_time = time(hh, mm)
    task.topic = body.topic
    task.rubric = body.rubric
    task.goal = body.goal
    task.description = body.description
    await session.commit()
    return {"ok": True}


@api.post("/tasks/{task_id}/delete")
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session_dependency)):
    task = await content_tasks.get_task(session, task_id)
    if task:
        await session.delete(task)
        await session.commit()
    return {"ok": True}


@api.post("/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, session: AsyncSession = Depends(get_session_dependency)):
    task = await content_tasks.get_task(session, task_id)
    if task:
        task.is_active = not task.is_active
        await session.commit()
    return {"ok": True, "is_active": task.is_active if task else None}


@api.post("/tasks/{task_id}/generate")
async def generate_task(task_id: int, request: Request, owner: int = Depends(require_owner)):
    from app.bot.flow import prepare_and_send_draft
    ok = await prepare_and_send_draft(request.app.state.bot, task_id, owner)
    return {"ok": ok}


@api.post("/tasks/{task_id}/approve")
async def approve(task_id: int, request: Request, owner: int = Depends(require_owner)):
    from app.bot.flow import approve_task
    msg = await approve_task(request.app.state.bot, task_id, owner, "owner")
    return {"ok": True, "message": msg}


class ReviseBody(BaseModel):
    comment: str


@api.post("/tasks/{task_id}/revise")
async def revise(task_id: int, body: ReviseBody, request: Request, owner: int = Depends(require_owner)):
    from app.bot.flow import regenerate_and_send
    await regenerate_and_send(request.app.state.bot, task_id, owner,
                              kind="revision", revision_comment=body.comment)
    return {"ok": True}


@api.post("/tasks/{task_id}/alternative")
async def alternative(task_id: int, request: Request, owner: int = Depends(require_owner)):
    from app.bot.flow import regenerate_and_send
    await regenerate_and_send(request.app.state.bot, task_id, owner, kind="alternative")
    return {"ok": True}


@api.post("/tasks/{task_id}/publish_now")
async def publish_now(task_id: int, request: Request, owner: int = Depends(require_owner)):
    """Публикует одобренный пост в канал немедленно (не дожидаясь расписания)."""
    from app.database.models import ApprovalAction
    from app.services import publishing

    bot = request.app.state.bot
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            return {"error": "not found"}
        # publish_failed → сначала вернуть в approved, затем публиковать
        if task.status == TaskStatus.PUBLISH_FAILED.value:
            try:
                await approval.change_status(
                    session, task, TaskStatus.APPROVED, action=ApprovalAction.APPROVED.value
                )
                await session.commit()
            except approval.InvalidTransitionError:
                await session.rollback()
        if task.status != TaskStatus.APPROVED.value:
            return {"ok": False, "message": f"Публиковать нельзя: статус «{task.status}»."}
        result = await publishing.publish_task(bot, session, task)
    return {"ok": result.ok, "message": result.message}


@api.post("/tasks/{task_id}/cancel")
async def cancel(task_id: int, session: AsyncSession = Depends(get_session_dependency)):
    from app.database.models import ApprovalAction
    task = await content_tasks.get_task(session, task_id)
    if task is None:
        return {"error": "not found"}
    try:
        await approval.change_status(
            session, task, TaskStatus.CANCELLED, action=ApprovalAction.CANCELLED.value
        )
        await session.commit()
    except approval.InvalidTransitionError:
        await session.rollback()
        return {"error": "cannot cancel"}
    return {"ok": True}


MAX_UPLOAD = 20 * 1024 * 1024  # 20 МБ


@api.post("/tasks/{task_id}/media")
async def upload_media(task_id: int, file: UploadFile = File(...)):
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        return {"ok": False, "message": "Файл больше 20 МБ."}
    async with get_session() as session:
        task = await content_tasks.get_task(session, task_id)
        if task is None:
            return {"error": "not found"}
        mt = media_service.media_type_from_mime(file.content_type or "")
        await media_service.add_media_bytes(
            session, task, content, file.content_type or "application/octet-stream", mt
        )
        await session.commit()
        count = len(task.media)
    return {"ok": True, "media_count": count}


@api.post("/tasks/{task_id}/media/{media_id}/delete")
async def delete_media(task_id: int, media_id: int, session: AsyncSession = Depends(get_session_dependency)):
    m = await session.get(TaskMedia, media_id)
    if m and m.task_id == task_id:
        await session.delete(m)
        await session.commit()
    return {"ok": True}


@api.get("/settings")
async def get_settings_api(session: AsyncSession = Depends(get_session_dependency)):
    s = get_settings()
    return {
        "channel_id": await get_setting(session, KEY_CHANNEL_ID, s.default_channel_id),
        "daily_check_time": await get_setting(session, KEY_DAILY_CHECK_TIME, s.daily_check_time),
        "draft_lead_days": await get_setting(session, KEY_DRAFT_LEAD_DAYS, str(s.draft_lead_days)),
        "default_publish_time": await get_setting(session, KEY_DEFAULT_PUBLISH_TIME, s.default_publish_time),
        "ai_provider": await get_setting(session, KEY_AI_PROVIDER, s.ai_provider),
        "ai_model": await get_setting(session, KEY_AI_MODEL, s.ai_model),
        "system_prompt": await get_setting(session, KEY_SYSTEM_PROMPT, prompts.DEFAULT_SYSTEM_PROMPT),
        "rubrics": RUBRICS,
    }


class SettingsBody(BaseModel):
    channel_id: str = ""
    daily_check_time: str = ""
    draft_lead_days: str = "1"
    default_publish_time: str = "10:00"
    ai_provider: str = ""
    ai_model: str = ""
    system_prompt: str = ""


@api.post("/settings")
async def save_settings_api(body: SettingsBody, session: AsyncSession = Depends(get_session_dependency)):
    await set_setting(session, KEY_CHANNEL_ID, body.channel_id)
    await set_setting(session, KEY_DAILY_CHECK_TIME, body.daily_check_time)
    await set_setting(session, KEY_DRAFT_LEAD_DAYS, body.draft_lead_days)
    await set_setting(session, KEY_DEFAULT_PUBLISH_TIME, body.default_publish_time)
    await set_setting(session, KEY_AI_PROVIDER, body.ai_provider)
    await set_setting(session, KEY_AI_MODEL, body.ai_model)
    await set_setting(session, KEY_SYSTEM_PROMPT, body.system_prompt)
    await session.commit()
    return {"ok": True}


def include_webapp(app) -> None:
    app.include_router(page_router)
    app.include_router(api)
