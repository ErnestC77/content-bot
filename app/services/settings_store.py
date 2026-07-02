from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.models import AppSetting

# Ключи настроек, редактируемых через админку
KEY_SYSTEM_PROMPT = "system_prompt"
KEY_OWNER_TELEGRAM_ID = "owner_telegram_id"
KEY_CHANNEL_ID = "channel_id"
KEY_DAILY_CHECK_TIME = "daily_check_time"
KEY_AI_PROVIDER = "ai_provider"
KEY_AI_MODEL = "ai_model"


async def get_setting(session: AsyncSession, key: str, default: str = "") -> str:
    row = await session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None or not row.value:
        return default
    return row.value


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.scalar(select(AppSetting).where(AppSetting.key == key))
    if row is None:
        session.add(AppSetting(key=key, value=value))
    else:
        row.value = value


async def get_owner_telegram_id(session: AsyncSession) -> int:
    value = await get_setting(session, KEY_OWNER_TELEGRAM_ID, str(get_settings().owner_telegram_id))
    return int(value)


async def get_channel_id(session: AsyncSession) -> str:
    """Telegram ID канала для публикации: настройка из БД или DEFAULT_CHANNEL_ID из .env."""
    return await get_setting(session, KEY_CHANNEL_ID, get_settings().default_channel_id)


async def get_ai_provider(session: AsyncSession) -> str:
    return await get_setting(session, KEY_AI_PROVIDER, get_settings().ai_provider)


async def get_ai_model(session: AsyncSession) -> str:
    return await get_setting(session, KEY_AI_MODEL, get_settings().ai_model)


async def get_daily_check_time(session: AsyncSession) -> str:
    return await get_setting(session, KEY_DAILY_CHECK_TIME, get_settings().daily_check_time)
