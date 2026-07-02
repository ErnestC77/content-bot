from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str
    owner_telegram_id: int
    admin_username: str = "admin"
    admin_password: str
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/content_bot"
    anthropic_api_key: str = ""
    # Базовый URL Anthropic-совместимого API. По умолчанию — прокси AITunnel.
    # Для прямого доступа к Anthropic оставьте пустым.
    anthropic_base_url: str = "https://api.aitunnel.ru"
    ai_provider: str = "anthropic"
    ai_model: str = "claude-sonnet-5"
    default_channel_id: str = ""
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    timezone: str = "Europe/Moscow"
    daily_check_time: str = "10:00"
    bot_mode: str = "polling"  # polling | webhook
    webhook_url: str = ""
    webhook_secret: str = "change-me"
    # Render автоматически прокидывает публичный URL сервиса сюда.
    render_external_url: str = ""

    @property
    def effective_webhook_url(self) -> str:
        """URL для webhook: явный WEBHOOK_URL или авто-URL от Render."""
        return self.webhook_url or self.render_external_url

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        """Render/Heroku отдают postgres://; asyncpg требует postgresql+asyncpg://."""
        if value.startswith("postgres://"):
            value = value.replace("postgres://", "postgresql+asyncpg://", 1)
        elif value.startswith("postgresql://"):
            value = value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
