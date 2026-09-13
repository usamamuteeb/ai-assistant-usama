"""Task and one-time reminder tools backed by the assistant's SQLite store."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.memory.store import SqliteStore
from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ConfirmFn = Callable[[str], bool]
_UNSET = object()
_PRIORITIES = {"low", "normal", "high", "urgent"}
_STATUSES = {"open", "in_progress", "completed", "cancelled"}


def _load_config(root: Path) -> dict[str, Any]:
    try:
        with (root / "config.yaml").open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return config if isinstance(config, dict) else {}
    except Exception:
        return {}


def _store_for(root: Path) -> SqliteStore:
    config = _load_config(root)
    memory = config.get("memory", {})
    configured = memory.get("sqlite_path", "data/sqlite.db") if isinstance(memory, dict) else "data/sqlite.db"
    path = Path(str(configured))
    return SqliteStore(path if path.is_absolute() else root / path)


def _local_timezone():
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_datetime(value: Any) -> float:
    """Parse ISO timestamps and conversational reminder time expressions.

    Relative expressions intentionally support both the long and common short
    units so values such as ``in 10 sec`` work just as well as ``in 10
    seconds``. Multiple units are accepted too (for example, ``in 1 hour and
    30 minutes``).
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        raise ValueError("A date/time is required.")

    relative = re.fullmatch(r"(?:in|after)\s+(.+)", text.lower())
    if relative:
        unit_pattern = (
            r"(\d+(?:\.\d+)?)\s*"
            r"(seconds?|secs?|sec|s|minutes?|mins?|min|m|"
            r"hours?|hrs?|hr|h|days?|d|weeks?|wks?|wk|w)"
        )
        duration = relative.group(1).strip()
        compound_pattern = rf"{unit_pattern}(?:\s*(?:and|,)\s*{unit_pattern})*"
        if not re.fullmatch(compound_pattern, duration):
            raise ValueError(
                "Use a relative time such as 'in 10 seconds', 'in 10 sec', "
                "'after 5 minutes', or 'in 1 hour and 30 minutes'."
            )

        unit_seconds = {
            "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
            "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
            "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
            "day": 86400, "days": 86400, "d": 86400,
            "week": 604800, "weeks": 604800, "wk": 604800, "wks": 604800, "w": 604800,
        }
        seconds = sum(
            float(amount) * unit_seconds[unit]
            for amount, unit in re.findall(unit_pattern, duration)
        )
        return time.time() + seconds

    lower = text.lower()
    tomorrow_match = re.fullmatch(r"tomorrow(?:\s+at\s+(.+))?", lower)
    today_match = re.fullmatch(r"today(?:\s+at\s+(.+))?", lower)
    if tomorrow_match or today_match:
        clock = (tomorrow_match or today_match).group(1) or "09:00"
        parsed_clock = _parse_clock(clock)
        today = datetime.now().astimezone().date()
        day = today + timedelta(days=1 if tomorrow_match else 0)
        return datetime.combine(day, parsed_clock, tzinfo=_local_timezone()).timestamp()

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(
            "Use an ISO date/time (for example 2026-09-20T09:00), "
            "'tomorrow at 9:00 AM', or a relative time such as "
            "'in 10 seconds' or 'in 30 minutes'."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_local_timezone())
    return parsed.timestamp()


def _parse_clock(value: str):
    cleaned = value.strip().upper().replace(" ", "")
    for pattern in ("%H:%M", "%H", "%I:%M%p", "%I%p"):
        try:
            return datetime.strptime(cleaned, pattern).time()
        except ValueError:
            continue
    raise ValueError("Invalid clock time. Use for example 09:00 or 9:00 PM.")


def _display_time(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), tz=_local_timezone()).isoformat(timespec="minutes")


