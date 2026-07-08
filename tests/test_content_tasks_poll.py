from app.services.content_tasks import detect_task_type, parse_poll_draft, PollValidationError
from app.database.models import TaskType
import pytest


def test_detect_task_type_plain_topic_is_post():
    task_type, topic = detect_task_type("Как выбрать велосипед")
    assert task_type == TaskType.POST.value
    assert topic == "Как выбрать велосипед"


def test_detect_task_type_marker_prefix_is_poll():
    task_type, topic = detect_task_type("📊 Какой формат вам интереснее?")
    assert task_type == TaskType.POLL.value
    assert topic == "Какой формат вам интереснее?"


def test_detect_task_type_marker_without_space():
    task_type, topic = detect_task_type("📊Какой формат?")
    assert task_type == TaskType.POLL.value
    assert topic == "Какой формат?"


def test_parse_poll_draft_valid():
    question, options = parse_poll_draft("Какой формат интереснее?\nВидео\nТекст\nОпрос")
    assert question == "Какой формат интереснее?"
    assert options == ["Видео", "Текст", "Опрос"]


def test_parse_poll_draft_strips_blank_lines():
    question, options = parse_poll_draft("Вопрос?\n\nВариант 1\n\nВариант 2\n")
    assert question == "Вопрос?"
    assert options == ["Вариант 1", "Вариант 2"]


def test_parse_poll_draft_empty_raises():
    with pytest.raises(PollValidationError):
        parse_poll_draft("   \n  ")


def test_parse_poll_draft_too_few_options_raises():
    with pytest.raises(PollValidationError):
        parse_poll_draft("Вопрос?\nЕдинственный вариант")


def test_parse_poll_draft_too_many_options_raises():
    text = "Вопрос?\n" + "\n".join(f"Вариант {i}" for i in range(11))
    with pytest.raises(PollValidationError):
        parse_poll_draft(text)


def test_parse_poll_draft_question_too_long_raises():
    with pytest.raises(PollValidationError):
        parse_poll_draft("Q" * 301 + "?\nВариант 1\nВариант 2")


def test_parse_poll_draft_option_too_long_raises():
    with pytest.raises(PollValidationError):
        parse_poll_draft("Вопрос?\n" + "O" * 101 + "\nВариант 2")
