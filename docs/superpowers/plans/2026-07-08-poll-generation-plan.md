# Опросы: ИИ создаёт опрос по запросу — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Позволить владельцу канала запросить у ИИ подготовку нативного Telegram-опроса (`bot.send_poll`) — самостоятельного или привязанного к посту, — с тем же циклом черновик → одобрение → публикация, что и у обычных постов.

**Architecture:** Опрос — не новая сущность, а `ContentTask` с `task_type="poll"`. Вопрос и варианты ответа сериализуются в существующую таблицу версий `GeneratedPost.text` (1-я строка — вопрос, остальные — варианты), это бесплатно даёт опросу версионирование/правки/альтернативы. Вся оркестрация (расписание, наводящие вопросы, одобрение, аудит, рассылка) переиспользуется без изменений — новый код только ветвится по `task_type` в трёх точках: генерация промпта, публикация (`bot.send_poll` вместо `send_message`), отображение в Mini App.

**Tech Stack:** Python 3.12, aiogram 3, FastAPI, SQLAlchemy 2 (async) + Alembic, ванильный JS во фронтенде Mini App (без сборки). Для тестов новой чистой логики парсинга — pytest (в проекте до сих пор не было тестовой инфраструктуры; добавляется только для этой фичи, см. Global Constraints).

## Global Constraints

- Спецификация: `docs/superpowers/specs/2026-07-08-poll-generation-design.md` — при любом сомнении сверяться с ней.
- Настройки опроса (анонимность/несколько ответов) НЕ конфигурируются владельцем — фиксировано `is_anonymous=True`, `allows_multiple_answers=False`.
- Лимиты Telegram (жёстко зашиты в валидации): вопрос ≤300 символов, 2–10 вариантов, каждый вариант ≤100 символов.
- Маркер опроса в массовом добавлении — эмодзи `📊` в начале темы.
- В проекте нет автоматических тестов и живой БД/Telegram-доступа в среде реализации. Для новой **чистой** логики (парсинг/валидация, без БД и сети) — пишем реальные pytest-тесты (venv в `.venv/`, `pytest` устанавливается как dev-зависимость). Для кода, трогающего БД/AI-провайдера/Telegram API/браузерный UI — пишем только компилируемый код и явно проверяем вручную (см. Task 7); не изобретать моки ради моков.
- Все существующие модули (`approval.py`, `audit.py`, `notify.py`, `scheduler.py`) НЕ меняются — транзакции статусов работают по `TaskStatus` независимо от `task_type`.
- Коммитить только файлы конкретной задачи (`git add <path>`), не трогать уже имеющиеся в рабочем дереве незакоммиченные правки фичи «цитаты» (`app/database/models.py`, `app/services/publishing.py`, `app/webapp/templates/app.html` уже содержат несохранённые изменения из предыдущей задачи — это чужая незавершённая работа этой же сессии, не отменять и не коммитить её вместе с опросами).

---

### Task 1: Модель данных — `TaskType`, `task_type`, `related_task_id`

**Files:**
- Modify: `app/database/models.py`
- Create: `app/database/migrations/versions/0006_poll_task.py`

**Interfaces:**
- Produces: `TaskType` enum (`TaskType.POST.value == "post"`, `TaskType.POLL.value == "poll"`) — используется во всех следующих задачах. `ContentTask.task_type: str` (default `"post"`). `ContentTask.related_task_id: int | None` (self-FK на `content_tasks.id`, `ondelete="SET NULL"`). `ContentTask.related_task: ContentTask | None` (relationship, `lazy="selectin"`).

- [ ] **Step 1: Добавить `TaskType` и колонки в `ContentTask`**

В `app/database/models.py` после класса `UserRole` (строка 60) добавить:

```python
class TaskType(str, enum.Enum):
    POST = "post"
    POLL = "poll"
```

В классе `ContentTask` после строки `rubric: Mapped[str] = mapped_column(String(255), default="")` (строка 107) добавить:

```python
    # "post" — обычный текстовый/медиа-пост (по умолчанию), "poll" — опрос
    # (вопрос+варианты сериализованы в GeneratedPost.text, см. content_tasks.parse_poll_draft)
    task_type: Mapped[str] = mapped_column(String(20), default=TaskType.POST.value, index=True)
    # необязательная связь «опрос к посту N» — только для отображения в Mini App,
    # одобрение и публикация у связанных задач полностью независимые
    related_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_tasks.id", ondelete="SET NULL"), nullable=True
    )
```

В блоке relationship (после `channel: Mapped[Channel | None] = relationship(lazy="selectin")`, строка 126) добавить:

```python
    related_task: Mapped["ContentTask | None"] = relationship(
        remote_side=[id], lazy="selectin", foreign_keys=[related_task_id]
    )
```

- [ ] **Step 2: Проверить синтаксис и структурную корректность модели**

