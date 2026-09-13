"""Offline tests for task and one-time reminder tools."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

from plugins.task_manager.plugin import (
    CancelReminderTool,
    CompleteTaskTool,
    CreateTaskTool,
    DeleteTaskTool,
    ListRemindersTool,
    ListTasksTool,
    SetReminderTool,
    SnoozeReminderTool,
    UpdateTaskTool,
    deliver_due_reminders,
)
from src.memory.store import SqliteStore


def _tools(tmp_path: Path):
    store = SqliteStore(tmp_path / "tasks.db")
    return store, {
        "create": CreateTaskTool(store),
        "list": ListTasksTool(store),
        "update": UpdateTaskTool(store),
        "delete": DeleteTaskTool(store),
        "complete": CompleteTaskTool(store),
        "set_reminder": SetReminderTool(store),
        "list_reminders": ListRemindersTool(store),
        "cancel": CancelReminderTool(store),
        "snooze": SnoozeReminderTool(store),
    }


def test_task_crud_and_filters(tmp_path):
    store, tools = _tools(tmp_path)
    created = tools["create"].run(
        "Prepare demo", "Finish the slides", priority="high", due_date="tomorrow at 9:00 AM"
    )
    task_id = created["task"]["id"]

    listed = tools["list"].run(status="open", priority="high", query="slides")
    assert listed["count"] == 1
    assert listed["tasks"][0]["title"] == "Prepare demo"
    assert listed["tasks"][0]["priority"] == "high"

    updated = tools["update"].run(task_id, title="Prepare final demo", status="in_progress")
    assert updated["task"]["status"] == "in_progress"
    assert updated["task"]["title"] == "Prepare final demo"

    completed = tools["complete"].run(task_id)
    assert completed["task"]["status"] == "completed"
    assert tools["list"].run(status="open")["count"] == 0
    assert tools["list"].run(status="completed")["count"] == 1
    store.close()


def test_delete_requires_confirmation(tmp_path):
    store, tools = _tools(tmp_path)
    task_id = tools["create"].run("Delete me")["task"]["id"]
    denied_confirm = MagicMock(return_value=False)
    denied = DeleteTaskTool(store, confirm_fn=denied_confirm).run(task_id)
    assert denied == {"error": "Task not deleted: confirmation denied or not provided."}
    assert store.get_task(task_id) is not None

    approved_confirm = MagicMock(return_value=True)
    deleted = DeleteTaskTool(store, confirm_fn=approved_confirm).run(task_id)
    assert deleted["status"] == "deleted"
    approved_confirm.assert_called_once()
    assert store.get_task(task_id) is None
    store.close()


def test_reminder_lifecycle_and_exactly_once_delivery(tmp_path):
    store, tools = _tools(tmp_path)
    task_id = tools["create"].run("Check status")["task"]["id"]
    reminder = tools["set_reminder"].run("Check the deployment", "in 5 minutes", task_id=task_id)
    reminder_id = reminder["reminder"]["id"]
    assert reminder["status"] == "scheduled"
    assert tools["list_reminders"].run()["count"] == 1

    snoozed = tools["snooze"].run(reminder_id, minutes=10)
    assert snoozed["status"] == "snoozed"
    assert tools["cancel"].run(reminder_id) == {"status": "cancelled", "reminder_id": reminder_id}
    assert tools["list_reminders"].run(status="upcoming")["count"] == 0

    due_id = store.add_reminder("Due now", time.time() - 1)
    logger = MagicMock()
    first = deliver_due_reminders(store, logger=logger)
    second = deliver_due_reminders(store, logger=logger)
    assert [row["id"] for row in first] == [due_id]
    assert second == []
    logger.warning.assert_called_once()
    store.close()


def test_relative_reminders_accept_seconds_shorthand_and_compound_durations(tmp_path):
    store, tools = _tools(tmp_path)
    now = time.time()

    for expression, expected_seconds in (
        ("in 10 seconds", 10),
        ("in 10 sec", 10),
        ("after 2 mins", 120),
        ("in 1 hour and 30 minutes", 5400),
    ):
        result = tools["set_reminder"].run("Test reminder", expression)
        assert result["status"] == "scheduled"
        scheduled = store.get_reminder(result["reminder"]["id"])["remind_at"]
        assert scheduled - now >= expected_seconds - 1
        now = time.time()
    store.close()


def test_update_can_clear_due_date(tmp_path):
    store, tools = _tools(tmp_path)
    task_id = tools["create"].run("Due task", due_date="in 1 day")["task"]["id"]
    result = tools["update"].run(task_id, due_date="")
    assert result["task"]["due_date"] is None
    store.close()
