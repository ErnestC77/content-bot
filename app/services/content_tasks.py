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
    TaskType,
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
    TaskStatus.WAITING_FOR_ANSWERS.value: "❓ ждёт ответов",
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

# Строка вида «05-07-2026 — Тема», «05.07.2026 10:00 — Тема» или «2026-07-05 — Тема».
_LINE_RE = re.compile(
    r"^\s*((?:\d{4}-\d{1,2}-\d{1,2})|(?:\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}))"
    r"\s*(\d{1,2}:\d{2})?\s*[—–\-|:•]*\s*(.+?)\s*$"
)

POLL_MARKER = "📊"


def detect_task_type(topic: str) -> tuple[str, str]:
    """Возвращает (task_type, тема без метки).

    Метка POLL_MARKER в начале темы означает, что строка массового добавления
    описывает опрос, а не обычный пост — так можно смешивать посты и опросы
    в одном списке.
    """
    if topic.startswith(POLL_MARKER):
        return TaskType.POLL.value, topic[len(POLL_MARKER):].strip()
    return TaskType.POST.value, topic


class PollValidationError(ValueError):
    """Черновик опроса не проходит ограничения Telegram (см. parse_poll_draft)."""


def parse_poll_draft(text: str) -> tuple[str, list[str]]:
    """Разбирает сериализованный черновик опроса: 1-я непустая строка — вопрос,
    остальные непустые строки — варианты ответа.

    Бросает PollValidationError, если вопрос пуст, вариантов меньше 2 или
    больше 10, либо превышены лимиты длины Telegram (вопрос ≤300 символов,
    вариант ≤100 символов).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise PollValidationError("Опрос пуст: нет ни вопроса, ни вариантов ответа.")
    question, options = lines[0], lines[1:]
    if len(question) > 300:
        raise PollValidationError(f"Вопрос длиннее 300 символов ({len(question)}).")
    if len(options) < 2:
        raise PollValidationError(f"Нужно минимум 2 варианта ответа, получено {len(options)}.")
    if len(options) > 10:
        raise PollValidationError(f"Максимум 10 вариантов ответа, получено {len(options)}.")
    too_long = next((o for o in options if len(o) > 100), None)
    if too_long:
        raise PollValidationError(f"Вариант ответа длиннее 100 символов: «{too_long[:40]}…»")
    return question, options


def _parse_date(token: str) -> date | None:
    """Разбирает дату в форматах dd-mm-yyyy, dd.mm.yyyy, dd/mm/yyyy или yyyy-mm-dd."""
    try:
        parts = [int(p) for p in re.split(r"[.\-/]", token)]
    except ValueError:
        return None
    if len(parts) != 3:
        return None
    if parts[0] > 31:  # год впереди -> yyyy-mm-dd
        y, m, d = parts
    else:  # dd-mm-yyyy
        d, m, y = parts
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_schedule_line(line: str, default_time: time) -> tuple[date, time, str] | None:
    """Возвращает (дата, время, тема) или None, если строку не разобрать."""
    m = _LINE_RE.match(line)
    if not m:
        return None
    date_str, time_str, topic = m.group(1), m.group(2), m.group(3)
    if not topic.strip():
        return None
    d = _parse_date(date_str)
    if d is None:
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
    session: AsyncSession, text: str, default_draft_time: time, lead_days: int, default_publish_time: time
) -> tuple[list[ContentTask], list[str]]:
    """Создаёт задачи по строкам «дата [время] — тема».

    Введённые дата/время — это дата/время ПОДГОТОВКИ ЧЕРНОВИКА (когда бот
    сгенерирует пост и пришлёт на согласование), а не дата публикации. Дата
    публикации вычисляется как черновик + lead_days в default_publish_time;
    обе даты дальше можно редактировать независимо. Возвращает (созданные, ошибки).
    """
    created: list[ContentTask] = []
    errors: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parsed = parse_schedule_line(raw, default_draft_time)
        if parsed is None:
            errors.append(raw.strip())
            continue
        d, t, topic = parsed
        task_type, topic = detect_task_type(topic)
        task = ContentTask(
            draft_date=d,
            draft_time=t,
            publish_date=d + timedelta(days=lead_days),
            publish_time=default_publish_time,
            topic=topic,
            task_type=task_type,
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


# Признак устаревшей версии системного промта (из ранней редакции, где AI
# просили самому дописывать блок согласования в конец черновика). Если в
# сохранённом в settings промте это ещё встречается — считаем его устаревшим
# и подменяем на актуальный дефолт, а не используем как есть.
_STALE_PROMPT_MARKERS = ("ФОРМАТ СОГЛАСОВАНИЯ", "Каждый черновик заканчивай блоком")


async def system_prompt(session: AsyncSession) -> str:
    value = await get_setting(session, KEY_SYSTEM_PROMPT, prompts.DEFAULT_SYSTEM_PROMPT)
    if any(marker in value for marker in _STALE_PROMPT_MARKERS):
        logger.warning("Обнаружен устаревший системный промт в settings — использую актуальный дефолт")
        return prompts.DEFAULT_SYSTEM_PROMPT
    return value


async def generate_questions(session: AsyncSession, task: ContentTask) -> list[str]:
    """Генерирует 1–3 уточняющих вопроса. При сбое AI — запасной набор из ТЗ."""
    try:
        provider_name = await get_ai_provider(session)
        model = await get_ai_model(session)
        provider = get_provider(provider_name, model)
        raw = await provider.generate(await system_prompt(session), prompts.build_questions_prompt(task))
        # Оставляем только строки-вопросы (заканчиваются на «?») — так рассуждения
        # или вступление модели («Вот несколько вопросов:» и т.п.) не попадают
        # в то, что видит владелец.
        lines = [line.strip(" -•\t") for line in raw.splitlines() if line.strip()]
        questions = [q for q in lines if q.endswith("?")][:3]
        return questions or prompts.QUESTION_FALLBACK
    except AIError:
        logger.warning("Не удалось сгенерировать вопросы через AI, использую запасные")
        return prompts.QUESTION_FALLBACK


def extract_marked(text: str) -> str:
    """Достаёт чистый текст поста между метками POST_START/POST_END.

    Защита от того, что модель добавит вступление/рассуждения до или после
    самого текста поста, несмотря на инструкцию в промте. Если меток нет
    (модель их не поставила) — возвращает исходный текст как есть.
    """
    start = text.find(prompts.POST_START)
    end = text.find(prompts.POST_END)
    if start != -1 and end != -1 and end > start:
        text = text[start + len(prompts.POST_START) : end]
    return _strip_approval_block(text.strip())


# Признаки мета-блока согласования (кнопки/подписи), который иногда добавляет
# модель по старой памяти/инерции — этот блок формирует сам бот в интерфейсе
# согласования, в тексте ПОСТА ему быть не должно ни при каких условиях.
_APPROVAL_MARKERS = (
    "📋 Жду вашего решения",
    "✅ Одобряю —",
    "✏️ Правки —",
    "🔄 Другой вариант —",
    "❌ Отменить —",
)


def _strip_approval_block(text: str) -> str:
    """Обрезает хвост текста, если в нём встретился мета-блок согласования."""
    for marker in _APPROVAL_MARKERS:
        idx = text.find(marker)
        if idx == -1:
            continue
        head = text[:idx].rstrip()
        # убрать горизонтальный разделитель ("———"/"---"), если он прямо перед меткой
        head = re.sub(r"[—\-–]{2,}\s*$", "", head).rstrip()
        return head
    return text


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
    """Генерирует новую версию поста или опроса (по task.task_type) и сохраняет её.

    kind: initial | revision | alternative.
    Бросает AIError, если провайдер недоступен, или PollValidationError, если
    ИИ вернул некорректный опрос — статус при этом НЕ переводится в
    waiting_for_approval (это делает вызывающий код только при успехе).
    """
    provider_name = await get_ai_provider(session)
    model = await get_ai_model(session)
    provider = get_provider(provider_name, model)

    is_poll = task.task_type == TaskType.POLL.value
    previous = task.posts[-1].text if task.posts else ""
    if kind == "revision":
        builder = prompts.build_poll_revision_prompt if is_poll else prompts.build_revision_prompt
        prompt = builder(task, previous, revision_comment or "")
    elif kind == "alternative":
        builder = prompts.build_poll_alternative_prompt if is_poll else prompts.build_alternative_prompt
        prompt = builder(task, previous)
    else:
        builder = prompts.build_poll_prompt if is_poll else prompts.build_generation_prompt
        prompt = builder(task)

    raw_text = await provider.generate(await system_prompt(session), prompt)
    text = extract_marked(raw_text)
    if is_poll:
        # Бросает PollValidationError ДО сохранения версии, если ИИ вернул
        # некорректный опрос. Вызывающий код (app/bot/flow.py) уже ловит любое
        # исключение из generate_post_version как общий сбой генерации — то же
        # сообщение владельцу «AI недоступен», без сохранения битого черновика.
        parse_poll_draft(text)

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