Run: `.venv/Scripts/python.exe -m py_compile app/database/models.py`
Expected: без вывода (успех).

Run (структурная проверка через SQLite in-memory, без реальной Postgres):
```bash
.venv/Scripts/python.exe -c "
from sqlalchemy import create_engine
from app.database.models import Base, ContentTask, TaskType

engine = create_engine('sqlite://')
Base.metadata.create_all(engine)
t = ContentTask(topic='x', task_type=TaskType.POLL.value)
assert t.task_type == 'poll'
assert t.related_task_id is None
print('OK')
"
```
Expected: `OK`.

- [ ] **Step 3: Написать миграцию Alembic 0006**

Создать `app/database/migrations/versions/0006_poll_task.py`:

```python
"""content_tasks: тип задачи (пост/опрос) + необязательная связь с постом

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-08

"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "content_tasks",
        sa.Column("task_type", sa.String(length=20), nullable=False, server_default="post"),
    )
    op.create_index(
        op.f("ix_content_tasks_task_type"), "content_tasks", ["task_type"], unique=False
    )
    op.add_column(
        "content_tasks",
        sa.Column("related_task_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_content_tasks_related_task_id",
        "content_tasks", "content_tasks",
        ["related_task_id"], ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_content_tasks_related_task_id", "content_tasks", type_="foreignkey")
    op.drop_column("content_tasks", "related_task_id")
    op.drop_index(op.f("ix_content_tasks_task_type"), table_name="content_tasks")
    op.drop_column("content_tasks", "task_type")
```

- [ ] **Step 4: Проверить синтаксис миграции**

Run: `.venv/Scripts/python.exe -m py_compile app/database/migrations/versions/0006_poll_task.py`
Expected: без вывода.

Применение к реальной Postgres (`alembic upgrade head`) в этой среде недоступно (нет живой БД) — владелец должен прогнать миграцию в dev/VDS-окружении перед деплоем (см. Task 7).

- [ ] **Step 5: Commit**

```bash
git add app/database/models.py app/database/migrations/versions/0006_poll_task.py
git commit -m "$(cat <<'EOF'
feat: добавить task_type (post/poll) и related_task_id в ContentTask

Опрос — не новая сущность, а ContentTask с task_type=poll, чтобы бесплатно
переиспользовать существующий пайплайн черновик/одобрение/публикация.
EOF
)"
```

---

### Task 2: Разбор массового добавления с меткой опроса + валидация черновика опроса

**Files:**
- Modify: `app/services/content_tasks.py`
- Create: `pytest.ini`
- Create: `requirements-dev.txt`
- Create: `tests/test_content_tasks_poll.py`

**Interfaces:**
- Consumes: `TaskType` (Task 1).
- Produces: `content_tasks.POLL_MARKER: str`. `content_tasks.detect_task_type(topic: str) -> tuple[str, str]`. `content_tasks.PollValidationError(ValueError)`. `content_tasks.parse_poll_draft(text: str) -> tuple[str, list[str]]` — используются в Task 3 (генерация) и Task 4 (публикация).

- [ ] **Step 1: Настроить pytest для проекта**

Создать `pytest.ini` в корне репозитория:

```ini
[pytest]
pythonpath = .
testpaths = tests
```

Создать `requirements-dev.txt`:

```
-r requirements.txt
pytest>=8.0
```

Установить в существующий venv проекта:

Run: `.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`
Expected: `Successfully installed pytest-...` (или "already satisfied", если уже стоит).

- [ ] **Step 2: Написать падающие тесты для `detect_task_type`**

Создать `tests/test_content_tasks_poll.py`:

```python
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
```

- [ ] **Step 3: Запустить тесты, убедиться что падают**

Run: `.venv/Scripts/python.exe -m pytest tests/test_content_tasks_poll.py -v`
Expected: `ImportError`/`ModuleNotFoundError: cannot import name 'detect_task_type'` (функции ещё нет).

- [ ] **Step 4: Реализовать `detect_task_type` и вписать в `bulk_create_tasks`**

В `app/services/content_tasks.py` добавить `TaskType` в импорт (строка 13-20, блок `from app.database.models import (...)`):

```python
from app.database.models import (
    ApprovalAction,
    ContentTask,
    GeneratedPost,
    TaskStatus,
    TaskType,
    User,
    UserRole,
)
```

После `_LINE_RE` (строка 65) добавить:

```python
POLL_MARKER = "📊"


def detect_task_type(topic: str) -> tuple[str, str]:
    """Возвращает (task_type, тема без метки).

    Метка POLL_MARKER в начале темы означает, что строка массового добавления
    описывает опрос, а не обычный пост — так можно смешивать посты и опросы
    в одном списке.
    """
    if topic.startswith(POLL_MARKER):
        return TaskType.POLL.value, topic[len(POLL_MARKER):].strip()
    return TaskType.POST.value, topic
```

