import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import settings
from alfacrm_client import AlfaCRMClient, AlfaCRMError
from database import Database
from bot.formatting import (
    STATUS_PLANNED,
    build_schedule,
    esc,
    fmt_date_long,
    format_reminder,
    parse_lesson_datetime,
    safe_call,
    send_blocks,
)
from bot.handlers.common import fetch_lessons, get_lesson_summary, load_customer_map
from bot.keyboards import lesson_action_keyboard

logger = logging.getLogger(__name__)


class ReminderScheduler:
    def __init__(self, db: Database, alfacrm: AlfaCRMClient, bot, cache=None):
        self.db = db
        self.alfacrm = alfacrm
        self.bot = bot
        self.cache = cache
        # Все задания живут в часовом поясе филиала, а не сервера.
        self.scheduler = AsyncIOScheduler(timezone=settings.TZ)
        self.manager_ids = settings.MANAGER_IDS

    def start(self):
        common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
        self.scheduler.add_job(
            self.send_daily_schedule, CronTrigger(hour=8, minute=0, timezone=settings.TZ),
            id="daily", **common,
        )
        self.scheduler.add_job(
            self.check_upcoming_lessons, IntervalTrigger(minutes=5), id="upcoming", **common,
        )
        self.scheduler.add_job(
            self.check_low_balance, CronTrigger(hour=10, minute=0, timezone=settings.TZ),
            id="balance", **common,
        )
        self.scheduler.add_job(
            self.send_daily_summary, CronTrigger(hour=0, minute=1, timezone=settings.TZ),
            id="daily_summary", **common,
        )
        self.scheduler.add_job(
            self.cleanup, CronTrigger(hour=3, minute=30, timezone=settings.TZ),
            id="cleanup", **common,
        )
        self.scheduler.start()
        logger.info(f"✅ Планировщик запущен (TZ={settings.TIMEZONE})")

    def stop(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        logger.info("⏹ Планировщик остановлен")

    # ==================== УТРЕННЯЯ РАССЫЛКА ====================

    async def send_daily_schedule(self):
        today = settings.today()
        today_iso = today.isoformat()

        try:
            customers = await load_customer_map(self.alfacrm)
        except Exception as e:
            logger.error(f"Не удалось загрузить карту учеников: {e}")
            customers = {}

        for role in ("teacher", "parent"):
            for user in self.db.get_all_users_by_role(role):
                try:
                    kwargs = (
                        {"teacher_id": user["crm_id"]} if role == "teacher"
                        else {"customer_id": user["crm_id"]}
                    )
                    lessons = await fetch_lessons(
                        self.alfacrm, self.cache,
                        status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso,
                        **kwargs,
                    )
                    if not lessons:
                        continue

                    blocks = build_schedule(
                        lessons,
                        role=role,
                        title=f"Расписание на сегодня, {fmt_date_long(today)}",
                        customers=customers,
                        today=today,
                    )
                    await send_blocks(self.bot, user["telegram_id"], blocks)
                except Exception as e:
                    logger.error(f"Ошибка рассылки {user['telegram_id']}: {e}")

    # ==================== НАПОМИНАНИЯ ====================

    async def check_upcoming_lessons(self):
        now = settings.now()
        today_iso = now.date().isoformat()

        try:
            lessons = await fetch_lessons(
                self.alfacrm, self.cache,
                status=STATUS_PLANNED, date_from=today_iso, date_to=today_iso,
            )
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки уроков: {e}")
            return

        for lesson in lessons:
            lesson_time = parse_lesson_datetime(lesson)
            if not lesson_time:
                continue

            minutes_left = (lesson_time - now).total_seconds() / 60
            if 55 <= minutes_left <= 65:
                await self._notify_lesson(lesson, "1 час")
            elif 12 <= minutes_left <= 18:
                await self._notify_lesson(lesson, "15 минут")

    async def _notify_lesson(self, lesson, when: str):
        lesson_id = lesson.get("id")
        if not lesson_id:
            return

        # Тип напоминания теперь один и тот же при записи и при проверке —
        # раньше писалось 'close_lesson', а проверялось 'upcoming_...',
        # поэтому дедупликация не работала и уведомления дублировались.
        reminder_type = f"upcoming_{when}"

        try:
            customers = await load_customer_map(self.alfacrm)
        except Exception:
            customers = {}

        for teacher_id in lesson.get("teacher_ids") or []:
            teacher = self.db.get_user_by_crm_id(teacher_id, "teacher")
            if not teacher:
                continue
            if self.db.was_reminder_sent(lesson_id, reminder_type, teacher["telegram_id"], hours=6):
                continue
            try:
                await safe_call(lambda: self.bot.send_message(
                    teacher["telegram_id"],
                    format_reminder(lesson, when=when, role="teacher", customers=customers),
                    parse_mode="HTML",
                    reply_markup=lesson_action_keyboard(lesson_id),
                ))
                self.db.mark_reminder_sent(lesson_id, reminder_type, teacher["telegram_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить преподавателю {teacher['telegram_id']}: {e}")

        for customer_id in lesson.get("customer_ids") or []:
            parent = self.db.get_user_by_crm_id(customer_id, "parent")
            if not parent:
                continue
            # Родителям дедупликации раньше не было вообще.
            if self.db.was_reminder_sent(lesson_id, reminder_type, parent["telegram_id"], hours=6):
                continue
            try:
                await safe_call(lambda: self.bot.send_message(
                    parent["telegram_id"],
                    format_reminder(lesson, when=when, role="parent"),
                    parse_mode="HTML",
                ))
                self.db.mark_reminder_sent(lesson_id, reminder_type, parent["telegram_id"])
            except Exception as e:
                logger.error(f"Не удалось напомнить родителю {parent['telegram_id']}: {e}")

    # ==================== ЕЖЕДНЕВНАЯ СВОДКА ====================

    async def send_daily_summary(self):
        yesterday = settings.today() - timedelta(days=1)
        yesterday_iso = yesterday.isoformat()
        period_label = f"за {fmt_date_long(yesterday)}"

        try:
            lessons = await fetch_lessons(
                self.alfacrm, self.cache, date_from=yesterday_iso, date_to=yesterday_iso
            )
        except Exception as e:
            logger.error(f"❌ Ошибка получения уроков для сводки: {e}", exc_info=True)
            return

        try:
            blocks = await get_lesson_summary(lessons, self.db, self.alfacrm, period_label)
        except Exception as e:
            logger.error(f"❌ Ошибка формирования сводки: {e}", exc_info=True)
            return

        for manager_id in self.manager_ids:
            try:
                await send_blocks(self.bot, manager_id, blocks)
            except Exception as e:
                logger.error(f"Не удалось отправить сводку менеджеру {manager_id}: {e}")

    # ==================== БАЛАНС ====================

    @staticmethod
    def _remaining_lessons(customer) -> int:
        # paid_lesson_count — оплачено, paid_count — израсходовано.
        # Раньше здесь было сложение, и «остаток» только рос.
        paid = int(customer.get("paid_lesson_count") or 0)
        used = int(customer.get("paid_count") or 0)
        return max(0, paid - used)

    async def check_low_balance(self):
        today = settings.today()
        try:
            customers = await self.alfacrm.load_all_customers(is_study=1)
        except AlfaCRMError as e:
            logger.error(f"Ошибка проверки баланса: {e}")
            return

        low_balance = [
            c for c in customers
            if self._remaining_lessons(c) <= settings.LOW_BALANCE_THRESHOLD
        ]
        if not low_balance:
            return

        for customer in low_balance:
            parent = self.db.get_user_by_crm_id(customer.get("id"), "parent")
            if not parent:
                continue
            if self.db.was_reminder_sent(
                int(customer["id"]), "low_balance", parent["telegram_id"], hours=20
            ):
                continue
            try:
                await safe_call(lambda: self.bot.send_message(
                    parent["telegram_id"],
                    f"⚠️ <b>Заканчивается абонемент</b>\n\n"
                    f"📅 <b>Дата проверки:</b> {today.strftime('%d.%m.%Y')}\n"
                    f"🎟 <b>Осталось занятий:</b> {self._remaining_lessons(customer)}\n"
                    f"💰 <b>Баланс:</b> {esc(customer.get('balance', '0'))} руб.\n"
                    f"➡️ <b>Следующее занятие:</b> {esc(customer.get('next_lesson_date') or '—')}\n\n"
                    f"Свяжитесь с менеджером для продления.",
                    parse_mode="HTML",
                ))
                self.db.mark_reminder_sent(
                    int(customer["id"]), "low_balance", parent["telegram_id"]
                )
            except Exception as e:
                logger.error(f"Не удалось уведомить родителя {parent['telegram_id']}: {e}")

        lines = [
            f"⚠️ <b>Заканчивается абонемент у {len(low_balance)} учеников</b>",
            f"📅 <b>Дата проверки:</b> {today.strftime('%d.%m.%Y')}",
            "",
        ]
        for customer in low_balance:
            lines.append(f"👤 <b>{esc(customer.get('name') or '—')}</b> (ID: {esc(customer.get('id'))})")
            lines.append(f"   🎟 Осталось: {self._remaining_lessons(customer)} занятий")
            lines.append(f"   💰 Баланс: {esc(customer.get('balance', '0'))} руб.")
            lines.append(f"   ➡️ След. занятие: {esc(customer.get('next_lesson_date') or '—')}")
            lines.append("")

        # Список может быть длиннее лимита Telegram — режем на сообщения.
        from bot.formatting import split_messages
        blocks = split_messages(["\n".join(lines)])
        for manager_id in self.manager_ids:
            try:
                await send_blocks(self.bot, manager_id, blocks)
            except Exception as e:
                logger.error(f"Не удалось уведомить менеджера {manager_id}: {e}")

    # ==================== ОБСЛУЖИВАНИЕ ====================

    async def cleanup(self):
        try:
            removed = self.db.cleanup_reminder_log(days=30)
            logger.info(f"🧹 Лог напоминаний: удалено {removed} записей")
        except Exception as e:
            logger.error(f"Не удалось почистить лог напоминаний: {e}")