def _task_view(row: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    current = time.time() if now is None else now
    due_at = row.get("due_at")
    status = str(row.get("status", "open"))
    result = {
        "id": int(row["id"]),
        "title": row.get("title") or row.get("description", ""),
        "description": row.get("description", ""),
        "status": status,
        "priority": row.get("priority", "normal"),
        "due_date": _display_time(due_at),
        "created_at": _display_time(row.get("created_at")),
        "updated_at": _display_time(row.get("updated_at")),
    }
    result["overdue"] = bool(
        due_at is not None and float(due_at) < current and status not in {"completed", "done", "cancelled"}
    )
    return result


def _reminder_view(row: dict[str, Any], store: SqliteStore) -> dict[str, Any]:
    result = {
        "id": int(row["id"]),
        "message": row.get("message", ""),
        "remind_at": _display_time(row.get("remind_at")),
        "status": row.get("status", "pending"),
        "task_id": row.get("task_id"),
        "created_at": _display_time(row.get("created_at")),
    }
    if row.get("task_id") is not None:
        task = store.get_task(int(row["task_id"]))
        result["task_title"] = task.get("title") if task else None
    return result


class _TaskTool(Tool):
    def __init__(self, store: SqliteStore):
        self.store = store

    @staticmethod
    def _positive_id(value: Any, label: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive integer.") from exc
        if number <= 0:
            raise ValueError(f"{label} must be a positive integer.")
        return number

    @staticmethod
    def _priority(value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in _PRIORITIES:
            raise ValueError("priority must be one of: low, normal, high, urgent.")
        return normalized


class CreateTaskTool(_TaskTool):
    name = "create_task"
    description = "Create a personal task with a title, optional description, priority, and optional due date."
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short task title."},
            "description": {"type": "string", "description": "Optional task details."},
            "priority": {"type": "string", "enum": sorted(_PRIORITIES), "default": "normal"},
            "due_date": {
                "type": "string",
                "description": (
                    "Optional due time: ISO, 'today at 5 PM', 'tomorrow at 9 AM', "
                    "or relative time such as 'in 10 minutes'."
                ),
            },
        },
        "required": ["title"],
    }

    def run(self, title: str, description: str = "", priority: str = "normal", due_date: str | None = None) -> Any:
        try:
            clean_title = str(title).strip()
            if not clean_title:
                return {"error": "Task title cannot be empty."}
            due_at = None if not due_date else _parse_datetime(due_date)
            task_id = self.store.add_task(
                str(description or "").strip(), clean_title, self._priority(priority), due_at
            )
            task = self.store.get_task(task_id)
            return {"status": "created", "task": _task_view(task or {"id": task_id, "title": clean_title})}
        except Exception as exc:
            return {"error": f"Could not create task: {exc}"}


class ListTasksTool(_TaskTool):
    name = "list_tasks"
    description = "List personal tasks by status, priority, search text, or overdue state."
    input_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["open", "in_progress", "completed", "cancelled", "overdue", "all"], "default": "open"},
            "priority": {"type": "string", "enum": sorted(_PRIORITIES)},
            "query": {"type": "string", "description": "Optional text to find in title or description."},
        },
        "required": [],
    }

    def run(self, status: str = "open", priority: str | None = None, query: str | None = None) -> Any:
        try:
            status = str(status or "open").strip().lower()
            if status not in {"open", "in_progress", "completed", "cancelled", "overdue", "all"}:
                return {"error": "status must be open, in_progress, completed, cancelled, overdue, or all."}
            if priority:
                priority = self._priority(priority)
            rows = self.store.list_tasks(status=status, priority=priority, query=query)
            tasks = [_task_view(row) for row in rows]
            return {"status": status, "count": len(tasks), "tasks": tasks}
        except Exception as exc:
            return {"error": f"Could not list tasks: {exc}"}