В `bulk_create_tasks` (строка 126-135) заменить:

```python
        d, t, topic = parsed
        task = ContentTask(
            draft_date=d,
            draft_time=t,
            publish_date=d + timedelta(days=lead_days),
            publish_time=default_publish_time,
            topic=topic,
            status=TaskStatus.SCHEDULED.value,
            is_active=True,
        )
```

на:

```python
        d, t, topic = parsed
        task_type, topic = detect_task_type(topic)
        task = ContentTask(
            draft_date=d,
            draft_time=t,
            publish_date=d + timedelta(days=lead_days),
            publish_time=default_publish_time,
            topic=topic,
            task_type=task_type,
            status=TaskStatus.SCHEDULED.value,
            is_active=True,
        )
```

- [ ] **Step 5: Запустить тесты, убедиться что проходят**

Run: `.venv/Scripts/python.exe -m pytest tests/test_content_tasks_poll.py -v`
Expected: 3 теста PASSED (остальные из файла ещё не написаны — см. следующий шаг).

- [ ] **Step 6: Написать падающие тесты для `parse_poll_draft`**

Добавить в `tests/test_content_tasks_poll.py`:

```python
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
```

- [ ] **Step 7: Запустить, убедиться что падают (нет `parse_poll_draft`)**

Run: `.venv/Scripts/python.exe -m pytest tests/test_content_tasks_poll.py -v`
Expected: тесты из Step 6 FAIL с `ImportError`.

- [ ] **Step 8: Реализовать `PollValidationError` и `parse_poll_draft`**

В `app/services/content_tasks.py` после `detect_task_type` добавить:

```python
class PollValidationError(ValueError):
    """Черновик опроса не проходит ограничения Telegram (см. parse_poll_draft)."""


def parse_poll_draft(text: str) -> tuple[str, list[str]]:
    """Разбирает сериализованный черновик опроса: 1-я непустая строка — вопрос,
    остальные непустые строки — варианты ответа.

    Бросает PollValidationError, если вопрос пуст, вариантов меньше 2 или
    больше 10, либо превышены лимиты длины Telegram (вопрос ≤300 символов,
    вариант ≤100 символов).
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise PollValidationError("Опрос пуст: нет ни вопроса, ни вариантов ответа.")
    question, options = lines[0], lines[1:]
    if len(question) > 300:
        raise PollValidationError(f"Вопрос длиннее 300 символов ({len(question)}).")
    if len(options) < 2:
        raise PollValidationError(f"Нужно минимум 2 варианта ответа, получено {len(options)}.")
    if len(options) > 10:
        raise PollValidationError(f"Максимум 10 вариантов ответа, получено {len(options)}.")
    too_long = next((o for o in options if len(o) > 100), None)
    if too_long:
        raise PollValidationError(f"Вариант ответа длиннее 100 символов: «{too_long[:40]}…»")
    return question, options
```

- [ ] **Step 9: Запустить все тесты файла, убедиться что проходят**

Run: `.venv/Scripts/python.exe -m pytest tests/test_content_tasks_poll.py -v`
Expected: все 10 тестов PASSED.

- [ ] **Step 10: Commit**

```bash
git add pytest.ini requirements-dev.txt tests/test_content_tasks_poll.py app/services/content_tasks.py
git commit -m "$(cat <<'EOF'
feat: метка 📊 для опроса в массовом добавлении + валидация черновика опроса

detect_task_type распознаёт метку в теме строки bulk-добавления.
parse_poll_draft проверяет черновик опроса против лимитов Telegram
(2-10 вариантов, длина) до того, как он попадёт владельцу на одобрение.
EOF
)"
```

---

### Task 3: Промпты опроса + ветвление генерации черновика

**Files:**
- Modify: `app/ai/prompts.py`
- Modify: `app/services/content_tasks.py`
- Create: `tests/test_prompts_poll.py`

**Interfaces:**
- Consumes: `TaskType` (Task 1), `parse_poll_draft` (Task 2).
- Produces: `prompts.build_poll_prompt(task)`, `prompts.build_poll_revision_prompt(task, previous_text, revision_comment)`, `prompts.build_poll_alternative_prompt(task, previous_text)` — все возвращают `str`, используют существующие `POST_START`/`POST_END`/`_MARKER_INSTRUCTION`, так что `extract_marked()` работает с ними без изменений.

- [ ] **Step 1: Написать падающий тест для промптов опроса**

Создать `tests/test_prompts_poll.py`:

```python
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
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts_poll.py -v`
Expected: `AttributeError: module 'app.ai.prompts' has no attribute 'build_poll_prompt'`.

- [ ] **Step 3: Реализовать промпты опроса**

В `app/ai/prompts.py` после `build_alternative_prompt` (конец файла, после строки 167) добавить:

