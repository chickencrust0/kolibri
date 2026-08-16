import asyncio
import logging
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from cache import LessonCache
from database import Database
from bot.formatting import (
    STATUS_CANCELLED,
    STATUS_CONDUCTED,
    answer_blocks,
    build_schedule,
    esc,
    format_homework_card,
    lesson_sort_key,
    format_lesson,  # noqa: F401  (используется через build_schedule)
    safe_call,
)
from bot.handlers.common import fetch_lessons
from bot.keyboards import transfer_decision_keyboard
from bot.states import ParentTransferStates

logger = logging.getLogger(__name__)
router = Router(name="parent")


def _parent(db: Database, telegram_id: int):
    user = db.get_user(telegram_id)
    return user if user and user["role"] == "parent" else None


@router.message(F.text == "📅 Расписание")
async def parent_schedule(
    message: Message, db: Database, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _parent(db, message.from_user.id)
    if not user:
        return

    today = settings.today()
    date_from = today.isoformat()
    date_to = (today + timedelta(days=7)).isoformat()

    try:
        lessons = await fetch_lessons(
            alfacrm, cache,
            customer_id=user["crm_id"], date_from=date_from, date_to=date_to,
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    lessons = [l for l in lessons if l.get("status") != STATUS_CANCELLED]

    blocks = build_schedule(
        lessons,
        role="parent",
        title="Расписание на неделю",
        empty_text="На ближайшую неделю занятий нет.",
        today=today,
    )
    await answer_blocks(message, blocks)


@router.message(F.text == "📚 Домашнее задание")
async def parent_homework(
    message: Message, db: Database, alfacrm: AlfaCRMClient, cache: LessonCache
) -> None:
    user = _parent(db, message.from_user.id)
    if not user:
        return

    today = settings.today()
    date_from = (today - timedelta(days=14)).isoformat()

    try:
        # Раньше сюда уходил запрос вообще без периода — выгружалась
        # вся история занятий ученика ради двух недель ДЗ.
        lessons = await fetch_lessons(
            alfacrm, cache,
            customer_id=user["crm_id"],
            status=STATUS_CONDUCTED,
            date_from=date_from,
            date_to=today.isoformat(),
        )
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    lessons_with_hw = [l for l in lessons if (l.get("homework") or "").strip()]
    if not lessons_with_hw:
        await message.answer("📚 Домашних заданий за последние 2 недели нет.")
        return

    await message.answer(
        f"📚 <b>Домашние задания</b> ({len(lessons_with_hw)})", parse_mode="HTML"
    )

    for lesson in sorted(lessons_with_hw, key=lesson_sort_key, reverse=True):
        files = db.get_homework_files(lesson.get("id")) if lesson.get("id") else []
        card = format_homework_card(lesson, files_count=len(files), today=today)

        await asyncio.sleep(settings.TG_SEND_DELAY)
        await safe_call(lambda c=card: message.answer(c, parse_mode="HTML"))

        for f in files:
            await asyncio.sleep(settings.TG_SEND_DELAY)
            try:
                if f.get("file_type") == "photo":
                    await safe_call(lambda fid=f["file_id"]: message.answer_photo(fid))
                else:
                    await safe_call(lambda fid=f["file_id"]: message.answer_document(fid))
            except Exception as e:
                logger.warning(f"Не удалось отправить файл ДЗ {f.get('file_id')}: {e}")


@router.message(F.text == "💰 Баланс")
async def parent_balance(message: Message, db: Database, alfacrm: AlfaCRMClient) -> None:
    user = _parent(db, message.from_user.id)
    if not user:
        return

    try:
        customer = await alfacrm.get_customer_info(user["crm_id"])
    except AlfaCRMError as e:
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
        return

    if not customer:
        await message.answer("❌ Не удалось получить данные.")
        return

    paid = int(customer.get("paid_lesson_count") or 0)   # оплачено занятий
    used = int(customer.get("paid_count") or 0)          # из них израсходовано
    remaining = max(0, paid - used)

    await message.answer(
        f"💰 <b>Абонемент</b>\n\n"
        f"💵 <b>Баланс:</b> {esc(customer.get('balance', '0'))} руб.\n"
        f"📅 <b>Оплачено занятий:</b> {paid}\n"
        f"✅ <b>Проведено:</b> {used}\n"
        f"🎟 <b>Осталось:</b> {remaining}\n"
        f"➡️ <b>Следующее занятие:</b> {esc(customer.get('next_lesson_date') or '—')}\n"
        f"⬅️ <b>Последнее посещение:</b> {esc(customer.get('last_attend_date') or '—')}",
        parse_mode="HTML",
    )


# ==================== ЗАЯВКА НА ПЕРЕНОС ====================

@router.message(F.text == "🔁 Заявка на перенос")
async def parent_transfer_start(message: Message, state: FSMContext, db: Database) -> None:
    if not _parent(db, message.from_user.id):
        return

    await state.set_state(ParentTransferStates.waiting_for_comment)
    example_date = (settings.today() + timedelta(days=1)).strftime("%d.%m.%Y")
    await message.answer(
        "🔁 <b>Заявка на перенос</b>\n\n"
        "Напишите дату, время урока и причину переноса.\n"
        "Заявка будет отправлена менеджеру.\n\n"
        f"<i>Пример: {example_date}, 15:00, хотим перенести на следующий день</i>",
        parse_mode="HTML",
    )


@router.message(ParentTransferStates.waiting_for_comment, F.text)
async def parent_transfer_send(message: Message, state: FSMContext, db: Database) -> None:
    user = _parent(db, message.from_user.id)
    if not user:
        await state.clear()
        return

    comment = message.text
    await state.clear()

    # Заявка теперь пишется в БД — раньше она уходила менеджеру в чат
    # и нигде не сохранялась, поэтому не попадала в «Заявки на перенос».
    request_id = db.create_transfer_request(
        message.from_user.id, None, comment, author_role="parent"
    )
    sent_at = settings.now().strftime("%d.%m.%Y %H:%M")

    for manager_id in settings.MANAGER_IDS:
        try:
            await safe_call(lambda mid=manager_id: message.bot.send_message(
                mid,
                f"🔁 <b>Заявка на перенос №{request_id} (от родителя)</b>\n\n"
                f"📅 <b>Получена:</b> {sent_at}\n"
                f"👤 <b>Родитель:</b> {esc(user['full_name'])}\n"
                f"🆔 <b>CRM ID:</b> {esc(user['crm_id'])}\n"
                f"📞 <b>Телефон:</b> {esc(user['phone'])}\n"
                f"💬 <b>Комментарий:</b> {esc(comment)}",
                parse_mode="HTML",
                reply_markup=transfer_decision_keyboard(request_id),
            ))
        except Exception as e:
            logger.warning(f"Не удалось уведомить менеджера {manager_id}: {e}")

    await message.answer("✅ Заявка отправлена менеджеру.")
