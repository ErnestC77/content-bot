from aiogram.fsm.state import State, StatesGroup


class AddPosts(StatesGroup):
    """Массовое добавление задач списком строк «дата [время] — тема»."""

    waiting_list = State()


class Revision(StatesGroup):
    """Владелец нажал «Правки» — ждём текст корректировок для конкретной задачи."""

    waiting_text = State()


class AddMedia(StatesGroup):
    """Добавление медиа к выбранной задаче."""

    choosing_task = State()
    receiving = State()
