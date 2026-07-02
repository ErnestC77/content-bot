"""Реализации AI-провайдеров. Чтобы добавить нового (OpenAI, DeepSeek и т.д.) —
реализуйте AIProvider и зарегистрируйте фабрику в PROVIDERS."""

import logging
from collections.abc import Callable

from app.ai.client import AIError, AIProvider
from app.config.settings import get_settings

logger = logging.getLogger(__name__)


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self, model: str) -> None:
        self.model = model

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        try:
            from anthropic import AsyncAnthropic

            settings = get_settings()
            # AITunnel и подобные прокси ждут токен в заголовке Authorization: Bearer
            # (SDK-параметр auth_token). Прямой Anthropic принимает x-api-key (api_key).
            if settings.anthropic_base_url:
                client = AsyncAnthropic(
                    auth_token=settings.anthropic_api_key,
                    base_url=settings.anthropic_base_url,
                )
            else:
                client = AsyncAnthropic(api_key=settings.anthropic_api_key)
            response = await client.messages.create(
                model=self.model,
                max_tokens=1500,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            if not text:
                raise AIError("Модель вернула пустой ответ")
            return text
        except AIError:
            raise
        except Exception as exc:  # сеть, авторизация, лимиты — всё наружу как AIError
            logger.exception("Ошибка Anthropic API")
            raise AIError(f"Anthropic API: {exc}") from exc


PROVIDERS: dict[str, Callable[[str], AIProvider]] = {
    "anthropic": AnthropicProvider,
}
