"""
bot/handlers/common.py — общие помощники для хендлеров и планировщика.

Здесь живут:
  * кешируемые карты имён (педагоги, ученики);
  * единая точка получения уроков (кеш → CRM);
  * сборка сводки менеджера.

Функции отправки сообщений определены в bot/formatting.py и
реэкспортируются отсюда для обратной совместимости.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Sequence

import settings
from alfacrm_client import AlfaCRMClient
from database import Database
from bot.formatting import (  # noqa: F401  (реэкспорт)
    answer_blocks,
    build_summary,
    in_date_range,
    send_blocks,
)

logger = logging.getLogger(__name__)

# Карты имён меняются редко — держим их 5 минут.
_MAP_TTL = 300
_maps_cache: Dict[str, tuple] = {}


async def _cached(key: str, loader) -> Dict[int, str]:
    cached = _maps_cache.get(key)
    if cached and (time.monotonic() - cached[1]) < _MAP_TTL:
        return cached[0]
    try:
        data = await loader()
    except Exception as e:
        logger.error(f"Не удалось загрузить карту '{key}': {e}")
        return cached[0] if cached else {}
    _maps_cache[key] = (data, time.monotonic())
    return data


def invalidate_name_maps() -> None:
    """Сбросить кеш имён (например, после добавления педагога в CRM)."""
    _maps_cache.clear()


async def load_teacher_map(alfacrm: AlfaCRMClient) -> Dict[int, str]:
    """{teacher_id: имя} по всем страницам."""
    async def loader():
        items = await alfacrm.load_all_teachers()
        return {t["id"]: alfacrm.extract_user_name(t) for t in items if t.get("id")}

    return await _cached("teachers", loader)


async def load_customer_map(alfacrm: AlfaCRMClient) -> Dict[int, str]:
    """{customer_id: имя}. is_study=2 — клиенты и лиды, с откатом на клиентов."""
    async def loader():
        items = await alfacrm.load_all_customers(is_study=2)
        if not items:
            items = await alfacrm.load_all_customers(is_study=1)
        return {c["id"]: alfacrm.extract_user_name(c) for c in items if c.get("id")}

    return await _cached("customers", loader)


# ==================== УРОКИ ====================

async def fetch_lessons(
    alfacrm: AlfaCRMClient,
    cache=None,
    *,
    teacher_id: Optional[int] = None,
    customer_id: Optional[int] = None,
    status: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Единая точка получения уроков.

    Берём из кеша, если он покрывает период целиком, иначе идём в CRM.
    Фильтрация по датам делается здесь в обоих случаях — раньше каждый
    вызывающий фильтровал сам, и делал это по-разному.
    """
    if cache is not None and cache.covers(date_from, date_to):
        return cache.get_lessons(
            teacher_id=teacher_id,
            customer_id=customer_id,
            status=status,
            date_from=date_from,
            date_to=date_to,
        )

    lessons = await alfacrm.get_lessons(
        teacher_id=teacher_id,
        customer_id=customer_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    if status is not None:
        lessons = [l for l in lessons if l.get("status") == status]
    if date_from or date_to:
        lessons = [l for l in lessons if in_date_range(l, date_from, date_to)]
    return lessons


async def get_lesson_snapshot(
    lesson_id: int,
    alfacrm: AlfaCRMClient,
    cache=None,
) -> Optional[Dict[str, Any]]:
    """Текущее состояние урока: сперва кеш, потом CRM."""
    if cache is not None:
        lesson = cache.get_lesson(lesson_id)
        if lesson:
            return lesson
    return await alfacrm.get_lesson(lesson_id)


async def get_lesson_summary(
    lessons: Sequence[Dict[str, Any]],
    db: Database,
    alfacrm: AlfaCRMClient,
    period_label: str,
) -> List[str]:
    """
    Сводка по урокам для менеджера, сгруппированная по дням.

    Возвращает СПИСОК готовых сообщений (parse_mode="HTML"): нарезка идёт
    по границам карточек, поэтому HTML-теги не рвутся посередине.
    """
    if not lessons:
        return build_summary([], period_label=period_label)

    teachers = await load_teacher_map(alfacrm)
    customers = await load_customer_map(alfacrm)

    lesson_ids = [l["id"] for l in lessons if l.get("id")]
    try:
        hw_counts = db.get_homework_file_counts(lesson_ids)
    except Exception as e:
        logger.error(f"Не удалось получить счётчики файлов ДЗ: {e}")
        hw_counts = {}

    return build_summary(
        lessons,
        period_label=period_label,
        teachers=teachers,
        customers=customers,
        hw_counts=hw_counts,
        today=settings.today(),
    )
