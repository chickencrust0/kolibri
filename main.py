"""Kolibri Telegram: full Kolibri MAX workflows on Telegram + AlfaCRM."""
import asyncio
import logging

import settings
from alfacrm_client import AlfaCRMClient
from cache import LessonCache
from database import Database
from freezes import FreezeCounter
from scheduler import ReminderScheduler
from telegram_adapter import TelegramBot, telegram_update
from bot.dispatcher import Dispatcher
from bot.notifier import ManagerNotifier
from bot.handlers import common as _common  # noqa: F401
from bot.handlers import lead, manager, parent, start, support, teacher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger=logging.getLogger(__name__)

def build_dispatcher(bot, db, crm, cache, notifier, freezes):
    dp=Dispatcher(bot, db=db, crm=crm, cache=cache, notifier=notifier, freezes=freezes)
    for router in (start.router, support.router, lead.router, teacher.router, parent.router, manager.router):
        dp.include_router(router)
    return dp

async def main():
    if not settings.BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    bot=TelegramBot(settings.BOT_TOKEN)
    crm=AlfaCRMClient(settings.ALFACRM_URL, settings.ALFACRM_EMAIL, settings.ALFACRM_API_KEY, settings.BRANCH_ID)
    db=Database(settings.DB_PATH)
    cache=LessonCache(crm); cache.start()
    scheduler=ReminderScheduler(db, crm, bot, cache); scheduler.start()
    notifier=ManagerNotifier(bot, db)
    dp=build_dispatcher(bot, db, crm, cache, notifier, FreezeCounter(crm, db))
    offset=None
    await bot._bot.delete_webhook(drop_pending_updates=False)
    me=await bot.get_me()
    logger.info("Telegram bot connected: %s; build: %s", me, settings.BUILD)
    try:
        while True:
            updates=await bot._bot.get_updates(offset=offset, timeout=30, allowed_updates=["message","callback_query"])
            for update in updates:
                offset=update.update_id+1
                await dp.feed_update(telegram_update(bot, update))
    finally:
        await notifier.shutdown()
        cache.stop(); scheduler.stop()
        await crm.close(); await bot.close()

if __name__=="__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
