"""
settings.py — единственная точка чтения окружения.

Раньше MANAGER_IDS / BRANCH_ID / статусы парсились в четырёх модулях
независимо, и значения расходились. Теперь всё здесь.

Модуль не импортирует ничего из проекта, поэтому его можно безопасно
подключать откуда угодно, включая bot/formatting.py.
"""

import os
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

# .env грузим здесь, а не в main.py — иначе порядок импортов влияет
# на то, увидит ли модуль переменные.
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore


def _int_list(raw: str):
    result = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            result.append(int(part))
    return result


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# ==================== ДОСТУПЫ ====================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()

ALFACRM_URL = os.getenv("ALFACRM_URL", "https://onlaynshkolashahmat.s20.online").strip()
ALFACRM_EMAIL = os.getenv("ALFACRM_EMAIL", "").strip()
ALFACRM_API_KEY = os.getenv("ALFACRM_API_KEY", "").strip()

BRANCH_ID = os.getenv("BRANCH_ID", "1").strip() or "1"
MANAGER_IDS = _int_list(os.getenv("ADMIN_TELEGRAM_IDS", ""))

DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "bot.db"))

# ==================== СТАТУСЫ УРОКОВ ====================

STATUS_PLANNED = _int("STATUS_PLANNED", 1)
STATUS_CANCELLED = _int("STATUS_CANCELLED", 2)
STATUS_CONDUCTED = _int("STATUS_CONDUCTED", 3)

ALL_STATUSES = (STATUS_PLANNED, STATUS_CANCELLED, STATUS_CONDUCTED)

# ==================== ЛИМИТЫ И ОКНА ====================

# AlfaCRM: не более 5 запросов в секунду на любые методы.
ALFACRM_RPS = _float("ALFACRM_RPS", 4.0)
ALFACRM_TIMEOUT = _float("ALFACRM_TIMEOUT", 30.0)
ALFACRM_MAX_PAGES = _int("ALFACRM_MAX_PAGES", 200)

# Кеш держит только окно вокруг сегодняшнего дня.
# Раньше он тянул ВСЮ историю уроков каждые 2 минуты — это росло
# без ограничений и съедало квоту API.
CACHE_DAYS_BACK = _int("CACHE_DAYS_BACK", 30)
CACHE_DAYS_FORWARD = _int("CACHE_DAYS_FORWARD", 60)
CACHE_REFRESH_MINUTES = _int("CACHE_REFRESH_MINUTES", 5)

# Пауза между сообщениями Telegram (лимит ~30 сообщений/сек суммарно,
# ~1 сообщение/сек в один чат).
TG_SEND_DELAY = _float("TG_SEND_DELAY", 0.06)
TG_BROADCAST_DELAY = _float("TG_BROADCAST_DELAY", 0.05)

# Сколько карточек урока с кнопками отправлять поштучно, прежде чем
# переключиться на сгруппированный текст без кнопок.
MAX_LESSON_CARDS = _int("MAX_LESSON_CARDS", 20)

# Порог «заканчивается абонемент».
LOW_BALANCE_THRESHOLD = _int("LOW_BALANCE_THRESHOLD", 2)

# ==================== ВРЕМЯ ====================

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")

# TZ_ERROR != None означает, что пояс филиала не разрешился и бот работает
# по UTC. На хостинге это выглядит как «напоминания приходят на 3 часа позже»,
# поэтому молча откатываться нельзя — main.py ругается на старте.
TZ_ERROR = None

if ZoneInfo is None:  # pragma: no cover  (Python < 3.9)
    TZ = timezone.utc
    TZ_ERROR = "zoneinfo недоступен (нужен Python 3.9+)"
else:
    try:
        TZ = ZoneInfo(TIMEZONE)
    except Exception as e:
        # Типичная причина: в образе нет базы часовых поясов IANA
        # (python:*-slim, alpine) — ставится пакетом tzdata.
        TZ = timezone.utc
        TZ_ERROR = (
            f"часовой пояс '{TIMEZONE}' не найден ({e.__class__.__name__}). "
            f"Установите пакет tzdata: pip install tzdata"
        )


def now() -> datetime:
    """Текущее время в часовом поясе филиала (aware)."""
    return datetime.now(TZ)


def today() -> date:
    return now().date()