class UpdateTaskTool(_TaskTool):
    name = "update_task"
    description = "Update a task's title, description, status, priority, or due date."
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "integer"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "status": {"type": "string", "enum": sorted(_STATUSES)},
            "priority": {"type": "string", "enum": sorted(_PRIORITIES)},
            "due_date": {
                "type": "string",
                "description": (
                    "New due time (ISO, today/tomorrow at a time, or relative such as "
                    "'in 10 minutes'); use an empty string to clear it."
                ),
            },
        },
        "required": ["task_id"],
    }

    def run(
        self,
        task_id: int,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        priority: str | None = None,
        due_date: str | object = _UNSET,
    ) -> Any:
        try:
            task_id = self._positive_id(task_id, "task_id")
            if self.store.get_task(task_id) is None:
                return {"error": f"Task {task_id} was not found."}
            if status is not None:
                status = str(status).strip().lower()
                if status not in _STATUSES:
                    return {"error": "status must be open, in_progress, completed, or cancelled."}
            if priority is not None:
                priority = self._priority(priority)
            update_due = due_date is not _UNSET
            due_at = None if update_due and not str(due_date).strip() else (_parse_datetime(due_date) if update_due else None)
            updated = self.store.update_task(
                task_id, title=title.strip() if isinstance(title, str) else title,
                description=description.strip() if isinstance(description, str) else description,
                status=status, priority=priority, due_at=due_at, update_due_at=update_due,
            )
            task = self.store.get_task(task_id)
            return {"status": "updated" if updated else "unchanged", "task": _task_view(task or {})}
        except Exception as exc:
            return {"error": f"Could not update task: {exc}"}


class DeleteTaskTool(_TaskTool):
    name = "delete_task"
    description = "Permanently delete a personal task. This always requires confirmation."
    input_schema = {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}

    def __init__(self, store: SqliteStore, confirm_fn: Optional[ConfirmFn] = None):
        super().__init__(store)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, task_id: int) -> Any:
        try:
            task_id = self._positive_id(task_id, "task_id")
            task = self.store.get_task(task_id)
            if task is None:
                return {"error": f"Task {task_id} was not found."}
            title = task.get("title") or task.get("description", "")
            if not self.confirm_fn(f"Delete task {task_id} '{title}' permanently?"):
                return {"error": "Task not deleted: confirmation denied or not provided."}
            self.store.delete_task(task_id)
            return {"status": "deleted", "task_id": task_id, "title": title}
        except Exception as exc:
            return {"error": f"Could not delete task: {exc}"}


class CompleteTaskTool(_TaskTool):
    name = "complete_task"
    description = "Mark a personal task as completed."
    input_schema = {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}

    def run(self, task_id: int) -> Any:
        try:
            task_id = self._positive_id(task_id, "task_id")
            if self.store.get_task(task_id) is None:
                return {"error": f"Task {task_id} was not found."}
            self.store.update_task_status(task_id, "completed")
            return {"status": "completed", "task": _task_view(self.store.get_task(task_id) or {})}
        except Exception as exc:
            return {"error": f"Could not complete task: {exc}"}


class SetReminderTool(_TaskTool):
    name = "set_reminder"
    description = (
        "Create a one-time reminder. Time may be ISO format, 'today at 5 PM', "
        "'tomorrow at 9 AM', or a relative expression such as 'in 10 seconds', "
        "'in 10 sec', 'after 5 minutes', or 'in 1 hour and 30 minutes'."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "What to remind the user about."},
            "remind_at": {
                "type": "string",
                "description": (
                    "When to remind the user: ISO, today/tomorrow at a time, "
                    "or relative time such as 'in 10 seconds' or 'in 30 minutes'."
                ),
            },
            "task_id": {"type": "integer", "description": "Optional task associated with this reminder."},
        },
        "required": ["message", "remind_at"],
    }

    def run(self, message: str, remind_at: str, task_id: int | None = None) -> Any:
        try:
            clean_message = str(message).strip()
            if not clean_message:
                return {"error": "Reminder message cannot be empty."}
            timestamp = _parse_datetime(remind_at)
            if timestamp <= time.time():
                return {"error": "The reminder time must be in the future."}
            linked_task = None
            if task_id is not None:
                linked_task = self._positive_id(task_id, "task_id")
                if self.store.get_task(linked_task) is None:
                    return {"error": f"Task {linked_task} was not found."}
            reminder_id = self.store.add_reminder(clean_message, timestamp, linked_task)
            return {"status": "scheduled", "reminder": _reminder_view(self.store.get_reminder(reminder_id) or {}, self.store)}
        except Exception as exc:
            return {"error": f"Could not set reminder: {exc}"}


