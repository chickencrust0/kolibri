import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# SQLite ограничивает число параметров в запросе (обычно 999).
_SQL_VAR_CHUNK = 500


class Database:
    def __init__(self, db_path: str = "bot.db"):
        self.db_path = db_path
        self._init_db()

    # ==================== СОЕДИНЕНИЯ ====================

    @contextmanager
    def _conn(self):
        """
        `with sqlite3.connect(...)` фиксирует транзакцию, но НЕ закрывает
        соединение — прежняя версия текла дескрипторами на каждом запросе.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    crm_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('teacher', 'parent', 'manager')),
                    phone TEXT NOT NULL,
                    full_name TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS homework_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    file_type TEXT DEFAULT 'document',
                    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS transfer_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    teacher_telegram_id INTEGER NOT NULL,
                    lesson_id INTEGER NOT NULL,
                    comment TEXT,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    resolved_by INTEGER,
                    FOREIGN KEY (teacher_telegram_id) REFERENCES users(telegram_id)
                );

                CREATE TABLE IF NOT EXISTS reminder_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lesson_id INTEGER NOT NULL,
                    reminder_type TEXT NOT NULL,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    target_telegram_id INTEGER,
                    status TEXT DEFAULT 'sent'
                );

                CREATE INDEX IF NOT EXISTS idx_homework_lesson ON homework_files(lesson_id);
                CREATE INDEX IF NOT EXISTS idx_transfer_status ON transfer_requests(status);
                CREATE INDEX IF NOT EXISTS idx_reminder_lookup
                    ON reminder_log(lesson_id, reminder_type, target_telegram_id, sent_at);
            """)

            # Миграция: роль автора заявки. Заявки от родителей раньше
            # вообще не сохранялись, теперь их нужно отличать.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(transfer_requests)")}
            if "author_role" not in columns:
                conn.execute(
                    "ALTER TABLE transfer_requests ADD COLUMN author_role TEXT DEFAULT 'teacher'"
                )
                logger.info("🛠 transfer_requests: добавлена колонка author_role")

    # ==================== ПОЛЬЗОВАТЕЛИ ====================

    def deactivate_user(self, telegram_id: int):
        with self._conn() as conn:
            conn.execute("UPDATE users SET is_active = 0 WHERE telegram_id = ?", (telegram_id,))

    def link_user(self, telegram_id: int, crm_id: int, role: str, phone: str, full_name: str):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO users (telegram_id, crm_id, role, phone, full_name, is_active)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(telegram_id) DO UPDATE SET
                       crm_id=excluded.crm_id,
                       role=excluded.role,
                       phone=excluded.phone,
                       full_name=excluded.full_name,
                       is_active=1""",
                (telegram_id, crm_id, role, phone, full_name),
            )

    def get_user(self, telegram_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ? AND is_active = 1", (telegram_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_crm_id(self, crm_id: int, role: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE crm_id = ? AND role = ? AND is_active = 1",
                (crm_id, role),
            ).fetchone()
            return dict(row) if row else None

    def get_all_users_by_role(self, role: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE role = ? AND is_active = 1", (role,)
            ).fetchall()
            return [dict(row) for row in rows]

    # ==================== ДОМАШНИЕ ЗАДАНИЯ ====================

    def add_homework_file(self, lesson_id: int, file_id: str, file_name: str, file_type: str = "document"):
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO homework_files (lesson_id, file_id, file_name, file_type) VALUES (?,?,?,?)",
                (lesson_id, file_id, file_name, file_type),
            )

    def get_homework_files(self, lesson_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM homework_files WHERE lesson_id = ? ORDER BY uploaded_at DESC",
                (lesson_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_homework_file_counts(
        self, lesson_ids: Optional[Sequence[int]] = None
    ) -> Dict[int, int]:
        """{lesson_id: количество файлов ДЗ} одним запросом."""
        counts: Dict[int, int] = {}
        with self._conn() as conn:
            if lesson_ids is None:
                rows = conn.execute(
                    "SELECT lesson_id, COUNT(*) AS cnt FROM homework_files GROUP BY lesson_id"
                ).fetchall()
                return {row["lesson_id"]: row["cnt"] for row in rows}

            ids = [int(i) for i in lesson_ids if i is not None]
            for start in range(0, len(ids), _SQL_VAR_CHUNK):
                chunk = ids[start:start + _SQL_VAR_CHUNK]
                placeholders = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT lesson_id, COUNT(*) AS cnt FROM homework_files "
                    f"WHERE lesson_id IN ({placeholders}) GROUP BY lesson_id",
                    tuple(chunk),
                ).fetchall()
                for row in rows:
                    counts[row["lesson_id"]] = row["cnt"]
        return counts

    # ==================== ЗАЯВКИ НА ПЕРЕНОС ====================

    def create_transfer_request(
        self,
        telegram_id: int,
        lesson_id: Optional[int],
        comment: str,
        author_role: str = "teacher",
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                """INSERT INTO transfer_requests
                       (teacher_telegram_id, lesson_id, comment, author_role)
                   VALUES (?,?,?,?)""",
                (telegram_id, int(lesson_id or 0), comment, author_role),
            )
            return cursor.lastrowid

    def get_transfer_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM transfer_requests WHERE id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def resolve_transfer_request(self, request_id: int, status: str, resolved_by: int) -> bool:
        with self._conn() as conn:
            cursor = conn.execute(
                """UPDATE transfer_requests
                   SET status=?, resolved_at=CURRENT_TIMESTAMP, resolved_by=?
                   WHERE id=? AND status='pending'""",
                (status, resolved_by, request_id),
            )
            # False — заявку уже обработал другой менеджер.
            return cursor.rowcount > 0

    def get_pending_transfer_requests(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT tr.*,
                          u.full_name AS author_name,
                          u.phone     AS author_phone
                   FROM transfer_requests tr
                   LEFT JOIN users u ON tr.teacher_telegram_id = u.telegram_id
                   WHERE tr.status = 'pending'
                   ORDER BY tr.created_at DESC"""
            ).fetchall()
            return [dict(row) for row in rows]

    # ==================== ЛОГ НАПОМИНАНИЙ ====================

    def mark_reminder_sent(
        self,
        lesson_id: int,
        reminder_type: str,
        target_telegram_id: Optional[int] = None,
    ):
        """
        Раньше все напоминания писались с типом 'close_lesson', а проверялись
        по типу 'upcoming_...' — дедупликация не срабатывала никогда,
        и напоминания дублировались на каждом тике планировщика.
        """
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reminder_log (lesson_id, reminder_type, target_telegram_id) VALUES (?,?,?)",
                (lesson_id, reminder_type, target_telegram_id),
            )

    def was_reminder_sent(
        self,
        lesson_id: int,
        reminder_type: str,
        target_telegram_id: Optional[int] = None,
        hours: int = 24,
    ) -> bool:
        query = """SELECT COUNT(*) AS count FROM reminder_log
                   WHERE lesson_id=? AND reminder_type=?
                     AND sent_at > datetime('now', ? || ' hours')"""
        params: List[Any] = [lesson_id, reminder_type, f"-{hours}"]
        if target_telegram_id is not None:
            query += " AND target_telegram_id=?"
            params.append(target_telegram_id)

        with self._conn() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
            return bool(row["count"])

    def cleanup_reminder_log(self, days: int = 30) -> int:
        """Лог напоминаний растёт вечно — чистим старое."""
        with self._conn() as conn:
            cursor = conn.execute(
                "DELETE FROM reminder_log WHERE sent_at < datetime('now', ? || ' days')",
                (f"-{days}",),
            )
            return cursor.rowcount