```python


def build_poll_prompt(task: ContentTask) -> str:
    return (
        "Придумай опрос (голосование) для Telegram-канала на основе задачи из календаря.\n\n"
        f"{_task_block(task)}\n\n"
        f"{_answers_block(task)}\n\n"
        "Сформулируй короткий вопрос и от 2 до 5 коротких вариантов ответа.\n"
        "Верни вопрос первой строкой, каждый вариант ответа — с новой строки, "
        "без нумерации и пояснений."
        f"{_MARKER_INSTRUCTION}"
    )


def build_poll_revision_prompt(task: ContentTask, previous_text: str, revision_comment: str) -> str:
    return (
        "Ниже черновик опроса (вопрос и варианты ответа) и правки владельца. "
        "Перепиши его с учётом правок, сохранив задачу.\n\n"
        f"{_task_block(task)}\n\n"
        f"Текущий черновик опроса:\n{previous_text}\n\n"
        f"Правки владельца:\n{revision_comment}\n\n"
        "Верни вопрос первой строкой, каждый вариант ответа — с новой строки, "
        "без нумерации и пояснений."
        f"{_MARKER_INSTRUCTION}"
    )


def build_poll_alternative_prompt(task: ContentTask, previous_text: str) -> str:
    return (
        "Владельцу не подошёл вариант опроса ниже. Придумай другой опрос на ту же "
        "задачу: другой вопрос или другие варианты, но та же тема.\n\n"
        f"{_task_block(task)}\n\n"
        f"{_answers_block(task)}\n\n"
        f"Предыдущий вариант (не повторяй его):\n{previous_text}\n\n"
        "Верни вопрос первой строкой, каждый вариант ответа — с новой строки, "
        "без нумерации и пояснений."
        f"{_MARKER_INSTRUCTION}"
    )
```

- [ ] **Step 4: Запустить, убедиться что проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/test_prompts_poll.py -v`
Expected: 3 теста PASSED.

- [ ] **Step 5: Ветвление в `generate_post_version`**

В `app/services/content_tasks.py` заменить тело `generate_post_version` (строки 313-322):

```python
    previous = task.posts[-1].text if task.posts else ""
    if kind == "revision":
        prompt = prompts.build_revision_prompt(task, previous, revision_comment or "")
    elif kind == "alternative":
        prompt = prompts.build_alternative_prompt(task, previous)
    else:
        prompt = prompts.build_generation_prompt(task)

    raw_text = await provider.generate(await system_prompt(session), prompt)
    text = extract_marked(raw_text)
```

на:

```python
    is_poll = task.task_type == TaskType.POLL.value
    previous = task.posts[-1].text if task.posts else ""
    if kind == "revision":
        builder = prompts.build_poll_revision_prompt if is_poll else prompts.build_revision_prompt
        prompt = builder(task, previous, revision_comment or "")
    elif kind == "alternative":
        builder = prompts.build_poll_alternative_prompt if is_poll else prompts.build_alternative_prompt
        prompt = builder(task, previous)
    else:
        builder = prompts.build_poll_prompt if is_poll else prompts.build_generation_prompt
        prompt = builder(task)

    raw_text = await provider.generate(await system_prompt(session), prompt)
    text = extract_marked(raw_text)
    if is_poll:
        # Бросает PollValidationError ДО сохранения версии, если ИИ вернул
        # некорректный опрос. Вызывающий код (app/bot/flow.py) уже ловит любое
        # исключение из generate_post_version как общий сбой генерации — то же
        # сообщение владельцу «AI недоступен», без сохранения битого черновика.
        parse_poll_draft(text)
```

Также обновить docstring функции (строка 303-307), заменив:
```python
    """Генерирует новую версию поста и сохраняет её.

    kind: initial | revision | alternative.
    Бросает AIError, если провайдер недоступен — статус при этом НЕ переводится
    в waiting_for_approval (это делает вызывающий код только при успехе).
    """
```
на:
```python
    """Генерирует новую версию поста или опроса (по task.task_type) и сохраняет её.

    kind: initial | revision | alternative.
    Бросает AIError, если провайдер недоступен, или PollValidationError, если
    ИИ вернул некорректный опрос — статус при этом НЕ переводится в
    waiting_for_approval (это делает вызывающий код только при успехе).
    """
```

- [ ] **Step 6: Проверить компиляцию и полный прогон тестов**

Run: `.venv/Scripts/python.exe -m py_compile app/ai/prompts.py app/services/content_tasks.py`
Expected: без вывода.

Run: `.venv/Scripts/python.exe -m pytest -v`
Expected: все тесты (Task 2 + Task 3) PASSED.

- [ ] **Step 7: Commit**

```bash
git add app/ai/prompts.py app/services/content_tasks.py tests/test_prompts_poll.py
git commit -m "$(cat <<'EOF'
feat: промпты опроса + ветвление generate_post_version по task_type