class ListRemindersTool(_TaskTool):
    name = "list_reminders"
    description = "List upcoming, overdue, delivered, cancelled, or all one-time reminders."
    input_schema = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["upcoming", "overdue", "pending", "delivered", "cancelled", "all"], "default": "upcoming"},
            "query": {"type": "string"},
        },
        "required": [],
    }

    def run(self, status: str = "upcoming", query: str | None = None) -> Any:
        try:
            status = str(status or "upcoming").strip().lower()
            allowed = {"upcoming", "overdue", "pending", "delivered", "cancelled", "all"}
            if status not in allowed:
                return {"error": "status must be upcoming, overdue, pending, delivered, cancelled, or all."}
            rows = self.store.list_reminders(status=status, query=query)
            reminders = [_reminder_view(row, self.store) for row in rows]
            return {"status": status, "count": len(reminders), "reminders": reminders}
        except Exception as exc:
            return {"error": f"Could not list reminders: {exc}"}


class CancelReminderTool(_TaskTool):
    name = "cancel_reminder"
    description = "Cancel a pending one-time reminder."
    input_schema = {"type": "object", "properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]}

    def run(self, reminder_id: int) -> Any:
        try:
            reminder_id = self._positive_id(reminder_id, "reminder_id")
            reminder = self.store.get_reminder(reminder_id)
            if reminder is None:
                return {"error": f"Reminder {reminder_id} was not found."}
            if reminder.get("status") != "pending":
                return {"error": f"Reminder {reminder_id} is already {reminder.get('status')}."}
            self.store.update_reminder(reminder_id, status="cancelled")
            return {"status": "cancelled", "reminder_id": reminder_id}
        except Exception as exc:
            return {"error": f"Could not cancel reminder: {exc}"}


class SnoozeReminderTool(_TaskTool):
    name = "snooze_reminder"
    description = "Postpone a pending reminder by minutes or move it to a new date/time."
    input_schema = {
        "type": "object",
        "properties": {
            "reminder_id": {"type": "integer"},
            "minutes": {"type": "integer", "description": "Minutes from now; default 15."},
            "remind_at": {"type": "string", "description": "Optional replacement date/time."},
        },
        "required": ["reminder_id"],
    }

    def run(self, reminder_id: int, minutes: int = 15, remind_at: str | None = None) -> Any:
        try:
            reminder_id = self._positive_id(reminder_id, "reminder_id")
            reminder = self.store.get_reminder(reminder_id)
            if reminder is None:
                return {"error": f"Reminder {reminder_id} was not found."}
            if reminder.get("status") != "pending":
                return {"error": f"Reminder {reminder_id} is already {reminder.get('status')}."}
            if remind_at:
                timestamp = _parse_datetime(remind_at)
            else:
                minutes = int(minutes)
                if minutes <= 0:
                    return {"error": "minutes must be greater than zero."}
                timestamp = time.time() + minutes * 60
            if timestamp <= time.time():
                return {"error": "The new reminder time must be in the future."}
            self.store.update_reminder(reminder_id, remind_at=timestamp, status="pending")
            return {"status": "snoozed", "reminder": _reminder_view(self.store.get_reminder(reminder_id) or {}, self.store)}
        except Exception as exc:
            return {"error": f"Could not snooze reminder: {exc}"}


def deliver_due_reminders(store: SqliteStore, logger: Any = None) -> list[dict[str, Any]]:
    """Claim due reminders once and log them for the scheduler/UI caller."""
    due = store.claim_due_reminders()
    for reminder in due:
        message = f"Reminder #{reminder['id']}: {reminder['message']}"
        if logger is not None:
            logger.warning(message)
        else:
            print(message)
    return due


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    store = _store_for(PROJECT_ROOT)
    return [
        CreateTaskTool(store),
        ListTasksTool(store),
        UpdateTaskTool(store),
        DeleteTaskTool(store, confirm_fn=confirm_fn),
        CompleteTaskTool(store),
        SetReminderTool(store),
        ListRemindersTool(store),
        CancelReminderTool(store),
        SnoozeReminderTool(store),
    ]
