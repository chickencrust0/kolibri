import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from cache import LessonCache
from database import Database
from bot.formatting import (
    STATUS_CANCELLED,
    STATUS_CONDUCTED,
    STATUS_PLANNED,
    answer_blocks,
    build_schedule,
    day_header,
    esc,
    format_lesson,
    group_by_day,
    safe_call,
)
from bot.handlers.common import fetch_lessons, get_lesson_snapshot, load_customer_map
from bot.keyboards import lesson_action_keyboard, transfer_decision_keyboard
from bot.states import DateRangeStates, HomeworkStates, TeacherTransferStates

logger = logging.getLogger(__name__)
router = Router(name="teacher")


def _teacher(db: Database, telegram_id: int):
    user = db.get_user(telegram_id)
    return user if user and user["role"] == "teacher" else None


def _parse_iso(text: str):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


# ==================== ВЫБОР ПЕРИОДА ====================

@router.message(F.text == "📅 Моё расписание")
async def teacher_schedule_menu(message: Message, db: Database, state: FSMContext) -> None:
    if not _teacher(db, message.from_user.id):
        return
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня", callback_data="schedule:today")],
        [InlineKeyboardButton(text="📅 Завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="📅 Неделя", callback_data="schedule:week")],
        [InlineKeyboardButton(text="📅 Месяц", callback_data="schedule:month")],
        [InlineKeyboardButton(text="📅 Свой период", callback_data="schedule:custom")],
    ])
    await message.answer("📅 <b>Выберите период:</b>", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "schedule:custom")