Опрос генерируется, правится и получает альтернативы тем же generate_post_version,
что и посты — отличается только промпт и валидация результата (parse_poll_draft)
перед сохранением версии.
EOF
)"
```

---

### Task 4: Публикация опроса (`bot.send_poll`)

**Files:**
- Modify: `app/services/publishing.py`
- Create: `tests/test_publishing_poll.py`

**Interfaces:**
- Consumes: `TaskType` (Task 1), `content_tasks.parse_poll_draft`/`PollValidationError` (Task 2).
- Produces: `publishing._send_poll(bot, channel_id, text, task)` (async) — вызывается из `publish_task` наравне с `_send_post`.

- [ ] **Step 1: Написать падающий тест для `_send_poll`**

Создать `tests/test_publishing_poll.py`:

```python
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
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `.venv/Scripts/python.exe -m pytest tests/test_publishing_poll.py -v`
Expected: `ImportError: cannot import name '_send_poll'`.

- [ ] **Step 3: Реализовать `_send_poll` и завести в `publish_task`**

В `app/services/publishing.py` добавить в импорт моделей (строка 18-23) `TaskType`:

```python
from app.database.models import (
    ApprovalAction,
    ContentTask,
    MediaType,
    TaskStatus,
    TaskType,
)
```

Добавить импорт `content_tasks` (строка 24, рядом с `from app.services import audit`):

```python
from app.services import audit, content_tasks
```

После функции `_send_post` (после строки 156, перед `def _truncate_at_word`) добавить:

```python


async def _send_poll(bot: Bot, channel_id: str, text: str, task: ContentTask) -> None:
    """Публикует опрос: text — сериализованный черновик (вопрос + варианты,
    см. content_tasks.parse_poll_draft). task не используется — параметр
    только ради единой сигнатуры с _send_post (общая точка вызова в publish_task)."""
    question, options = content_tasks.parse_poll_draft(text)
    await bot.send_poll(
        chat_id=channel_id,
        question=question,
        options=options,
        is_anonymous=True,
        allows_multiple_answers=False,
    )
```

В `publish_task` заменить (строки 236-238):
```python
    text = task.posts[-1].text
    try:
        await _send_post(bot, channel_id, text, task)
    except TelegramAPIError as exc:
```
на:
```python
    text = task.posts[-1].text
    send = _send_poll if task.task_type == TaskType.POLL.value else _send_post
    try:
        await send(bot, channel_id, text, task)
    except (TelegramAPIError, content_tasks.PollValidationError) as exc:
```

- [ ] **Step 4: Запустить, убедиться что проходит**

Run: `.venv/Scripts/python.exe -m pytest tests/test_publishing_poll.py -v`
Expected: 2 теста PASSED.

- [ ] **Step 5: Полный прогон + компиляция**

Run: `.venv/Scripts/python.exe -m py_compile app/services/publishing.py && .venv/Scripts/python.exe -m pytest -v`
Expected: без ошибок компиляции, все тесты PASSED.

- [ ] **Step 6: Commit**

```bash
git add app/services/publishing.py tests/test_publishing_poll.py
git commit -m "$(cat <<'EOF'
feat: публикация опроса через bot.send_poll

publish_task ветвится по task_type: опрос парсится (parse_poll_draft) и уходит
через bot.send_poll вместо send_message/медиа. Ошибка парсинга ловится там же,
где TelegramAPIError — тот же PUBLISH_FAILED-путь с понятным текстом владельцу.
EOF
)"
```

---

### Task 5: Backend Mini App — сериализация задачи + точка входа «+ Опрос»

**Files:**
- Modify: `app/webapp/routes.py`

**Interfaces:**
- Consumes: `TaskType`, `ContentTask.related_task_id`/`related_task` (Task 1).
- Produces: `_task_dict()` включает `task_type`, `related_task_id`, `related_topic`, `poll_question`, `poll_options`. Новый эндпоинт `POST /api/webapp/tasks/poll` (тело `{topic, draft_date, draft_time?, related_task_id?}` → `{ok, id}` или `{ok: false, message}`).

- [ ] **Step 1: Расширить `_task_dict`**

В `app/webapp/routes.py` добавить `TaskType` в импорт (строка 16):

```python
from app.database.models import ContentTask, TaskMedia, TaskStatus, TaskType
```

В `_task_dict` перед строкой `if full:` (строка 90) вставить:

```python
    data["task_type"] = task.task_type
    data["related_task_id"] = task.related_task_id
    data["related_topic"] = task.related_task.topic if task.related_task else ""
    if task.task_type == TaskType.POLL.value:
        poll_lines = [line.strip() for line in data["text"].splitlines() if line.strip()]
        data["poll_question"] = poll_lines[0] if poll_lines else ""
        data["poll_options"] = poll_lines[1:]
    else:
        data["poll_question"] = ""
        data["poll_options"] = []
```

- [ ] **Step 2: Проверить компиляцию**

