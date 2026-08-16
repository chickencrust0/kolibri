import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.formatting import esc
from bot.keyboards import (
    confirm_logout_keyboard,
    manager_menu_keyboard,
    parent_menu_keyboard,
    request_phone_keyboard,
    teacher_menu_keyboard,
)

logger = logging.getLogger(__name__)
router = Router(name="start")

ROLE_LABELS = {
    "teacher": "👨‍🏫 Преподаватель",
    "parent": "👨‍👩‍👧 Родитель",
    "manager": "👑 Менеджер",
}


def get_menu_by_role(role: str):
    if role == "teacher":
        return teacher_menu_keyboard()
    if role == "parent":
        return parent_menu_keyboard()
    return manager_menu_keyboard()


def _login_manager(db: Database, message: Message, phone: str = "") -> None:
    db.link_user(
        telegram_id=message.from_user.id,
        crm_id=0,
        role="manager",
        phone=phone,
        full_name=message.from_user.full_name or "Менеджер",
    )


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    user = db.get_user(message.from_user.id)

    if not user and message.from_user.id in settings.MANAGER_IDS:
        # Менеджеру телефон не нужен — он опознаётся по Telegram ID.
        _login_manager(db, message)
        user = db.get_user(message.from_user.id)

    if user:
        role = user["role"]
        crm_line = (
            f"🆔 CRM ID: <code>{user['crm_id']}</code>\n" if user["crm_id"] else ""
        )
        await message.answer(
            f"👋 С возвращением, <b>{esc(user['full_name'])}</b>!\n\n"
            f"📊 Роль: {ROLE_LABELS.get(role, esc(role))}\n"
            f"{crm_line}\n"
            f"👇 Выберите раздел:",
            reply_markup=get_menu_by_role(role),
            parse_mode="HTML",
        )
        return

    await message.answer(
        "👋 <b>Добро пожаловать!</b>\n\n"
        "Для входа поделитесь номером телефона.\n\n"
        "📱 Нажмите кнопку ниже или напишите номер вручную\n"
        "<i>Формат: +7(XXX)XXX-XX-XX или 8XXXXXXXXXX</i>",
        reply_markup=request_phone_keyboard(),
        parse_mode="HTML",
    )


@router.message(F.contact)
async def handle_contact(
    message: Message, db: Database, alfacrm: AlfaCRMClient, state: FSMContext
) -> None:
    if message.contact.user_id and message.contact.user_id != message.from_user.id:
        # Чужой контакт: иначе можно войти под любым номером из адресной книги.
        await message.answer(
            "❌ Поделитесь, пожалуйста, <b>своим</b> контактом.",
            parse_mode="HTML",
            reply_markup=request_phone_keyboard(),
        )
        return
    await state.clear()
    await process_phone_login(message, db, alfacrm, message.contact.phone_number)


@router.message(F.text.regexp(r"^\+?\d[\d\-\(\)\s]{5,}$"))
async def handle_manual_phone(
    message: Message, db: Database, alfacrm: AlfaCRMClient, state: FSMContext
) -> None:
    if db.get_user(message.from_user.id):
        # Уже авторизован — пропускаем, пусть обработают другие роутеры.
        return
    await state.clear()
    await process_phone_login(message, db, alfacrm, message.text.strip())


async def process_phone_login(
    message: Message, db: Database, alfacrm: AlfaCRMClient, phone: str
) -> None:
    telegram_id = message.from_user.id
    logger.info(f"Вход по телефону для {telegram_id}")

    if telegram_id in settings.MANAGER_IDS:
        _login_manager(db, message, phone)
        await message.answer("✅ Вы вошли как менеджер.", reply_markup=manager_menu_keyboard())
        return

    try:
        teacher = await alfacrm.find_teacher_by_phone(phone)
    except AlfaCRMError as e:
        logger.warning(f"Поиск преподавателя не удался: {e}")
        teacher = None

    if teacher:
        name = alfacrm.extract_user_name(teacher)
        db.link_user(
            telegram_id=telegram_id, crm_id=teacher["id"], role="teacher",
            phone=phone, full_name=name,
        )
        await message.answer(
            f"✅ Добро пожаловать, <b>{esc(name)}</b>! (Преподаватель)",
            parse_mode="HTML",
            reply_markup=teacher_menu_keyboard(),
        )
        return

    try:
        customer = await alfacrm.find_customer_by_phone(phone)
    except AlfaCRMError as e:
        logger.warning(f"Поиск клиента не удался: {e}")
        customer = None

    if customer:
        name = alfacrm.extract_user_name(customer)
        db.link_user(
            telegram_id=telegram_id, crm_id=customer["id"], role="parent",
            phone=phone, full_name=name,
        )
        await message.answer(
            f"✅ Добро пожаловать, <b>{esc(name)}</b>!",
            parse_mode="HTML",
            reply_markup=parent_menu_keyboard(),
        )
        return

    await message.answer(
        "Номер не найден. Попробуйте ещё раз или обратитесь к менеджеру.",
        reply_markup=request_phone_keyboard(),
    )


@router.message(F.text == "🚪 Выйти из профиля")
async def logout_start(message: Message) -> None:
    await message.answer("⚠️ Вы уверены, что хотите выйти?", reply_markup=confirm_logout_keyboard())


@router.callback_query(F.data.startswith("logout:"))
async def logout_process(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    action = callback.data.split(":")[1]
    if action == "confirm":
        await state.clear()
        db.deactivate_user(callback.from_user.id)
        await callback.message.edit_text("👋 Вы вышли.")
        await callback.message.answer(
            "Для входа поделитесь номером телефона:", reply_markup=request_phone_keyboard()
        )
    else:
        await callback.message.edit_text("❌ Отменено.")
    await callback.answer()