async def custom_date_from(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Только для преподавателей.", show_alert=True)
        return
    await state.set_state(DateRangeStates.waiting_for_date_from)
    await callback.message.edit_text(
        "📅 Введите начальную дату в формате <b>ГГГГ-ММ-ДД</b>\n"
        f"Пример: <code>{settings.today().isoformat()}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(DateRangeStates.waiting_for_date_from, F.text)
async def custom_date_to(message: Message, state: FSMContext) -> None:
    if not _parse_iso(message.text):
        await message.answer("❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")
        return
    await state.update_data(date_from=message.text.strip())
    await state.set_state(DateRangeStates.waiting_for_date_to)
    await message.answer("📅 Введите конечную дату в формате <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")


@router.message(DateRangeStates.waiting_for_date_to, F.text)
async def show_custom_schedule(
    message: Message,
    state: FSMContext,
    db: Database,
    alfacrm: AlfaCRMClient,
    cache: LessonCache,
) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    if not _parse_iso(message.text):
        await message.answer("❌ Неверный формат. Введите дату как <b>ГГГГ-ММ-ДД</b>", parse_mode="HTML")
        return

    data = await state.get_data()
    date_from = data.get("date_from")
    date_to = message.text.strip()
    await state.clear()

    if not date_from:
        await message.answer("❌ Начальная дата потерялась, начните заново.")
        return
    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await show_schedule(message, alfacrm, cache, user, date_from, date_to)


@router.callback_query(F.data.startswith("schedule:"))
async def handle_schedule_period(
    callback: CallbackQuery, db: Database, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _teacher(db, callback.from_user.id)
    if not user:
        await callback.answer("❌ Только для преподавателей.", show_alert=True)
        return

    period = callback.data.split(":")[1]
    today = settings.today()

    if period == "today":
        date_from = date_to = today.isoformat()
    elif period == "tomorrow":
        date_from = date_to = (today + timedelta(days=1)).isoformat()
    elif period == "week":
        date_from, date_to = today.isoformat(), (today + timedelta(days=7)).isoformat()
    elif period == "month":
        date_from, date_to = today.isoformat(), (today + timedelta(days=30)).isoformat()
    else:
        await callback.answer()
        return

    await callback.answer()
    await show_schedule(callback.message, alfacrm, cache, user, date_from, date_to)


async def show_schedule(
    message: Message,
    alfacrm: AlfaCRMClient,
    cache: LessonCache,
    user,
    date_from: str,
    date_to: str,
) -> None:
    """
    Короткий период — карточки с кнопками действий.
    Длинный — сгруппированный текст: сотня сообщений подряд упирается
    во флуд-лимит Telegram и половина просто не доходит.
    """
    try:
        lessons = await fetch_lessons(
            alfacrm, cache,
            teacher_id=user["crm_id"], date_from=date_from, date_to=date_to,
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка получения расписания: {esc(str(e))}", parse_mode="HTML")
        return

    if not lessons:
        hint = "" if cache.is_ready() else "\n\n<i>Кеш ещё обновляется, попробуйте через минуту.</i>"
        await message.answer(
            f"📅 Нет уроков в период с <b>{esc(date_from)}</b> по <b>{esc(date_to)}</b>{hint}",
            parse_mode="HTML",
        )
        return

    customers = await load_customer_map(alfacrm)
    today = settings.today()

    if len(lessons) > settings.MAX_LESSON_CARDS:
        blocks = build_schedule(
            lessons,
            role="teacher",
            title=f"Расписание {date_from} – {date_to}",
            customers=customers,
            today=today,
            note="Кнопки действий доступны при выборе более короткого периода.",
        )
        await answer_blocks(message, blocks)
        return

    await message.answer(
        f"📅 <b>Расписание</b>\n"
        f"Период: {esc(date_from)} – {esc(date_to)}\n"
        f"Всего уроков: <b>{len(lessons)}</b>",
        parse_mode="HTML",
    )

    for day, day_lessons in group_by_day(lessons):
        await asyncio.sleep(settings.TG_SEND_DELAY)
        await safe_call(lambda d=day: message.answer(day_header(d, today), parse_mode="HTML"))
        for lesson in day_lessons:
            card = format_lesson(lesson, role="teacher", customers=customers)
            keyboard = (
                lesson_action_keyboard(lesson["id"])
                if lesson.get("status") == STATUS_PLANNED and lesson.get("id")
                else None
            )
            await asyncio.sleep(settings.TG_SEND_DELAY)
            await safe_call(
                lambda c=card, k=keyboard: message.answer(c, parse_mode="HTML", reply_markup=k)
            )


# ==================== ОТЧЁТ ====================

@router.message(F.text == "📊 Отчёт по урокам")
async def teacher_report(
    message: Message, db: Database, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        return

    today = settings.today()
    date_from = (today - timedelta(days=30)).isoformat()

    try:
        lessons = await fetch_lessons(
            alfacrm, cache,
            teacher_id=user["crm_id"], date_from=date_from, date_to=today.isoformat(),
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    total = len(lessons)
    conducted = sum(1 for l in lessons if l.get("status") == STATUS_CONDUCTED)
    cancelled = sum(1 for l in lessons if l.get("status") == STATUS_CANCELLED)
    not_closed = total - conducted - cancelled
    with_hw = sum(1 for l in lessons if (l.get("homework") or "").strip())

    await message.answer(
        f"📊 <b>Отчёт за 30 дней</b>\n"
        f"Период: {date_from} – {today.isoformat()}\n\n"
        f"📅 <b>Всего:</b> {total}\n"
        f"✅ <b>Проведено:</b> {conducted}\n"
        f"❌ <b>Отменено:</b> {cancelled}\n"
        f"⚠️ <b>Не закрыто:</b> {not_closed}\n\n"
        f"📚 <b>С ДЗ:</b> {with_hw}\n"
        f"📝 <b>Без ДЗ:</b> {max(0, conducted - with_hw)}\n\n"
        f"{'⚠️ Есть незакрытые уроки!' if not_closed > 0 else '✅ Все уроки закрыты!'}",
        parse_mode="HTML",
    )


# ==================== ЗАКРЫТИЕ УРОКА ====================

@router.callback_query(F.data.startswith("close:"))
async def close_lesson(
    callback: CallbackQuery, db: Database, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.", show_alert=True)
        return

    lesson_id = int(callback.data.split(":")[1])
    try:
        # Передаём текущую модель урока: частичный апдейт
        # {"status": 3} затирал в CRM время и состав участников.
        current = await get_lesson_snapshot(lesson_id, alfacrm, cache)
        await alfacrm.mark_lesson_conducted(lesson_id, current=current)
        await cache.patch_lesson(lesson_id, {"status": STATUS_CONDUCTED})
        db.mark_reminder_sent(lesson_id, "closed", callback.from_user.id)

        original = callback.message.html_text or callback.message.text or ""
        await callback.message.edit_text(
            f"{original}\n\n✅ <b>Отмечен как проведённый</b>", parse_mode="HTML"
        )
        await callback.answer("✅ Урок проведён!")
    except AlfaCRMError as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)


# ==================== ДОМАШНЕЕ ЗАДАНИЕ ====================

@router.callback_query(F.data.startswith("hw:"))
async def attach_hw_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.", show_alert=True)
        return

    lesson_id = int(callback.data.split(":")[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(HomeworkStates.waiting_for_text_or_file)
    await callback.message.answer("📝 Отправьте текст или файл ДЗ.")
    await callback.answer()


@router.message(HomeworkStates.waiting_for_text_or_file, F.text)
async def attach_hw_text(
    message: Message, state: FSMContext, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        await state.clear()
        await message.answer("❌ Урок потерялся, откройте расписание заново.")
        return

    try:
        current = await get_lesson_snapshot(lesson_id, alfacrm, cache)
        await alfacrm.set_homework(lesson_id, message.text, current=current)
        await cache.patch_lesson(lesson_id, {"homework": message.text})
        await message.answer("✅ ДЗ сохранено!")
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
    await state.clear()


@router.message(HomeworkStates.waiting_for_text_or_file, F.document | F.photo)
async def attach_hw_file(
    message: Message, state: FSMContext, db: Database, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    if not lesson_id:
        await state.clear()
        await message.answer("❌ Урок потерялся, откройте расписание заново.")
        return

    if message.document:
        file_id = message.document.file_id
        file_name = message.document.file_name or "файл"
        file_type = "document"
    else:
        file_id = message.photo[-1].file_id
        file_name = "фото"
        file_type = "photo"

    db.add_homework_file(lesson_id, file_id, file_name, file_type)

    try:
        current = await get_lesson_snapshot(lesson_id, alfacrm, cache)
        existing_hw = ((current or {}).get("homework") or "").strip()
        note = f"[📎 {file_name}]"
        new_hw = f"{existing_hw}\n{note}" if existing_hw else note
        await alfacrm.set_homework(lesson_id, new_hw, current=current)
        await cache.patch_lesson(lesson_id, {"homework": new_hw})
        await message.answer("✅ Файл прикреплён!")
    except AlfaCRMError as e:
        await message.answer(
            f"⚠️ Файл сохранён локально. Ошибка CRM: {esc(str(e))}", parse_mode="HTML"
        )

    await state.clear()


# ==================== ЗАЯВКА НА ПЕРЕНОС ====================

@router.callback_query(F.data.startswith("transfer:"))
async def transfer_start(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not _teacher(db, callback.from_user.id):
        await callback.answer("❌ Доступно только преподавателям.", show_alert=True)
        return

    lesson_id = int(callback.data.split(":")[1])
    await state.update_data(lesson_id=lesson_id)
    await state.set_state(TeacherTransferStates.waiting_for_comment)
    await callback.message.answer("🔁 Напишите желаемую дату/время и причину переноса.")
    await callback.answer()


@router.message(TeacherTransferStates.waiting_for_comment, F.text)
async def transfer_finish(message: Message, state: FSMContext, db: Database) -> None:
    user = _teacher(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    data = await state.get_data()
    lesson_id = data.get("lesson_id")
    await state.clear()

    request_id = db.create_transfer_request(
        message.from_user.id, lesson_id, message.text, author_role="teacher"
    )
    sent_at = settings.now().strftime("%d.%m.%Y %H:%M")

    for manager_id in settings.MANAGER_IDS:
        try:
            await safe_call(lambda mid=manager_id: message.bot.send_message(
                mid,
                f"🔁 <b>Заявка на перенос №{request_id}</b>\n\n"
                f"📅 <b>Получена:</b> {sent_at}\n"
                f"👨‍🏫 <b>Преподаватель:</b> {esc(user['full_name'])}\n"
                f"🆔 <b>Урок ID:</b> {esc(lesson_id or '—')}\n"
                f"💬 <b>Комментарий:</b> {esc(message.text)}",
                parse_mode="HTML",
                reply_markup=transfer_decision_keyboard(request_id),
            ))
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id}: {e}")

    await message.answer("✅ Заявка отправлена менеджеру.")
