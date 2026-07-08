from app.ai import prompts
from app.database.models import ContentTask


def _task(topic="Опрос про кофе"):
    return ContentTask(topic=topic, goal="", description="", rubric="")


def test_build_poll_prompt_includes_topic_and_markers():
    prompt = prompts.build_poll_prompt(_task())
    assert "Опрос про кофе" in prompt
    assert prompts.POST_START in prompt
    assert prompts.POST_END in prompt


def test_build_poll_revision_prompt_includes_previous_and_comment():
    prompt = prompts.build_poll_revision_prompt(_task(), "Вопрос?\nА\nБ", "сделай короче")
    assert "Вопрос?\nА\nБ" in prompt
    assert "сделай короче" in prompt


def test_build_poll_alternative_prompt_includes_previous():
    prompt = prompts.build_poll_alternative_prompt(_task(), "Вопрос?\nА\nБ")
    assert "Вопрос?\nА\nБ" in prompt
