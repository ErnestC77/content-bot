"""Работа с задачами календаря и генерация версий постов через AI."""

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.client import AIError, get_provider
from app.database.models import (
    ApprovalAction,
    ContentTask,
    GeneratedPost,
    TaskStatus,
)
from app.services import approval, audit
from app.services.settings_store import (
    KEY_SYSTEM_PROMPT,
    get_ai_model,
    get_ai_provider,
    get_setting,
)

logger = logging.getLogger(__name__)


async def get_task(session: AsyncSession, task_id: int) -> ContentTask | None:
    return await session.get(ContentTask, task_id)


async def tasks_for_date(session: AsyncSession, day: date) -> list[ContentTask]:
    result = await session.scalars(
        select(ContentTask)
        .where(ContentTask.publish_date == day)
        .where(ContentTask.is_active.is_(True))
        .order_by(ContentTask.publish_time)
    )
    return list(result)


async def upcoming_tasks(session: AsyncSession, limit: int = 10) -> list[ContentTask]:
    today = date.today()
    result = await session.scalars(
        select(ContentTask)
        .where(ContentTask.publish_date >= today)
        .where(ContentTask.is_active.is_(True))
        .order_by(ContentTask.publish_date, ContentTask.publish_time)
        .limit(limit)
    )
    return list(result)


async def system_prompt(session: AsyncSession) -> str:
    return await get_setting(session, KEY_SYSTEM_PROMPT, prompts.DEFAULT_SYSTEM_PROMPT)


async def generate_questions(session: AsyncSession, task: ContentTask) -> list[str]:
    """Генерирует 1–3 уточняющих вопроса. При сбое AI — запасной набор из ТЗ."""
    try:
        provider_name = await get_ai_provider(session)
        model = await get_ai_model(session)
        provider = get_provider(provider_name, model)
        raw = await provider.generate(await system_prompt(session), prompts.build_questions_prompt(task))
        questions = [line.strip(" -•\t") for line in raw.splitlines() if line.strip()]
        questions = [q for q in questions if q][:3]
        return questions or prompts.QUESTION_FALLBACK
    except AIError:
        logger.warning("Не удалось сгенерировать вопросы через AI, использую запасные")
        return prompts.QUESTION_FALLBACK


async def _next_version_number(task: ContentTask) -> int:
    return max((p.version_number for p in task.posts), default=0) + 1


async def generate_post_version(
    session: AsyncSession,
    task: ContentTask,
    *,
    kind: str = "initial",
    revision_comment: str | None = None,
    user_id: int | None = None,
) -> GeneratedPost:
    """Генерирует новую версию поста и сохраняет её.

    kind: initial | revision | alternative.
    Бросает AIError, если провайдер недоступен — статус при этом НЕ переводится
    в waiting_for_approval (это делает вызывающий код только при успехе).
    """
    provider_name = await get_ai_provider(session)
    model = await get_ai_model(session)
    provider = get_provider(provider_name, model)

    previous = task.posts[-1].text if task.posts else ""
    if kind == "revision":
        prompt = prompts.build_revision_prompt(task, previous, revision_comment or "")
    elif kind == "alternative":
        prompt = prompts.build_alternative_prompt(task, previous)
    else:
        prompt = prompts.build_generation_prompt(task)

    text = await provider.generate(await system_prompt(session), prompt)

    version = GeneratedPost(
        task_id=task.id,
        version_number=await _next_version_number(task),
        text=text,
        generation_prompt=prompt,
        ai_provider=provider_name,
        ai_model=model,
    )
    session.add(version)
    task.posts.append(version)
    await session.flush()

    await audit.log_action(
        session,
        task.id,
        ApprovalAction.DRAFT_GENERATED.value,
        user_id=user_id,
        comment=f"kind={kind}, version={version.version_number}",
    )
    return version


def latest_post(task: ContentTask) -> GeneratedPost | None:
    return task.posts[-1] if task.posts else None


def format_draft_for_owner(text: str) -> str:
    """Черновик + блок согласования (по ТЗ)."""
    return f"{text}\n\n{prompts.APPROVAL_BLOCK}"


async def create_next_recurrence(session: AsyncSession, task: ContentTask) -> ContentTask | None:
    """Для повторяющихся задач создаёт следующий экземпляр (weekly)."""
    if task.recurrence != "weekly":
        return None
    from datetime import timedelta

    new_task = ContentTask(
        channel_id=task.channel_id,
        publish_date=task.publish_date + timedelta(days=7),
        publish_time=task.publish_time,
        rubric=task.rubric,
        topic=task.topic,
        goal=task.goal,
        description=task.description,
        status=TaskStatus.SCHEDULED.value,
        is_active=True,
        recurrence="weekly",
    )
    session.add(new_task)
    await session.flush()
    return new_task
