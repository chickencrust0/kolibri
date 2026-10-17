"""Диагностический прогон реальных хендлеров без сети (Telegram/AlfaCRM — заглушки).

Запуск: python _probe_flows.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BOT_TOKEN", "123456:AAFakeTokenForLocalDiagnostics")
os.environ.setdefault("ADMIN_TELEGRAM_IDS", "999")

import settings
from database import Database
from bot.dispatcher import Dispatcher
from bot.notifier import ManagerNotifier
from freezes import FreezeCounter
from bot.handlers import lead, manager, parent, start, support, teacher

ADMIN = 999
PARENT = 111
TEACHER = 222


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self._messages = {}
        self._callbacks = {}
        self.crash = None

    def _mid(self, user_id):
        return f"{user_id}:{len(self.sent) + 1}"

    async def send_message(self, user_id=None, text="", attachments=None, fmt=None, **kw):
        if self.crash and self.crash[0] == "send":
            raise self.crash[1]
        mid = self._mid(user_id)
        self.sent.append({"to": user_id, "text": text, "kb": attachments, "fmt": fmt})
        self._messages[mid] = user_id
        return {"message": {"body": {"mid": mid}}}

    async def edit_message(self, message_id, text, attachments=None, fmt=None, **kw):
        if self.crash and self.crash[0] == "edit":
            raise self.crash[1]
        self.edits.append({"mid": message_id, "text": text, "kb": attachments})
        return {}

    async def answer_callback(self, callback_id, notification=None):
        self.answers.append({"cb": callback_id, "note": notification})

    async def get_me(self):
        return {"name": "probe"}

    async def close(self):
        pass

    def last_texts(self, n=5):
        return [m["text"] for m in self.sent[-n:]]

    def reset(self):
        self.sent.clear()
        self.edits.clear()
        self.answers.clear()


class FakeCRM:
    """Минимальная заглушка AlfaCRM: отдаёт по одной карточке ученика/педагога."""

    def __init__(self):
        self.calls = []
        self.customer = {
            "id": 501,
            "name": "Мама Ученика",
            "phone": "+79001234567",
            "paid_lesson_count": 8,
            "paid_count": 3,
            "balance": 0,
            "paid_till": "2026-12-31",
            "custom_free_freezes": 2,
        }
        self.tariffs = [
            {"id": 1, "tariff_name": "Шахматы", "lesson_count": 8,
             "lesson_left": 5, "is_deleted": 0, "e_date": "2026-12-31"},
        ]
        self.lessons = [
            {"id": 77, "date": "2026-10-17", "time_from": "2026-10-17 16:00:00",
             "time_to": "2026-10-17 17:00:00", "teacher_ids": [601],
             "customer_ids": [501], "status": 1, "topic": "Рокировка",
             "homework": "", "group_ids": [], "_raw": {"id": 77, "details": []}},
        ]

    async def get_customer_info(self, customer_id):
        self.calls.append(("get_customer_info", customer_id))
        info = dict(self.customer)
        info["lessons_left"] = 5
        info["subscriptions"] = await self.get_subscriptions(customer_id)
        return info

    async def get_subscriptions(self, client_id):
        return [{"id": t["id"], "entity": "customer-tariff",
                 "name": t["tariff_name"], "left": t["lesson_left"],
                 "total": t["lesson_count"], "used": 3, "frozen": False,
                 "end_date": t["e_date"], "days_left": None}
                for t in self.tariffs]

    async def get_crm_freezes(self, customer_id):
        return 2

    async def load_all_teachers(self):
        return [{"id": 601, "name": "Педагог"}]

    async def load_all_customers(self, is_study=None):
        return [self.customer]

    async def load_all_clients(self):
        return [self.customer]

    async def get_lessons(self, **kw):
        self.calls.append(("get_lessons", kw))
        return list(self.lessons)

    async def get_lesson(self, lesson_id):
        return next((l for l in self.lessons if str(l["id"]) == str(lesson_id)), None)

    async def get_attendance(self, lesson_id):
        return {}

    async def find_teacher_by_phone(self, phone):
        digits = "".join(c for c in phone if c.isdigit())
        return {"id": 601, "name": "Педагог", "phone": phone} if digits.endswith("4433") else None

    async def find_customer_by_phone(self, phone):
        return dict(self.customer) if "900" in phone else None

    def extract_user_name(self, rec):
        return rec.get("name") or "Без имени"

    def extract_user_phone(self, rec):
        return rec.get("phone") or "Нет телефона"

    async def mark_absent(self, *a, **kw):
        return True

    async def mark_present(self, *a, **kw):
        return True

    async def clear_attendance(self, *a, **kw):
        return True

    async def update_lesson(self, *a, **kw):
        return {}

    async def set_crm_freezes(self, *a, **kw):
        return True

    async def close(self):
        pass


def msg_update(user_id, text, mid="1"):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": user_id, "first_name": "Тест", "last_name": ""},
            "body": {"mid": f"{user_id}:{mid}", "text": text, "attachments": []},
        },
    }


def cb_update(user_id, payload, mid="900"):
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": f"cb{mid}",
            "payload": payload,
            "user": {"user_id": user_id, "first_name": "Тест", "last_name": ""},
        },
        "message": {"body": {"mid": f"{user_id}:{mid}", "text": "меню"}},
    }


async def main():
    tmp = tempfile.mkdtemp(prefix="kolibri_probe_")
    db = Database(os.path.join(tmp, "probe.db"))
    bot = FakeBot()
    crm = FakeCRM()
    notifier = ManagerNotifier(bot, db)
    dp = Dispatcher(bot, db=db, crm=crm, cache=None, notifier=notifier, freezes=FreezeCounter(crm, db))
    for router in (start.router, support.router, lead.router, teacher.router,
                   parent.router, manager.router):
        dp.include_router(router)

    print("=" * 72)
    print("1. РОДИТЕЛЬ: /start -> телефон -> меню")
    print("=" * 72)
    await dp.feed_update(msg_update(PARENT, "/start"))
    print("после /start:", bot.last_texts(1))
    await dp.feed_update(msg_update(PARENT, "+79001234567", mid="2"))
    print("после телефона:", bot.last_texts(2))
    print("роль в БД:", (db.get_user(PARENT) or {}).get("role"))

    print()
    print("=" * 72)
    print("2. РОДИТЕЛЬ: свой период (pschedule:custom)")
    print("=" * 72)
    bot.reset()
    await dp.feed_update(cb_update(PARENT, "pschedule:custom", mid="901"))
    print("ответы:", bot.last_texts(2), "| edits:", [e["text"][:40] for e in bot.edits])
    await dp.feed_update(msg_update(PARENT, "2026-10-01", mid="3"))
    print("после даты-от:", bot.last_texts(2))
    await dp.feed_update(msg_update(PARENT, "2026-10-31", mid="4"))
    print("после даты-до:", [t[:90] for t in bot.last_texts(3)])

    print()
    print("=" * 72)
    print("3. РОДИТЕЛЬ: остаток занятий (menu:parent:balance)")
    print("=" * 72)
    bot.reset()
    await dp.feed_update(cb_update(PARENT, "menu:parent:balance", mid="902"))
    print("ответы:", [t[:200] for t in bot.last_texts(3)])

    print()
    print("=" * 72)
    print("4. ПРЕПОДАВАТЕЛЬ: /start -> телефон -> свой период")
    print("=" * 72)
    bot.reset()
    await dp.feed_update(msg_update(TEACHER, "/start", mid="10"))
    await dp.feed_update(msg_update(TEACHER, "+79005554433", mid="11"))
    print("роль в БД:", (db.get_user(TEACHER) or {}).get("role"))
    print("ответы:", [t[:70] for t in bot.last_texts(2)])
    bot.reset()
    await dp.feed_update(cb_update(TEACHER, "menu:teacher:schedule", mid="910"))
    print("меню периодов:", [t[:60] for t in bot.last_texts(1)])
    await dp.feed_update(cb_update(TEACHER, "schedule:custom", mid="911"))
    print("после 'свой период' -> ответы:", [t[:70] for t in bot.last_texts(1)],
          "| edits:", [e["text"][:50] for e in bot.edits])

    print()
    print("=" * 72)
    print("5. МЕНЕДЖЕР: /start (в ADMIN_TELEGRAM_IDS) -> сводка за период")
    print("=" * 72)
    bot.reset()
    await dp.feed_update(msg_update(ADMIN, "/start", mid="20"))
    print("после /start:", [t[:80] for t in bot.last_texts(2)])
    print("менеджер в БД:", db.get_user(ADMIN))
    bot.reset()
    await dp.feed_update(cb_update(ADMIN, "menu:manager:summary", mid="920"))
    print("после 'Сводка за период':", [t[:120] for t in bot.last_texts(2)])
    await dp.feed_update(msg_update(ADMIN, "2026-10-01", mid="21"))
    print("после начальной даты:", [t[:120] for t in bot.last_texts(2)])

    print()
    print("=" * 72)
    print("6. МЕНЕДЖЕР по паролю: /manager")
    print("=" * 72)
    bot.reset()
    settings.MANAGER_PASSWORD = "secret"
    await dp.feed_update(msg_update(ADMIN, "/manager", mid="30"))
    await dp.feed_update(msg_update(ADMIN, "secret", mid="31"))
    print("после входа по паролю:", [t[:80] for t in bot.last_texts(2)])


asyncio.run(main())
