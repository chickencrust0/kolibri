import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import settings  # читает .env самостоятельно
from alfacrm_client import AlfaCRMClient
from cache import LessonCache
from database import Database
from scheduler import ReminderScheduler
from bot.handlers import manager, parent, start, teacher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def _check_config() -> None:
    problems = []
    if not settings.BOT_TOKEN or settings.BOT_TOKEN == "your_telegram_bot_token_here":
        problems.append("BOT_TOKEN не указан")
    if not settings.ALFACRM_EMAIL or not settings.ALFACRM_API_KEY:
        problems.append("ALFACRM_EMAIL / ALFACRM_API_KEY не указаны")
    if not settings.MANAGER_IDS:
        logger.warning("⚠️ ADMIN_TELEGRAM_IDS пуст — заявки и сводки никому не уйдут")
    if settings.TZ_ERROR:
        # Не падаем, но и не даём этому потеряться: при откате на UTC
        # напоминания на московском хостинге уедут на 3 часа.
        logger.error(f"❌ ЧАСОВОЙ ПОЯС: {settings.TZ_ERROR}")
        logger.error("❌ Бот работает по UTC — время напоминаний будет смещено!")
    if problems:
        for p in problems:
            print(f"❌ {p}")
        sys.exit(1)


def _build_bot() -> Bot:
    session = None
    if settings.PROXY_URL:
        # Таймаут задаётся у сессии: Bot(timeout=...) в aiogram 3 не существует
        # и падал с TypeError, как только был выставлен PROXY_URL.
        session = AiohttpSession(proxy=settings.PROXY_URL, timeout=60)
        logger.info("✅ Прокси включён")

    return Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def main() -> None:
    _check_config()
    logger.info(f"Запуск бота (branch_id={settings.BRANCH_ID}, TZ={settings.TIMEZONE})")

    db = Database(settings.DB_PATH)
    logger.info("✅ База данных")

    alfacrm = AlfaCRMClient(
        base_url=settings.ALFACRM_URL,
        email=settings.ALFACRM_EMAIL,
        api_key=settings.ALFACRM_API_KEY,
        branch_id=settings.BRANCH_ID,
    )
    logger.info("✅ Клиент AlfaCRM")

    cache = LessonCache(alfacrm)
    cache.start()

    bot = _build_bot()
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(manager.router)
    dp.include_router(teacher.router)
    dp.include_router(parent.router)
    dp.include_router(start.router)  # start последним: у него самые широкие фильтры

    scheduler = None
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот @{me.username} запущен")

        scheduler = ReminderScheduler(db, alfacrm, bot, cache=cache)
        scheduler.start()

        await dp.start_polling(bot, db=db, alfacrm=alfacrm, cache=cache)
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}", exc_info=True)
    finally:
        if scheduler:
            scheduler.stop()
        cache.stop()
        await alfacrm.close()
        await bot.session.close()
        logger.info("👋 Остановлено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Бот остановлен")
