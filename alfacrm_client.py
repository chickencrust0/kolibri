import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

import settings

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    settings.STATUS_PLANNED: "📌 запланирован",
    settings.STATUS_CANCELLED: "❌ отменён",
    settings.STATUS_CONDUCTED: "✅ проведён",
}

# Поля, которые AlfaCRM ожидает получить обратно при lesson/update.
# Частичный апдейт (только {"status": 3}) в лучшем случае возвращает 400,
# в худшем — затирает время и состав участников урока.
LESSON_UPDATE_FIELDS = (
    "branch_id",
    "date",
    "time_from",
    "time_to",
    "lesson_type_id",
    "subject_id",
    "room_id",
    "status",
    "teacher_ids",
    "customer_ids",
    "group_ids",
    "topic",
    "homework",
    "note",
    "streaming_link",
    "is_public",
)


class AlfaCRMError(Exception):
    pass


class _RateLimiter:
    """Простой ограничитель: не больше N запросов в секунду."""

    def __init__(self, rps: float):
        self._min_interval = 1.0 / max(rps, 0.1)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class AlfaCRMClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        api_key: str,
        branch_id: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_key = api_key
        self.branch_id = str(branch_id or settings.BRANCH_ID)
        self.token: Optional[str] = None
        self.session: Optional[aiohttp.ClientSession] = None
        self._auth_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._limiter = _RateLimiter(settings.ALFACRM_RPS)

    # ==================== ИНФРАСТРУКТУРА ====================

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            async with self._session_lock:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=settings.ALFACRM_TIMEOUT)
                    )
        return self.session

    async def _ensure_token(self) -> None:
        if self.token:
            return
        # Без блокировки десяток параллельных запросов делал десяток
        # логинов одновременно и упирался в лимит 5 rps.
        async with self._auth_lock:
            if not self.token:
                await self._auth()

    async def _auth(self) -> None:
        session = await self._get_session()
        await self._limiter.acquire()
        try:
            async with session.post(
                f"{self.base_url}/v2api/auth/login",
                json={"email": self.email, "api_key": self.api_key},
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    raise AlfaCRMError(f"Ошибка авторизации ({response.status}): {data}")
                self.token = (data or {}).get("token")
                if not self.token:
                    raise AlfaCRMError("Токен не получен")
                logger.info("✅ Токен AlfaCRM получен")
        except aiohttp.ClientError as e:
            raise AlfaCRMError(f"Ошибка сети при авторизации: {e}")

    async def _make_request(
        self, method: str, endpoint: str, *, attempts: int = 3, **kwargs
    ) -> Dict[str, Any]:
        await self._ensure_token()
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        last_error: Optional[str] = None

        for attempt in range(attempts):
            headers = {"X-ALFACRM-TOKEN": self.token, "Content-Type": "application/json"}
            headers.update(kwargs.get("headers") or {})
            request_kwargs = {k: v for k, v in kwargs.items() if k != "headers"}

            await self._limiter.acquire()
            try:
                async with session.request(
                    method, url, headers=headers, **request_kwargs
                ) as response:
                    if response.status == 401:
                        logger.warning("🔄 Токен истёк, обновляем…")
                        self.token = None
                        await self._ensure_token()
                        last_error = "401 Unauthorized"
                        continue

                    if response.status == 429 or response.status >= 500:
                        last_error = f"HTTP {response.status}"
                        backoff = 0.5 * (2 ** attempt)
                        logger.warning(f"⚠️ {last_error} на {endpoint}, повтор через {backoff:.1f}с")
                        await asyncio.sleep(backoff)
                        continue

                    if response.status >= 400:
                        body = await response.text()
                        raise AlfaCRMError(f"HTTP {response.status} на {endpoint}: {body[:300]}")

                    return await response.json(content_type=None) or {}

            except asyncio.TimeoutError:
                last_error = "таймаут"
                await asyncio.sleep(0.5 * (2 ** attempt))
            except aiohttp.ClientError as e:
                last_error = f"сеть: {e}"
                await asyncio.sleep(0.5 * (2 ** attempt))

        raise AlfaCRMError(f"Запрос {method} {endpoint} не удался ({last_error})")

    async def _load_all_pages(self, endpoint: str, payload: dict) -> List[Dict]:
        """Забирает ВСЕ страницы, а не только нулевую."""
        all_items: List[Dict] = []
        page = 0
        payload = dict(payload)
        while page < settings.ALFACRM_MAX_PAGES:
            payload["page"] = page
            result = await self._make_request("POST", endpoint, json=payload)
            items = result.get("items") or []
            if not items:
                break
            all_items.extend(items)
            total = int(result.get("total") or 0)
            if total and len(all_items) >= total:
                break
            page += 1
        else:
            logger.warning(f"⚠️ Достигнут лимит страниц на {endpoint}")
        return all_items

    # ==================== ТЕЛЕФОНЫ ====================

    @staticmethod
    def _normalize_phone(phone: Any) -> str:
        return "".join(filter(str.isdigit, str(phone or "")))

    def _phone_matches(self, phone1: Any, phone2: Any) -> bool:
        clean1 = self._normalize_phone(phone1)
        clean2 = self._normalize_phone(phone2)
        if len(clean1) < 10 or len(clean2) < 10:
            return False
        # Сравниваем по последним 10 цифрам: +7/8/без кода — одно и то же.
        return clean1[-10:] == clean2[-10:]

    @staticmethod
    def _phones_of(record: Dict[str, Any]) -> List[str]:
        raw = record.get("phone")
        if isinstance(raw, (list, tuple)):
            return [str(p) for p in raw]
        if raw:
            return [str(raw)]
        return []

    # ==================== ПОЛЬЗОВАТЕЛИ ====================

    async def load_all_teachers(self) -> List[Dict[str, Any]]:
        return await self._load_all_pages(f"/v2api/{self.branch_id}/teacher/index", {})

    async def load_all_customers(self, is_study: Optional[int] = None) -> List[Dict]:
        payload: Dict[str, Any] = {}
        if is_study is not None:
            payload["is_study"] = is_study
        return await self._load_all_pages(f"/v2api/{self.branch_id}/customer/index", payload)

    async def find_teacher_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        # Раньше здесь смотрелась только страница 0 — педагоги за пределами
        # первой полусотни просто не могли войти в бота.
        for teacher in await self.load_all_teachers():
            if any(self._phone_matches(phone, p) for p in self._phones_of(teacher)):
                return teacher
        return None

    async def find_customer_by_phone(self, phone: str) -> Optional[Dict[str, Any]]:
        for is_study in (1, None):
            for customer in await self.load_all_customers(is_study=is_study):
                if any(self._phone_matches(phone, p) for p in self._phones_of(customer)):
                    return customer
        return None

    async def get_teacher_info(self, teacher_id: int) -> Optional[Dict[str, Any]]:
        for teacher in await self.load_all_teachers():
            if teacher.get("id") == teacher_id:
                return teacher
        return None

    async def get_customer_info(self, customer_id: int) -> Optional[Dict[str, Any]]:
        try:
            result = await self._make_request(
                "POST",
                f"/v2api/{self.branch_id}/customer/index",
                json={"id": customer_id, "page": 0},
            )
            for customer in result.get("items") or []:
                if customer.get("id") == customer_id:
                    return customer
        except AlfaCRMError as e:
            logger.debug(f"Поиск клиента по id не сработал: {e}")

        for is_study in (1, None):
            for customer in await self.load_all_customers(is_study=is_study):
                if customer.get("id") == customer_id:
                    return customer
        return None

    # ==================== УРОКИ ====================

    async def get_lessons(
        self,
        teacher_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        status: Optional[int] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Уроки за период. Если status не задан — перебираем все статусы,
        т.к. AlfaCRM по умолчанию отдаёт только проведённые.
        """
        statuses = [status] if status is not None else list(settings.ALL_STATUSES)

        base_payload: Dict[str, Any] = {}
        if teacher_id:
            base_payload["teacher_id"] = teacher_id
        if customer_id:
            base_payload["customer_id"] = customer_id
        if date_from:
            base_payload["date_from"] = date_from
        if date_to:
            base_payload["date_to"] = date_to

        by_id: Dict[Any, Dict[str, Any]] = {}

        for st in statuses:
            payload = {**base_payload, "status": st}
            page = 0
            fetched = 0
            while page < settings.ALFACRM_MAX_PAGES:
                payload["page"] = page
                result = await self._make_request(
                    "POST", f"/v2api/{self.branch_id}/lesson/index", json=payload
                )
                items = result.get("items") or []
                if not items:
                    break
                for lesson in items:
                    key = lesson.get("id") or id(lesson)
                    by_id[key] = lesson
                fetched += len(items)
                total = int(result.get("total") or 0)
                # Раньше здесь на каждой странице пересчитывался весь
                # накопленный список — O(n²) на месячных выборках.
                if total and fetched >= total:
                    break
                page += 1

        lessons = list(by_id.values())
        logger.info(f"📊 Получено уроков из API: {len(lessons)} ({date_from} – {date_to})")
        return lessons

    async def get_lesson(self, lesson_id: int) -> Optional[Dict[str, Any]]:
        """Один урок. Перебор всей истории как fallback убран — он стоил сотни запросов."""
        for st in [None] + list(settings.ALL_STATUSES):
            payload: Dict[str, Any] = {"id": lesson_id, "page": 0}
            if st is not None:
                payload["status"] = st
            try:
                result = await self._make_request(
                    "POST", f"/v2api/{self.branch_id}/lesson/index", json=payload
                )
            except AlfaCRMError as e:
                logger.debug(f"lesson/index id={lesson_id} status={st}: {e}")
                continue
            for lesson in result.get("items") or []:
                if lesson.get("id") == lesson_id:
                    return lesson
        return None

    async def update_lesson(
        self,
        lesson_id: int,
        updates: Dict[str, Any],
        current: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Обновляет урок, передавая полную модель.

        `current` можно передать из кеша, чтобы не ходить в CRM лишний раз.
        """
        if current is None:
            current = await self.get_lesson(lesson_id)
        if not current:
            raise AlfaCRMError(f"Урок {lesson_id} не найден в CRM")

        payload: Dict[str, Any] = {}
        for field in LESSON_UPDATE_FIELDS:
            value = current.get(field)
            if value is not None:
                payload[field] = value
        payload.update(updates)
        payload.setdefault("branch_id", int(self.branch_id))

        await self._make_request(
            "POST",
            f"/v2api/{self.branch_id}/lesson/update",
            params={"id": lesson_id},
            json=payload,
        )
        logger.info(f"✅ Урок {lesson_id} обновлён: {list(updates.keys())}")
        return payload

    async def mark_lesson_conducted(
        self, lesson_id: int, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(
            lesson_id, {"status": settings.STATUS_CONDUCTED}, current=current
        )

    async def set_homework(
        self, lesson_id: int, homework_text: str, current: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return await self.update_lesson(lesson_id, {"homework": homework_text}, current=current)

    # ==================== УТИЛИТЫ ====================

    @staticmethod
    def extract_user_name(user: Dict) -> str:
        return (user.get("name") or "").strip() or (user.get("legal_name") or "").strip() or "Без имени"

    def extract_user_phone(self, user: Dict) -> str:
        phones = self._phones_of(user)
        return phones[0] if phones else "Нет телефона"

    @staticmethod
    def get_lesson_status_label(status: int) -> str:
        return STATUS_LABELS.get(status, f"статус {status}")

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
