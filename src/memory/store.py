"""Structured memory: conversation log, key/value state, and a simple task queue.

This is plain SQLite on purpose — at personal-assistant scale you don't need a
server, and it's trivial to inspect (`sqlite3 data/sqlite.db`).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional


class SqliteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS kv_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS model_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tier TEXT NOT NULL,
                message_preview TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'normal',
                due_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT NOT NULL,
                remind_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                task_id INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS procedures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_signature TEXT NOT NULL,
                steps TEXT NOT NULL,
                success_count INTEGER DEFAULT 1,
                last_used_at REAL NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        # Older installations already have the original four-column tasks
        # table. Add new fields without invalidating existing task records.
        existing_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        migrations = {
            "title": "ALTER TABLE tasks ADD COLUMN title TEXT",
            "priority": "ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'normal'",
            "due_at": "ALTER TABLE tasks ADD COLUMN due_at REAL",
            "completed_at": "ALTER TABLE tasks ADD COLUMN completed_at REAL",
        }
        for column, statement in migrations.items():
            if column not in existing_columns:
                self.conn.execute(statement)
        self.conn.execute(
            "UPDATE tasks SET title = description WHERE title IS NULL OR title = ''"
        )
        self.conn.commit()

    def close(self) -> None:
        if getattr(self, "conn", None) is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "SqliteStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    # --- conversation log ---
    def log_message(self, session_id: str, role: str, content: str) -> None:
        self.conn.execute(
            "INSERT INTO conversation_log (session_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (session_id, role, content, time.time()),
        )
        self.conn.commit()

    def recent_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, content FROM conversation_log WHERE session_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    # --- key/value state ---
    def set_state(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, json.dumps(value), time.time()),
        )
        self.conn.commit()

    def get_state(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    # --- model call logging (for tuning the router heuristic) ---
    def log_model_call(self, tier: str, message_preview: str) -> None:
        self.conn.execute(
            "INSERT INTO model_calls (tier, message_preview, created_at) VALUES (?, ?, ?)",
            (tier, message_preview[:200], time.time()),
        )
        self.conn.commit()

    # --- tasks (used by the scheduler / future automations) ---
    def add_task(
        self,
        description: str,
        title: str | None = None,
        priority: str = "normal",
        due_at: float | None = None,
    ) -> int:
        """Create a task while preserving the original one-argument API."""
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO tasks "
                "(title, description, status, priority, due_at, created_at, updated_at) "
                "VALUES (?, ?, 'open', ?, ?, ?, ?)",
                (title or description, description, priority, due_at, now, now),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update_task_status(self, task_id: int, status: str) -> None:
        with self._lock:
            completed_at = time.time() if status in {"completed", "done"} else None
            self.conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ?, completed_at = ? WHERE id = ?",
                (status, time.time(), completed_at, task_id),
            )
            self.conn.commit()

    def open_tasks(self) -> list[dict[str, Any]]:
        return self.list_tasks(status="open")

    def list_tasks(
        self,
        status: str | None = None,
        priority: str | None = None,
        query: str | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        """List tasks with optional status, priority, and text filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if status and status not in {"all", "overdue"}:
            clauses.append("status = ?")
            params.append(status)
        if status == "overdue":
            clauses.append("status NOT IN ('completed', 'done', 'cancelled')")
            clauses.append("due_at IS NOT NULL AND due_at < ?")
            params.append(time.time() if now is None else now)
        if priority:
            clauses.append("priority = ?")
            params.append(priority)
        if query:
            clauses.append("(title LIKE ? OR description LIKE ?)")
            pattern = f"%{query}%"
            params.extend([pattern, pattern])
        sql = "SELECT * FROM tasks"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE WHEN status IN ('completed', 'done', 'cancelled') THEN 1 ELSE 0 END, "
        sql += "CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC, created_at DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            return dict(row) if row else None

    def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_at: float | None | object = None,
        update_due_at: bool = False,
    ) -> bool:
        fields: list[str] = []
        params: list[Any] = []
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        if description is not None:
            fields.append("description = ?")
        if status is not None:
            fields.append("status = ?")
            params.append(status)
            fields.append("completed_at = ?")
            params.append(time.time() if status in {"completed", "done"} else None)
        if priority is not None:
            fields.append("priority = ?")
            params.append(priority)
        if update_due_at:
            fields.append("due_at = ?")
            params.append(due_at)
        if description is not None:
            params.insert(1 if title is not None else 0, description)
        if not fields:
            return self.get_task(task_id) is not None
        fields.append("updated_at = ?")
        params.append(time.time())
        params.append(task_id)
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params
            )
            self.conn.commit()
            return cur.rowcount > 0

    def delete_task(self, task_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.conn.commit()
            return cur.rowcount > 0

    # --- reminders ---
    def add_reminder(self, message: str, remind_at: float, task_id: int | None = None) -> int:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO reminders "
                "(message, remind_at, status, task_id, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (message, remind_at, task_id, now, now),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_reminder(self, reminder_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_reminders(
        self,
        status: str | None = None,
        query: str | None = None,
        now: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status == "upcoming" or status == "pending":
            clauses.append("status = 'pending'")
            if status == "upcoming":
                clauses.append("remind_at >= ?")
                params.append(time.time() if now is None else now)
        elif status in {"delivered", "cancelled"}:
            clauses.append("status = ?")
            params.append(status)
        elif status == "overdue":
            clauses.extend(["status = 'pending'", "remind_at < ?"])
            params.append(time.time() if now is None else now)
        if query:
            clauses.append("message LIKE ?")
            params.append(f"%{query}%")
        sql = "SELECT * FROM reminders"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY remind_at ASC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def update_reminder(
        self,
        reminder_id: int,
        *,
        remind_at: float | None = None,
        status: str | None = None,
    ) -> bool:
        fields: list[str] = []
        params: list[Any] = []
        if remind_at is not None:
            fields.append("remind_at = ?")
            params.append(remind_at)
        if status is not None:
            fields.append("status = ?")
            params.append(status)
        if not fields:
            return self.get_reminder(reminder_id) is not None
        fields.append("updated_at = ?")
        params.extend([time.time(), reminder_id])
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE reminders SET {', '.join(fields)} WHERE id = ?", params
            )
            self.conn.commit()
            return cur.rowcount > 0

    def claim_due_reminders(self, now: float | None = None) -> list[dict[str, Any]]:
        """Atomically mark due pending reminders as delivered and return them."""
        cutoff = time.time() if now is None else now
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= ? "
                "ORDER BY remind_at ASC",
                (cutoff,),
            ).fetchall()
            reminders = [dict(row) for row in rows]
            if reminders:
                self.conn.executemany(
                    "UPDATE reminders SET status = 'delivered', updated_at = ? "
                    "WHERE id = ? AND status = 'pending'",
                    [(cutoff, reminder["id"]) for reminder in reminders],
                )
                self.conn.commit()
            return reminders

    # --- learned multi-tool procedures ---
    def add_procedure(self, task_signature: str, steps: list[str]) -> int:
        now = time.time()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO procedures (task_signature, steps, success_count, last_used_at, created_at) "
                "VALUES (?, ?, 1, ?, ?)",
                (task_signature, json.dumps(list(steps)), now, now),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_procedure(self, procedure_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM procedures WHERE id = ?", (procedure_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        try:
            decoded = json.loads(result["steps"])
            result["steps"] = decoded if isinstance(decoded, list) else []
        except (TypeError, json.JSONDecodeError):
            result["steps"] = []
        return result

    def mark_procedure_used(self, procedure_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE procedures SET success_count = success_count + 1, last_used_at = ? WHERE id = ?",
                (time.time(), procedure_id),
            )
            self.conn.commit()
            return cur.rowcount > 0