Run: `.venv/Scripts/python.exe -m py_compile app/webapp/routes.py`
Expected: без вывода.

- [ ] **Step 3: Добавить эндпоинт быстрого добавления опроса**

В `app/webapp/routes.py` изменить импорт datetime (строка 4):

```python
from datetime import date, datetime, time, timedelta
```

После эндпоинта `bulk_add` (после строки 145, перед `class EditBody`) добавить:

```python
class AddPollBody(BaseModel):
    topic: str
    draft_date: str
    draft_time: str = ""
    related_task_id: int | None = None


@api.post("/tasks/poll")
async def add_poll(body: AddPollBody, session: AsyncSession = Depends(get_session_dependency)):
    """Быстрое добавление одного опроса — кнопки «+ Опрос» и «Опрос к посту» в Mini App."""
    topic = body.topic.strip()
    if not topic:
        return {"ok": False, "message": "Нужна тема опроса."}
    s = get_settings()
    raw = await get_setting(session, KEY_DEFAULT_PUBLISH_TIME, s.default_publish_time)
    hh, mm = (int(x) for x in raw.split(":"))
    default_t = time(hh, mm)
    lead_raw = await get_setting(session, KEY_DRAFT_LEAD_DAYS, str(s.draft_lead_days))
    try:
        lead_days = max(0, int(lead_raw))
    except ValueError:
        lead_days = s.draft_lead_days
    d = date.fromisoformat(body.draft_date)
    t = _parse_hhmm(body.draft_time) or default_t
    task = ContentTask(
        draft_date=d,
        draft_time=t,
        publish_date=d + timedelta(days=lead_days),
        publish_time=default_t,
        topic=topic,
        task_type=TaskType.POLL.value,
        related_task_id=body.related_task_id,
        status=TaskStatus.SCHEDULED.value,
        is_active=True,
    )
    session.add(task)
    await session.commit()
    return {"ok": True, "id": task.id}
```

Примечание: `_parse_hhmm` определена ниже по файлу (строка 159) — в Python порядок определения функций модуля не важен для вызова внутри других функций того же модуля (тело `add_poll` выполнится только после полной загрузки модуля), так что переносить её не нужно.

- [ ] **Step 4: Проверить компиляцию всего модуля**

Run: `.venv/Scripts/python.exe -m py_compile app/webapp/routes.py`
Expected: без вывода.

- [ ] **Step 5: Commit**

```bash
git add app/webapp/routes.py
git commit -m "$(cat <<'EOF'
feat: task_type/related_task в API задач + эндпоинт быстрого добавления опроса

_task_dict отдаёт разобранные question/options для опроса. POST /tasks/poll
создаёт один опрос (сам по себе или привязанный к посту через related_task_id).
EOF
)"
```

---

### Task 6: Frontend Mini App — отображение опроса и точки входа

**Files:**
- Modify: `app/webapp/templates/app.html`

**Interfaces:**
- Consumes: поля `task_type`/`related_topic`/`poll_question`/`poll_options` из Task 5, эндпоинт `POST /tasks/poll` из Task 5.
- Produces: `pollPreview(t)`, `doAddPoll(relatedId?, relatedDate?)` — вызывается из карточки поста и из вкладки «Добавить».

- [ ] **Step 1: Подсказка про метку и кнопка быстрого добавления во вкладке «Добавить»**

В `app/webapp/templates/app.html` заменить блок `#tab-add` (строки 97-107):

```html
  <div id="tab-add" class="hidden">
    <label>Список постов (по одному на строку): <code>ДД-ММ-ГГГГ [ЧЧ:ММ] — тема</code></label>
    <p style="color:var(--hint);font-size:13px;margin:4px 0 0">
      Указанные дата и время — это когда бот подготовит <b>черновик</b> (задаст вопросы
      и сгенерирует пост). Дата публикации в канал вычислится автоматически
      (черновик + лид-тайм из настроек) и её можно поменять отдельно в карточке поста.
    </p>
    <textarea id="bulk" rows="8" placeholder="05-07-2026 — Как выбрать велосипед&#10;06-07-2026 10:00 — Разбор ошибок"></textarea>
    <button class="primary" id="bulk-submit">Создать посты</button>
    <div id="bulk-result" class="meta"></div>
  </div>
```

на:

```html
  <div id="tab-add" class="hidden">
    <label>Список постов (по одному на строку): <code>ДД-ММ-ГГГГ [ЧЧ:ММ] — тема</code></label>
    <p style="color:var(--hint);font-size:13px;margin:4px 0 0">
      Указанные дата и время — это когда бот подготовит <b>черновик</b> (задаст вопросы
      и сгенерирует пост). Дата публикации в канал вычислится автоматически
      (черновик + лид-тайм из настроек) и её можно поменять отдельно в карточке поста.
      Чтобы строка стала опросом, а не постом — поставьте перед темой 📊:
      <code>05-07-2026 — 📊 Какой формат вам интереснее?</code>
    </p>
    <textarea id="bulk" rows="8" placeholder="05-07-2026 — Как выбрать велосипед&#10;06-07-2026 10:00 — 📊 Какой формат вам интереснее?"></textarea>
    <button class="primary" id="bulk-submit">Создать посты</button>
    <button class="act sec" onclick="doAddPoll()" style="margin-top:6px">📊 Быстро добавить один опрос</button>
    <div id="bulk-result" class="meta"></div>
  </div>
```

