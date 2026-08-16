from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def request_phone_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться контактом", request_contact=True)],
        ],
        resize_keyboard=True,
    )


def teacher_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Моё расписание")],
            [KeyboardButton(text="📊 Отчёт по урокам")],
            [KeyboardButton(text="🚪 Выйти из профиля")],
        ],
        resize_keyboard=True,
    )


def parent_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Расписание")],
            [KeyboardButton(text="📚 Домашнее задание")],
            [KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="🔁 Заявка на перенос")],
            [KeyboardButton(text="🚪 Выйти из профиля")],
        ],
        resize_keyboard=True,
    )


def manager_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="🔁 Заявки на перенос")],
            [KeyboardButton(text="📊 Сводка за период")],
            [KeyboardButton(text="🚪 Выйти из профиля")],
        ],
        resize_keyboard=True,
    )


def lesson_action_keyboard(lesson_id: int) -> InlineKeyboardMarkup:
    # Кнопка переноса добавлена: обработчик "transfer:" в teacher.py
    # существовал, но нажать его было негде.
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отметить как проведённый", callback_data=f"close:{lesson_id}")],
            [InlineKeyboardButton(text="📝 Прикрепить ДЗ", callback_data=f"hw:{lesson_id}")],
            [InlineKeyboardButton(text="🔁 Заявка на перенос", callback_data=f"transfer:{lesson_id}")],
        ]
    )


def transfer_decision_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"transfer_ok:{request_id}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"transfer_no:{request_id}"),
            ]
        ]
    )


def confirm_logout_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, выйти", callback_data="logout:confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="logout:cancel"),
            ]
        ]
    )
