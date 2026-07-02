"""Web-админка (FastAPI + Jinja2). Управление календарём, задачами и настройками."""

import logging
from datetime import date, datetime, time
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import require_admin
from app.ai import prompts
from app.database.models import ContentTask, TaskStatus
from app.database.session import get_session_dependency
from app.services import content_tasks
from app.services.settings_store import (
    KEY_AI_MODEL,
    KEY_AI_PROVIDER,
    KEY_CHANNEL_ID,
    KEY_DAILY_CHECK_TIME,
    KEY_OWNER_TELEGRAM_ID,
    KEY_SYSTEM_PROMPT,
    get_setting,
    set_setting,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

RUBRICS = [
    "Новинка", "Закулисье", "Полезный пост", "Отзыв",
    "Вопрос подписчикам", "Специальное предложение", "Личный пост владельца",
]


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    h, m = value.split(":")
    return time(int(h), int(m))


@router.get("", response_class=HTMLResponse)
async def index(request: Request, session: AsyncSession = Depends(get_session_dependency)):
    tasks = list(
        await session.scalars(
            select(ContentTask).order_by(ContentTask.publish_date.desc(), ContentTask.publish_time)
        )
    )
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "tasks": tasks, "rubrics": RUBRICS, "today": date.today()},
    )


@router.post("/tasks")
async def create_task(
    publish_date: str = Form(...),
    publish_time: str = Form(""),
    rubric: str = Form(""),
    topic: str = Form(""),
    goal: str = Form(""),
    description: str = Form(""),
    recurrence: str = Form("none"),
    session: AsyncSession = Depends(get_session_dependency),
):
    task = ContentTask(
        publish_date=date.fromisoformat(publish_date),
        publish_time=_parse_time(publish_time),
        rubric=rubric,
        topic=topic,
        goal=goal,
        description=description,
        recurrence=recurrence,
        status=TaskStatus.SCHEDULED.value,
    )
    session.add(task)
    await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(
    task_id: int, request: Request, session: AsyncSession = Depends(get_session_dependency)
):
    task = await content_tasks.get_task(session, task_id)
    if task is None:
        return RedirectResponse("/admin", status_code=303)
    return templates.TemplateResponse(
        "task.html", {"request": request, "task": task, "rubrics": RUBRICS}
    )


@router.post("/tasks/{task_id}/edit")
async def edit_task(
    task_id: int,
    publish_date: str = Form(...),
    publish_time: str = Form(""),
    rubric: str = Form(""),
    topic: str = Form(""),
    goal: str = Form(""),
    description: str = Form(""),
    recurrence: str = Form("none"),
    session: AsyncSession = Depends(get_session_dependency),
):
    task = await content_tasks.get_task(session, task_id)
    if task:
        task.publish_date = date.fromisoformat(publish_date)
        task.publish_time = _parse_time(publish_time)
        task.rubric = rubric
        task.topic = topic
        task.goal = goal
        task.description = description
        task.recurrence = recurrence
        await session.commit()
    return RedirectResponse(f"/admin/tasks/{task_id}", status_code=303)


@router.post("/tasks/{task_id}/toggle")
async def toggle_task(task_id: int, session: AsyncSession = Depends(get_session_dependency)):
    task = await content_tasks.get_task(session, task_id)
    if task:
        task.is_active = not task.is_active
        await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/tasks/{task_id}/delete")
async def delete_task(task_id: int, session: AsyncSession = Depends(get_session_dependency)):
    task = await content_tasks.get_task(session, task_id)
    if task:
        await session.delete(task)
        await session.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/tasks/{task_id}/run")
async def run_task(request: Request, task_id: int):
    """Ручной запуск сценария задачи на сегодня (через бота)."""
    from app.config.settings import get_settings

    bot = request.app.state.bot
    storage = request.app.state.storage
    from app.bot.flow import begin_task_flow

    await begin_task_flow(bot, storage, task_id, get_settings().owner_telegram_id)
    return RedirectResponse(f"/admin/tasks/{task_id}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, session: AsyncSession = Depends(get_session_dependency)):
    from app.config.settings import get_settings

    s = get_settings()
    data = {
        "owner_telegram_id": await get_setting(session, KEY_OWNER_TELEGRAM_ID, str(s.owner_telegram_id)),
        "channel_id": await get_setting(session, KEY_CHANNEL_ID, s.default_channel_id),
        "daily_check_time": await get_setting(session, KEY_DAILY_CHECK_TIME, s.daily_check_time),
        "ai_provider": await get_setting(session, KEY_AI_PROVIDER, s.ai_provider),
        "ai_model": await get_setting(session, KEY_AI_MODEL, s.ai_model),
        "system_prompt": await get_setting(session, KEY_SYSTEM_PROMPT, prompts.DEFAULT_SYSTEM_PROMPT),
    }
    return templates.TemplateResponse("settings.html", {"request": request, "data": data})


@router.post("/settings")
async def save_settings(
    owner_telegram_id: str = Form(""),
    channel_id: str = Form(""),
    daily_check_time: str = Form(""),
    ai_provider: str = Form(""),
    ai_model: str = Form(""),
    system_prompt: str = Form(""),
    session: AsyncSession = Depends(get_session_dependency),
):
    await set_setting(session, KEY_OWNER_TELEGRAM_ID, owner_telegram_id)
    await set_setting(session, KEY_CHANNEL_ID, channel_id)
    await set_setting(session, KEY_DAILY_CHECK_TIME, daily_check_time)
    await set_setting(session, KEY_AI_PROVIDER, ai_provider)
    await set_setting(session, KEY_AI_MODEL, ai_model)
    await set_setting(session, KEY_SYSTEM_PROMPT, system_prompt)
    await session.commit()
    return RedirectResponse("/admin/settings", status_code=303)
