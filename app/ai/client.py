"""Абстрактный AI-клиент: единый интерфейс для любых провайдеров."""

from abc import ABC, abstractmethod


class AIError(Exception):
    """AI-провайдер недоступен или вернул ошибку."""


class AIProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        """Возвращает текст ответа модели. Бросает AIError при сбое."""


def get_provider(provider_name: str, model: str) -> AIProvider:
    # Импорт внутри функции, чтобы не тянуть SDK провайдеров при импорте модуля
    from app.ai.providers import PROVIDERS

    factory = PROVIDERS.get(provider_name)
    if factory is None:
        raise AIError(f"Неизвестный AI-провайдер: {provider_name}")
    return factory(model)
