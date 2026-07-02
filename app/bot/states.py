from aiogram.fsm.state import State, StatesGroup


class TaskFlow(StatesGroup):
    """FSM сценария подготовки поста для конкретной задачи.

    В data хранится task_id и список оставшихся вопросов.
    """

    waiting_for_answers = State()   # ждём ответы на уточняющие вопросы
    collecting_media = State()      # ждём фото/видео (опционально)
    waiting_for_approval = State()  # черновик отправлен, ждём решения
    waiting_for_revision = State()  # нажали «Правки», ждём текст правок