- [ ] **Step 2: Функция `pollPreview` и ветвление `taskCard`**

Перед функцией `function taskCard(t) {` (строка 282) добавить:

```js
function pollPreview(t) {
  const opts = (t.poll_options || []).map((o, i) => `${i + 1}. ${esc(o)}`).join("<br>");
  return `<div class="preview">📊 <b>${esc(t.poll_question || "")}</b><br><br>${opts}</div>`;
}
```

Заменить тело `taskCard` (строки 282-312):

```js
function taskCard(t) {
  let actions = "";
  if (t.can_approve) {
    actions = `
      <button class="act" onclick="doApprove(${t.id}, this)">✅ Одобрить</button>
      <button class="act sec" onclick="doRevise(${t.id})">✏️ Правки</button>
      <button class="act sec" onclick="doAlt(${t.id}, this)">🔄 Вариант</button>
      <button class="act danger" onclick="doCancel(${t.id})">❌ Отмена</button>`;
  } else if (t.can_generate) {
    actions = `<button class="act" onclick="doGenerate(${t.id}, this)">⚙️ Задать вопросы</button>
               <button class="act sec" onclick="doEdit(${t.id})">✎ Изменить</button>
               <button class="act danger" onclick="doDelete(${t.id})">🗑</button>`;
  }
  if (t.can_publish) {
    actions += `<button class="act" onclick="doPublishNow(${t.id})">📢 Опубликовать сейчас</button>`;
  }
  const body = postText(t);
  const meta = (t.can_generate && t.draft_date)
    ? `#${t.id} · ✍️ черновик ${fmtDate(t.draft_date)} ${t.draft_time||""} → 📢 публикация ${fmtDate(t.publish_date)} ${t.publish_time||""}`
    : `#${t.id} · 📢 ${fmtDate(t.publish_date)} ${t.publish_time||""}`;
  return `<div class="card">
    <div class="top"><div><div class="topic">${esc(t.topic) || "(без темы)"}</div>
      <div class="meta">${meta}</div></div></div>
    <div class="status">${esc(t.status_label)}</div>
    ${body}
    ${questionsBlock(t)}
    ${quoteToggle(t)}
    ${mediaChips(t)}
    <div class="actions">${actions}</div>
  </div>`;
}
```

на:

```js
function taskCard(t) {
  const isPoll = t.task_type === "poll";
  let actions = "";
  if (t.can_approve) {
    actions = `
      <button class="act" onclick="doApprove(${t.id}, this)">✅ Одобрить</button>
      <button class="act sec" onclick="doRevise(${t.id})">✏️ Правки</button>
      <button class="act sec" onclick="doAlt(${t.id}, this)">🔄 Вариант</button>
      <button class="act danger" onclick="doCancel(${t.id})">❌ Отмена</button>`;
  } else if (t.can_generate) {
    actions = `<button class="act" onclick="doGenerate(${t.id}, this)">⚙️ Задать вопросы</button>
               <button class="act sec" onclick="doEdit(${t.id})">✎ Изменить</button>
               <button class="act danger" onclick="doDelete(${t.id})">🗑</button>`;
  }
  if (t.can_publish) {
    actions += `<button class="act" onclick="doPublishNow(${t.id})">📢 Опубликовать сейчас</button>`;
  }
  if (!isPoll && t.status !== "cancelled") {
    actions += `<button class="act sec" onclick="doAddPoll(${t.id}, '${t.publish_date}')">📊 Опрос к этому посту</button>`;
  }
  const body = isPoll ? pollPreview(t) : postText(t);
  const meta = (t.can_generate && t.draft_date)
    ? `#${t.id} · ✍️ черновик ${fmtDate(t.draft_date)} ${t.draft_time||""} → 📢 публикация ${fmtDate(t.publish_date)} ${t.publish_time||""}`
    : `#${t.id} · 📢 ${fmtDate(t.publish_date)} ${t.publish_time||""}`;
  const relatedNote = t.related_topic
    ? `<div class="meta">🗳 к посту «${esc(t.related_topic)}»</div>` : "";
  return `<div class="card">
    <div class="top"><div><div class="topic">${isPoll ? "📊 " : ""}${esc(t.topic) || "(без темы)"}</div>
      <div class="meta">${meta}</div>${relatedNote}</div></div>
    <div class="status">${esc(t.status_label)}</div>
    ${body}
    ${questionsBlock(t)}
    ${isPoll ? "" : quoteToggle(t)}
    ${isPoll ? "" : mediaChips(t)}
    <div class="actions">${actions}</div>
  </div>`;
}
```

- [ ] **Step 3: Функция `doAddPoll`**

После функции `doEdit` (после строки 444, в конце файла перед закрывающими тегами скриптов) добавить:

```js
function doAddPoll(relatedId, relatedDate){
  const todayIso = new Date().toISOString().slice(0,10);
  const title = relatedId ? "Опрос к посту" : "Добавить опрос";
  openModal(title, `
    <label>Тема опроса</label><input id="p-topic" placeholder="Какой формат вам интереснее?">
    <label>✍️ Дата подготовки черновика</label><input id="p-date" type="date" value="${relatedDate || todayIso}">
    <label>Время подготовки</label><input id="p-time" type="time" value="10:00">`,
    async () => {
      const topic = document.getElementById("p-topic").value.trim();
      if (!topic) return;
      const res = await api("/tasks/poll", "POST", {
        topic,
        draft_date: document.getElementById("p-date").value,
        draft_time: document.getElementById("p-time").value || "10:00",
        related_task_id: relatedId || null,
      });
      if (!res.ok) { tg.showAlert(res.message || "Не удалось добавить опрос."); return; }
      toast("Опрос добавлен ✓");
      await loadTasks();
    });
}
```

- [ ] **Step 4: Проверить, что файл — валидный HTML/JS (нет незакрытых тегов/скобок)**

Run: `.venv/Scripts/python.exe -c "
import re
html = open('app/webapp/templates/app.html', encoding='utf-8').read()
open_braces = html.count('{')
close_braces = html.count('}')
print('braces balanced' if open_braces == close_braces else f'MISMATCH {open_braces} vs {close_braces}')
assert '<script' in html and '</script>' in html
print('OK')
"`
Expected: `braces balanced` (или совпадающие числа) и `OK`. Это грубая проверка — реальная проверка UI делается вручную в Task 7 (нет headless-браузера в этой среде).

