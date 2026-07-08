import asyncio
from types import SimpleNamespace

import pytest

from app.services.publishing import _send_poll
from app.services.content_tasks import PollValidationError


class FakeBot:
    def __init__(self):
        self.calls = []

    async def send_poll(self, **kwargs):
        self.calls.append(kwargs)


def test_send_poll_passes_parsed_question_and_options():
    bot = FakeBot()
    task = SimpleNamespace()  # _send_poll не читает поля task, параметр только для единой сигнатуры с _send_post
    asyncio.run(_send_poll(bot, "-100123", "Вопрос?\nВариант 1\nВариант 2", task))
    assert bot.calls == [{
        "chat_id": "-100123",
        "question": "Вопрос?",
        "options": ["Вариант 1", "Вариант 2"],
        "is_anonymous": True,
        "allows_multiple_answers": False,
    }]


def test_send_poll_raises_on_invalid_draft():
    bot = FakeBot()
    task = SimpleNamespace()
    with pytest.raises(PollValidationError):
        asyncio.run(_send_poll(bot, "-100123", "Только вопрос без вариантов", task))
