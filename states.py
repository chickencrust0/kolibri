from aiogram.fsm.state import State, StatesGroup


class HomeworkStates(StatesGroup):
    waiting_for_text_or_file = State()


class TeacherTransferStates(StatesGroup):
    """Перенос конкретного урока: в данных состояния всегда есть lesson_id."""
    waiting_for_comment = State()


class ParentTransferStates(StatesGroup):
    """
    Отдельная группа состояний намеренно.

    Раньше родитель и преподаватель делили TransferStates.waiting_for_comment,
    а teacher.router подключался раньше parent.router — поэтому ответ родителя
    попадал в обработчик преподавателя и падал на data["lesson_id"].
    """
    waiting_for_comment = State()


class BroadcastStates(StatesGroup):
    waiting_for_content = State()
    waiting_for_recipient = State()


class DateRangeStates(StatesGroup):
    """Произвольный период расписания преподавателя."""
    waiting_for_date_from = State()
    waiting_for_date_to = State()


class ManagerSummaryStates(StatesGroup):
    waiting_for_date_from = State()
    waiting_for_date_to = State()
