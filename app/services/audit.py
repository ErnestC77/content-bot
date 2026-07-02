import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ApprovalLog

logger = logging.getLogger(__name__)


async def log_action(
    session: AsyncSession,
    task_id: int,
    action: str,
    *,
    old_status: str | None = None,
    new_status: str | None = None,
    user_id: int | None = None,
    comment: str | None = None,
) -> None:
    """Записывает событие согласования в approval_logs (без commit)."""
    session.add(
        ApprovalLog(
            task_id=task_id,
            user_id=user_id,
            action=action,
            old_status=old_status,
            new_status=new_status,
            comment=comment,
        )
    )
    logger.info("task=%s action=%s %s->%s", task_id, action, old_status, new_status)
