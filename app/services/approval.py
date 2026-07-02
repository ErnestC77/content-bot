"""Единственная точка смены статусов задач и распознавания одобрения.

Главное правило системы: публикация возможна только по цепочке
    waiting_for_approval -> approved -> publishing -> published
и только после явного действия владельца.
"""

import re
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ContentTask, TaskStatus
from app.services import audit

# Фразы владельца, которые считаются явным одобрением (по ТЗ).
APPROVAL_PHRASES = frozenset({"одобряю", "публикуй", "можно публиковать", "утверждаю"})


class InvalidTransitionError(Exception):
    """Попытка запрещённого перехода статуса."""

    def __init__(self, old: str, new: str) -> None:
        super().__init__(f"Переход {old} -> {new} запрещён")
        self.old = old
        self.new = new


# Разрешённые переходы статусов. Любой переход, которого здесь нет, отклоняется
# с InvalidTransitionError — это и есть защита от автопубликации.
# Публикация возможна ТОЛЬКО по цепочке waiting_for_approval -> approved
# -> publishing -> published. Из generating/scheduled/draft/revision_requested/
# cancelled попасть в published нельзя.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.DRAFT.value: {
        TaskStatus.SCHEDULED.value,
        TaskStatus.WAITING_FOR_ANSWERS.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.SCHEDULED.value: {
        TaskStatus.WAITING_FOR_ANSWERS.value,
        TaskStatus.GENERATING.value,       # прямая авто-генерация из темы
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.WAITING_FOR_ANSWERS.value: {
        TaskStatus.COLLECTING_MEDIA.value,
        TaskStatus.GENERATING.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.COLLECTING_MEDIA.value: {
        TaskStatus.GENERATING.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.GENERATING.value: {
        TaskStatus.WAITING_FOR_APPROVAL.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.WAITING_FOR_APPROVAL.value: {
        TaskStatus.APPROVED.value,          # явное одобрение
        TaskStatus.REVISION_REQUESTED.value,  # правки
        TaskStatus.GENERATING.value,        # «другой вариант»
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.REVISION_REQUESTED.value: {
        TaskStatus.GENERATING.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.APPROVED.value: {
        TaskStatus.PUBLISHING.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.PUBLISHING.value: {
        TaskStatus.PUBLISHED.value,
        TaskStatus.PUBLISH_FAILED.value,
    },
    TaskStatus.PUBLISH_FAILED.value: {
        TaskStatus.APPROVED.value,    # повторная попытка публикации
        TaskStatus.PUBLISHING.value,
        TaskStatus.CANCELLED.value,
    },
    # published и cancelled — терминальные статусы, выходов нет.
    TaskStatus.PUBLISHED.value: set(),
    TaskStatus.CANCELLED.value: set(),
}


def can_transition(old_status: str, new_status: str) -> bool:
    return new_status in ALLOWED_TRANSITIONS.get(old_status, set())


def is_text_approval(text: str) -> bool:
    """Является ли текстовое сообщение владельца явным одобрением.

    Одобрением считаются ТОЛЬКО фразы из APPROVAL_PHRASES при точном совпадении
    после нормализации. Проверка подстрокой недопустима: «не одобряю» не должно
    считаться одобрением. При любом сомнении возвращаем False.
    """
    if not text:
        return False
    # нормализация: нижний регистр, схлопывание пробелов, срез крайних знаков
    normalized = re.sub(r"\s+", " ", text.strip().lower()).strip(" .!?,")
    return normalized in APPROVAL_PHRASES


async def change_status(
    session: AsyncSession,
    task: ContentTask,
    new_status: TaskStatus,
    *,
    action: str | None = None,
    user_id: int | None = None,
    comment: str | None = None,
) -> None:
    """Меняет статус задачи с проверкой перехода и записью в approval_logs.

    Не делает commit — граница транзакции остаётся за вызывающим кодом.
    """
    old = task.status
    new = new_status.value
    if not can_transition(old, new):
        raise InvalidTransitionError(old, new)

    now = datetime.now(timezone.utc)
    task.status = new
    if new_status is TaskStatus.APPROVED:
        task.approved_at = now
        task.approved_by_user_id = user_id
    elif new_status is TaskStatus.PUBLISHED:
        task.published_at = now
    elif new_status is TaskStatus.CANCELLED:
        task.cancelled_at = now

    if action:
        await audit.log_action(
            session,
            task.id,
            action,
            old_status=old,
            new_status=new,
            user_id=user_id,
            comment=comment,
        )
