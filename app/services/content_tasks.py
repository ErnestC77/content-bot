"""Работа с задачами календаря и генерация версий постов через AI."""

import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai import prompts
from app.ai.client import AIError, get_provider
from app.database.models import (
    ApprovalAction,
    ContentTask,
    GeneratedPost,
    TaskStatus,
    User,
    UserRole,
)
from app.services import approval, audit
from app.services.settings_store import (
    KEY_SYSTEM_PROMPT,
    get_ai_model,
    get_ai_provider,
    get_setting,
)

logger = logging.getLogger(__name__)

# Человекочитаемые подписи статусов (для бота и админки).
STATUS_LABELS = {
    TaskStatus.SCHEDULED.value: "⏳ ждёт генерации",
    TaskStatus.GENERATING.value: "⚙️ генерируется",
    TaskStatus.WAITING_FOR_APPROVAL.value: "🕓 на согласовании",
    TaskStatus.REVISION_REQUESTED.value: "✏️ правки",
    TaskStatus.APPROVED.value: "✅ одобрен, ждёт публикации",
    TaskStatus.PUBLISHING.value: "📤 публикуется",
    TaskStatus.PUBLISHED.value: "📢 опубликован",
    TaskStatus.PUBLISH_FAILED.value: "⚠️ ошибка публикации",
    TaskStatus.CANCELLED.value: "❌ отменён",
}


async def get_task(session: AsyncSession, task_id: int) -> ContentTask | None:
    return await session.get(ContentTask, task_id)


async def ensure_owner_user(session: AsyncSession, telegram_id: int, name: str) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, name=name, role=UserRole.OWNER.value)
        session.add(user)
        await session.flush()
    return user


# ---------- Разбор и массовое создание расписания ----------

# Строка вида «2026-07-05 — Тема» или «2026-07-05 10:00 — Тема».
_LINE_RE = re.compile(
    r"^\s*(\d{4}-\d{2}-\d{2})\s*(\d{1,2}:\d{2})?\s*[—–\-|:•.]*\s*(.+?)\s*$"
)


def parse_schedule_line(line: str, default_time: time) -> tuple[date, time, str] | None:
    """Возвращает (дата, время, тема) или None, если строку не разобрать."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    date_str, time_str, topic = m.group(1), m.group(2), m.group(3)
    if not topic.strip():
        return None
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return None
    t = default_time
    if time_str:
        try:
            hh, mm = (int(x) for x in time_str.split(":"))
            t = time(hh, mm)
        except (ValueError, TypeError):
            return None
    return d, t, topic.strip()


async def bulk_create_tasks(
    session: AsyncSession, text: str, default_time: time, lead_days: int, draft_time: time
) -> tuple[list[ContentTask], list[str]]:
    """Создаёт задачи по строкам «дата [время] — тема».

    Дата/время подготовки черновика вычисляются как публикация минус lead_days
    (в draft_time), но дальше их можно редактировать независимо. Возвращает
    (созданные, ошибки).
    """
    created: list[ContentTask] = []
    errors: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parsed = parse_schedule_line(raw, default_time)
        if parsed is None:
            errors.append(raw.strip())
            continue
        d, t, topic = parsed
        task = ContentTask(
            publish_date=d,
            publish_time=t,
            draft_date=d - timedelta(days=lead_days),
            draft_time=draft_time,
            topic=topic,
            status=TaskStatus.SCHEDULED.value,
            is_active=True,
        )
        session.add(task)
        created.append(task)
    if created:
        await session.flush()
    return created, errors


# ---------- Планировщик: что пора генерировать / публиковать ----------


def publish_datetime(task: ContentTask, tz: ZoneInfo, default_time: time) -> datetime:
    """Дата+время публикации задачи как aware-datetime в зоне tz."""
    t = task.publish_time or default_time
    return datetime.combine(task.publish_date, t, tzinfo=tz)


def draft_datetime(task: ContentTask, tz: ZoneInfo, default_time: time, lead_days: int) -> datetime:
    """Дата+время подготовки черновика. Если не задано — публикация минус lead_days."""
    if task.draft_date is not None:
        t = task.draft_time or default_time
        return datetime.combine(task.draft_date, t, tzinfo=tz)
    base = publish_datetime(task, tz, default_time)
    return base - timedelta(days=lead_days)


async def tasks_due_for_draft(
    session: AsyncSession, now: datetime, tz: ZoneInfo, default_time: time, lead_days: int
) -> list[ContentTask]:
    """Активные scheduled-задачи, у которых наступил момент подготовки черновика."""
    result = await session.scalars(
        select(ContentTask)
        .where(ContentTask.is_active.is_(True))
        .where(ContentTask.status == TaskStatus.SCHEDULED.value)
        .order_by(ContentTask.publish_date, ContentTask.publish_time)
    )
    return [t for t in result if draft_datetime(t, tz, default_time, lead_days) <= now]


async def tasks_due_for_publish(
    session: AsyncSession, now: datetime, tz: ZoneInfo, default_time: time
) -> list[ContentTask]:
    """Одобренные задачи, у которых наступило время публикации."""
    result = await session.scalars(
        select(ContentTask)
        .where(ContentTask.is_active.is_(True))
        .where(ContentTask.status == TaskStatus.APPROVED.value)
        .order_by(ContentTask.publish_date, ContentTask.publish_time)
    )
    return [t for t in result if publish_datetime(t, tz, default_time) <= now]


async def tasks_awaiting_approval(session: AsyncSession) -> list[ContentTask]:
    """Задачи, ожидающие согласования (для текстового одобрения последней)."""
    result = await session.scalars(
        select(ContentTask)
        .where(ContentTask.status == TaskStatus.WAITING_FOR_APPROVAL.value)
        .order_by(ContentTask.updated_at.desc())
    )
    return list(result)


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
