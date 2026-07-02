"""Контроль доступа: владелец + администраторы (таблица users)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.models import User, UserRole

_ROLES = (UserRole.OWNER.value, UserRole.ADMIN.value)


async def is_authorized(session: AsyncSession, telegram_id: int) -> bool:
    """Владелец из .env всегда разрешён; иначе — активный admin/owner из БД."""
    if telegram_id == get_settings().owner_telegram_id:
        return True
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    return user is not None and user.is_active and user.role in _ROLES


async def list_admins(session: AsyncSession) -> list[dict]:
    """Список доступа: владелец (неудаляемый) + активные админы."""
    owner_id = get_settings().owner_telegram_id
    rows = list(await session.scalars(
        select(User).where(User.is_active.is_(True)).where(User.role.in_(_ROLES))
    ))
    owner_row = next((u for u in rows if u.telegram_id == owner_id), None)
    result = [{
        "telegram_id": owner_id,
        "name": owner_row.name if owner_row else "Владелец",
        "role": "owner",
        "removable": False,
    }]
    seen = {owner_id}
    for u in rows:
        if u.telegram_id in seen:
            continue
        result.append({"telegram_id": u.telegram_id, "name": u.name, "role": u.role, "removable": True})
        seen.add(u.telegram_id)
    return result


async def add_admin(session: AsyncSession, telegram_id: int, name: str = "") -> None:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        session.add(User(telegram_id=telegram_id, name=name or "admin",
                         role=UserRole.ADMIN.value, is_active=True))
    else:
        user.is_active = True
        if user.telegram_id != get_settings().owner_telegram_id:
            user.role = UserRole.ADMIN.value
    await session.flush()


async def seed_admins(session: AsyncSession, telegram_ids: list[int]) -> None:
    """Создаёт админов из списка, только если записи ещё нет (идемпотентно)."""
    for tid in telegram_ids:
        existing = await session.scalar(select(User).where(User.telegram_id == tid))
        if existing is None:
            session.add(User(telegram_id=tid, name="admin",
                             role=UserRole.ADMIN.value, is_active=True))
    await session.flush()


async def remove_admin(session: AsyncSession, telegram_id: int) -> bool:
    """Деактивирует админа (не удаляем из-за внешних ссылок). Владельца нельзя."""
    if telegram_id == get_settings().owner_telegram_id:
        return False
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is not None:
        user.is_active = False
    return True
