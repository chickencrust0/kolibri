import asyncio
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import settings
from alfacrm_client import AlfaCRMClient
from cache import LessonCache
from database import Database
from bot.formatting import answer_blocks, esc, fmt_date_long, fmt_db_time, safe_call
from bot.handlers.common import fetch_lessons, get_lesson_summary
from bot.keyboards import transfer_decision_keyboard
from bot.states import BroadcastStates, ManagerSummaryStates

logger = logging.getLogger(__name__)
router = Router(name="manager")


def _is_manager(telegram_id: int) -> bool:
    return telegram_id in settings.MANAGER_IDS


def _parse_date(text: str):
    """Принимает ГГГГ-ММ-ДД и ДД.ММ.ГГГГ, возвращает date или None."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _recipients_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Преподавателям", callback_data="broadcast:teacher")],
        [InlineKeyboardButton(text="Родителям", callback_data="broadcast:parent")],
        [InlineKeyboardButton(text="Всем", callback_data="broadcast:all")],
        [InlineKeyboardButton(text="Отмена", callback_data="broadcast:cancel")],
    ])


# ==================== РАССЫЛКА ====================

@router.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext) -> None:
    if not _is_manager(message.from_user.id):
        return
    await state.set_state(BroadcastStates.waiting_for_content)
    await message.answer(
        "📢 Отправьте текст рассылки или фото (можно с подписью).\n"
        "Если нужно только фото — отправьте его без текста."
    )


@router.message(BroadcastStates.waiting_for_content, F.text)
async def broadcast_content_text(message: Message, state: FSMContext) -> None:
    await state.update_data(broadcast_text=message.text, broadcast_photo=None)
    await _ask_recipients(message, state)


@router.message(BroadcastStates.waiting_for_content, F.photo)
async def broadcast_content_photo(message: Message, state: FSMContext) -> None:
    await state.update_data(
        broadcast_text=message.caption or "",
        broadcast_photo=message.photo[-1].file_id,
    )
    await _ask_recipients(message, state)


async def _ask_recipients(message: Message, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_for_recipient)
    await message.answer("Кому отправить?", reply_markup=_recipients_keyboard())


@router.message(BroadcastStates.waiting_for_recipient)
async def broadcast_recipient_ignore(message: Message) -> None:
    await message.answer(
        "Пожалуйста, выберите получателей с помощью кнопок ниже.",
        reply_markup=_recipients_keyboard(),
    )


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_execute(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.", show_alert=True)
        return

    action = callback.data.split(":")[1]
    if action == "cancel":
        await state.clear()
        await callback.message.edit_text("❌ Отменено.")
        await callback.answer()
        return

    data = await state.get_data()
    await state.clear()

    text = data.get("broadcast_text") or ""
    photo_file_id = data.get("broadcast_photo")
    if not text and not photo_file_id:
        await callback.message.edit_text("❌ Нечего отправлять — начните заново.")
        await callback.answer()
        return

    if action == "teacher":
        recipients = db.get_all_users_by_role("teacher")
    elif action == "parent":
        recipients = db.get_all_users_by_role("parent")
    else:
        recipients = db.get_all_users_by_role("teacher") + db.get_all_users_by_role("parent")

    await callback.message.edit_text(f"📤 Отправляю… ({len(recipients)} получателей)")
    await callback.answer()

    success = failed = 0
    for i, user in enumerate(recipients):
        if i:
            # Без паузы рассылка на сотню человек упирается в лимит
            # ~30 сообщений/сек и часть просто теряется.
            await asyncio.sleep(settings.TG_BROADCAST_DELAY)
        try:
            if photo_file_id:
                await safe_call(lambda u=user: callback.bot.send_photo(
                    chat_id=u["telegram_id"], photo=photo_file_id, caption=text
                ))
            else:
                await safe_call(lambda u=user: callback.bot.send_message(u["telegram_id"], text))
            success += 1
        except Exception as e:
            failed += 1
            logger.warning(f"Рассылка не дошла до {user['telegram_id']}: {e}")

    await callback.message.edit_text(
        f"✅ Отправлено: {success}/{len(recipients)}"
        + (f"\n⚠️ Не доставлено: {failed}" if failed else "")
    )


# ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

@router.message(F.text == "🔁 Заявки на перенос")
async def transfer_list(message: Message, db: Database) -> None:
    if not _is_manager(message.from_user.id):
        return

    requests = db.get_pending_transfer_requests()
    if not requests:
        await message.answer("📭 Заявок нет.")
        return

    role_label = {"teacher": "👨‍🏫 Преподаватель", "parent": "👤 Родитель"}

    for i, req in enumerate(requests):
        if i:
            await asyncio.sleep(settings.TG_SEND_DELAY)
        author_role = req.get("author_role") or "teacher"
        lesson_id = req.get("lesson_id") or "—"
        await safe_call(lambda r=req, al=author_role, lid=lesson_id: message.answer(
            f"🔁 <b>Заявка №{r['id']}</b>\n\n"
            f"📅 <b>Создана:</b> {esc(fmt_db_time(r.get('created_at')))}\n"
            f"{role_label.get(al, '👤 Автор')}: {esc(r.get('author_name') or '—')}\n"
            f"📞 <b>Телефон:</b> {esc(r.get('author_phone') or '—')}\n"
            f"🆔 <b>Урок ID:</b> {esc(lid)}\n"
            f"💬 <b>Комментарий:</b> {esc(r.get('comment') or '—')}",
            parse_mode="HTML",
            reply_markup=transfer_decision_keyboard(r["id"]),
        ))


async def _resolve_transfer(
    callback: CallbackQuery, db: Database, status: str, label: str
) -> None:
    if not _is_manager(callback.from_user.id):
        await callback.answer("❌ Недоступно.", show_alert=True)
        return

    request_id = int(callback.data.split(":")[1])
    request = db.get_transfer_request(request_id)
    if not request:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return

    # resolve_* возвращает False, если заявку уже закрыл другой менеджер.
    if not db.resolve_transfer_request(request_id, status, callback.from_user.id):
        await callback.message.edit_text(f"ℹ️ Заявка №{request_id} уже обработана.")
        await callback.answer()
        return

    try:
        await callback.bot.send_message(
            request["teacher_telegram_id"], f"{label} Заявка №{request_id}."
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить автора заявки {request_id}: {e}")

    await callback.message.edit_text(f"{label} Заявка №{request_id}.")
    await callback.answer()


@router.callback_query(F.data.startswith("transfer_ok:"))
async def transfer_approve(callback: CallbackQuery, db: Database) -> None:
    await _resolve_transfer(callback, db, "approved", "✅ Одобрено:")


@router.callback_query(F.data.startswith("transfer_no:"))
async def transfer_reject(callback: CallbackQuery, db: Database) -> None:
    await _resolve_transfer(callback, db, "rejected", "❌ Отклонено:")


# ==================== СВОДКА ЗА ПЕРИОД ====================

@router.message(F.text == "📊 Сводка за период")
async def summary_period_start(message: Message, state: FSMContext) -> None:
    if not _is_manager(message.from_user.id):
        return
    await state.set_state(ManagerSummaryStates.waiting_for_date_from)
    today = settings.today()
    await message.answer(
        "📅 Введите <b>начальную</b> дату.\n"
        f"Формат: <code>{today.isoformat()}</code> или <code>{today.strftime('%d.%m.%Y')}</code>",
        parse_mode="HTML",
    )


@router.message(ManagerSummaryStates.waiting_for_date_from, F.text)
async def summary_date_from(message: Message, state: FSMContext) -> None:
    parsed = _parse_date(message.text)
    if not parsed:
        await message.answer(
            "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
            parse_mode="HTML",
        )
        return

    await state.update_data(date_from=parsed.isoformat())
    await state.set_state(ManagerSummaryStates.waiting_for_date_to)
    await message.answer(
        "📅 Теперь <b>конечную</b> дату.\n"
        "<i>Отправьте «-», чтобы взять тот же день.</i>",
        parse_mode="HTML",
    )


@router.message(ManagerSummaryStates.waiting_for_date_to, F.text)
async def summary_date_to(
    message: Message,
    state: FSMContext,
    db: Database,
    alfacrm: AlfaCRMClient,
    cache: LessonCache,
) -> None:
    if not _is_manager(message.from_user.id):
        await state.clear()
        return

    data = await state.get_data()
    date_from = data.get("date_from")
    if not date_from:
        await state.clear()
        await message.answer("❌ Начальная дата потерялась, начните заново.")
        return

    if message.text.strip() in ("-", "—"):
        date_to = date_from
    else:
        parsed = _parse_date(message.text)
        if not parsed:
            await message.answer(
                "❌ Не понял дату. Введите как <b>ГГГГ-ММ-ДД</b> или <b>ДД.ММ.ГГГГ</b>",
                parse_mode="HTML",
            )
            return
        date_to = parsed.isoformat()

    await state.clear()

    if date_to < date_from:
        date_from, date_to = date_to, date_from

    await message.answer("🔍 Собираю сводку…")
    logger.info(f"📅 Сводка за {date_from} – {date_to} (branch_id={settings.BRANCH_ID})")

    try:
        lessons = await fetch_lessons(alfacrm, cache, date_from=date_from, date_to=date_to)
        logger.info(f"📊 Уроков в периоде: {len(lessons)}")
    except Exception as e:
        logger.error(f"❌ Ошибка получения уроков: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка получения уроков: {esc(str(e))}", parse_mode="HTML")
        return

    if date_from == date_to:
        day = datetime.strptime(date_from, "%Y-%m-%d").date()
        period_label = f"за {fmt_date_long(day)}"
    else:
        period_label = f"за период {date_from} – {date_to}"

    try:
        blocks = await get_lesson_summary(lessons, db, alfacrm, period_label)
        await answer_blocks(message, blocks)
        logger.info(f"✅ Сводка отправлена ({len(blocks)} сообщений)")
    except Exception as e:
        logger.error(f"❌ Ошибка формирования/отправки сводки: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка: {esc(str(e))}", parse_mode="HTML")
