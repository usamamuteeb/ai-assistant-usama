"""Structured memory: conversation log, key/value state, and a simple task queue.

This is plain SQLite on purpose — at personal-assistant scale you don't need a
server, and it's trivial to inspect (`sqlite3 data/sqlite.db`).
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


class SqliteStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
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
                description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            """
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
    def add_task(self, description: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO tasks (description, status, created_at, updated_at) VALUES (?, 'open', ?, ?)",
            (description, time.time(), time.time()),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_task_status(self, task_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, time.time(), task_id),
        )
        self.conn.commit()

    def open_tasks(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM tasks WHERE status = 'open'").fetchall()
        return [dict(r) for r in rows]