- [ ] **Step 5: Commit**

```bash
git add app/webapp/templates/app.html
git commit -m "$(cat <<'EOF'
feat: Mini App — карточка опроса, кнопка «Опрос к посту», быстрое добавление

Опрос показывается read-only превью (вопрос+варианты), без блоков цитаты и
медиа — они неприменимы. Тот же набор кнопок одобрения, что у поста.
EOF
)"
```

---

### Task 7: Ручная сквозная проверка (E2E QA)

**Files:** нет изменений кода — чек-лист проверки в реальном окружении (Docker/Postgres/Telegram недоступны в среде реализации).

**Interfaces:** нет.

- [ ] **Step 1: Применить миграцию в dev/VDS-окружении**

```bash
docker compose exec app alembic upgrade head
```
Expected: применяется `0006_poll_task`, ошибок нет.

- [ ] **Step 2: Опрос из массового добавления (текстовая метка)**

В Mini App, вкладка «Добавить», ввести:
```
<завтрашняя дата> — 📊 Какой формат контента вам интереснее?
```
Нажать «Создать посты». Expected: в «Драфты» появляется карточка с `📊` перед темой.

- [ ] **Step 3: Быстрое добавление через кнопку**

Нажать «📊 Быстро добавить один опрос», заполнить тему и дату. Expected: карточка опроса создаётся так же, как в Step 2.

- [ ] **Step 4: Генерация и валидация черновика**

Для созданного опроса нажать «⚙️ Задать вопросы» → ответить/пропустить. Expected: приходит черновик — вопрос и 2-5 вариантов, без UI цитаты/медиа под ним.

- [ ] **Step 5: Правки и альтернатива**

Нажать «🔄 Вариант» — должен прийти другой опрос на ту же тему. Нажать «✏️ Правки», ввести комментарий — черновик должен обновиться с учётом правки.

- [ ] **Step 6: Одобрение и публикация**

Нажать «✅ Одобрить», затем «📢 Опубликовать сейчас» (или дождаться расписания). Expected: в тестовом канале появляется нативный Telegram-опрос (голосование), а не текстовое сообщение.

- [ ] **Step 7: Опрос к посту (связка)**

На карточке обычного поста нажать «📊 Опрос к этому посту», заполнить тему. Expected: создаётся отдельная карточка опроса с пометкой «🗳 к посту «<тема поста>»»; одобрение/публикация поста и опроса происходят независимо друг от друга.

- [ ] **Step 8: Финальный commit (если в ходе ручной проверки не потребовалось правок кода)**

Если Steps 2-7 прошли без правок — отдельный коммит не нужен, фича считается завершённой на коммите из Task 6. Если потребовались фиксы — применить обычный цикл: найти причину, минимальный фикс, коммит с понятным сообщением.
