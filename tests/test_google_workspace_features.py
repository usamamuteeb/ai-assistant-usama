import base64
import email
import sqlite3
from pathlib import Path

from plugins.google_workspace import plugin
from src.memory.store import SqliteStore


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _Messages:
    def __init__(self, message):
        self.message = message
        self.sent = []
        self.modified = []

    def get(self, **kwargs):
        return _Request(self.message)

    def send(self, **kwargs):
        self.sent.append(kwargs)
        return _Request({"id": "sent-1", "threadId": "thread-1"})

    def modify(self, **kwargs):
        self.modified.append(kwargs)
        return _Request({"id": self.message.get("id", "message-1")})

    def list(self, **kwargs):
        return _Request({"messages": [{"id": "message-1"}]})


class _Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _Gmail:
    def __init__(self, message):
        self.messages = _Messages(message)
        self._users = _Users(self.messages)

    def users(self):
        return self._users


class _Events:
    def __init__(self, events):
        self.events = events
        self.inserted = []
        self.patched = []
        self.deleted = []

    def list(self, **kwargs):
        return _Request({"items": self.events})

    def get(self, **kwargs):
        return _Request(self.events[0])

    def insert(self, **kwargs):
        self.inserted.append(kwargs)
        event = dict(kwargs["body"])
        event["id"] = "created-1"
        return _Request(event)

    def patch(self, **kwargs):
        self.patched.append(kwargs)
        event = dict(self.events[0])
        event.update(kwargs["body"])
        return _Request(event)

    def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return _Request({})


class _Calendar:
    def __init__(self, events):
        self._events = _Events(events)

    def events(self):
        return self._events


def _b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _message():
    return {
        "id": "message-1",
        "threadId": "thread-1",
        "payload": {
            "headers": [
                {"name": "From", "value": "Sender <sender@example.com>"},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Quarterly update"},
                {"name": "Date", "value": "Sun, 13 Sep 2026 09:00:00 +0500"},
                {"name": "Message-ID", "value": "<message-1@example.com>"},
                {"name": "References", "value": "<previous@example.com>"},
            ],
            "mimeType": "multipart/mixed",
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Hello from the full message.")}},
                {"mimeType": "application/pdf", "filename": "report.pdf", "body": {"data": _b64("pdf bytes")}},
            ],
        },
        "snippet": "short preview",
    }


def _tool_config(root: Path):
    (root / "config.yaml").write_text(
        "filesystem_tool:\n  workspace_root: workspace\nmemory:\n  sqlite_path: data/sqlite.db\n  chroma_path: data/chroma\n",
        encoding="utf-8",
    )


def test_get_message_and_reply_preserve_full_body_and_thread(tmp_path):
    gmail = _Gmail(_message())
    get_tool = plugin.GmailGetMessageTool(tmp_path)
    get_tool.service.gmail_service = lambda: gmail
    details = get_tool.run("message-1")
    assert details["body"] == "Hello from the full message."
    assert details["attachments"] == ["report.pdf"]

    reply = plugin.GmailReplyTool(tmp_path, confirm_fn=lambda _: True)
    reply.service.gmail_service = lambda: gmail
    result = reply.run("message-1", "Thanks!")
    assert result["thread_id"] == "thread-1"
    request = gmail.messages.sent[-1]["body"]
    assert request["threadId"] == "thread-1"
    raw = base64.urlsafe_b64decode(request["raw"] + "==")
    sent = email.message_from_bytes(raw)
    assert sent["In-Reply-To"] == "<message-1@example.com>"
    assert "Hello from" not in sent.get_payload()


def test_forward_quotes_body_without_reattaching_and_denials_are_exact(tmp_path):
    gmail = _Gmail(_message())
    forward = plugin.GmailForwardTool(tmp_path, confirm_fn=lambda _: True)
    forward.service.gmail_service = lambda: gmail
    result = forward.run("message-1", "me@example.com", "FYI")
    assert result["attachments_forwarded"] is False
    request = gmail.messages.sent[-1]["body"]
    raw = base64.urlsafe_b64decode(request["raw"] + "==")
    assert "FYI" in email.message_from_bytes(raw).get_payload()

    denied = plugin.GmailArchiveMessageTool(tmp_path, confirm_fn=lambda _: False)
    denied.service.gmail_service = lambda: gmail
    assert denied.run("message-1") == {"error": "Message not archived: confirmation denied or not provided."}


def test_attachment_download_uses_numbered_name(tmp_path):
    _tool_config(tmp_path)
    gmail = _Gmail(_message())
    tool = plugin.GmailDownloadAttachmentTool(tmp_path)
    tool.service.gmail_service = lambda: gmail
    first = tool.run("message-1", filename="report.pdf")
    second = tool.run("message-1", filename="report.pdf")
    assert first["filename"] == "report.pdf"
    assert second["filename"] == "report (1).pdf"
    assert (tmp_path / "workspace" / "gmail_attachments" / "report (1).pdf").read_bytes() == b"pdf bytes"


def test_free_time_handles_same_day_and_avoids_events(tmp_path):
    calendar = _Calendar([
        {"id": "event-1", "summary": "Meeting", "start": {"dateTime": "2026-09-14T10:00:00+05:00"}, "end": {"dateTime": "2026-09-14T11:00:00+05:00"}}
    ])
    tool = plugin.CalendarFindFreeTimeTool(tmp_path)
    tool.service.calendar_service = lambda: calendar
    result = tool.run("2026-09-14T09:00:00+05:00", "2026-09-14T17:00:00+05:00", 60)
    assert [(slot["start"][11:16], slot["end"][11:16]) for slot in result["free_slots"]] == [("09:00", "10:00"), ("11:00", "17:00")]


def test_calendar_update_delete_are_confirmed_with_event_context(tmp_path):
    event = {"id": "event-1", "summary": "Planning", "start": {"dateTime": "2026-09-14T10:00:00+05:00"}, "end": {"dateTime": "2026-09-14T11:00:00+05:00"}}
    calendar = _Calendar([event])
    prompts = []
    update = plugin.CalendarUpdateEventTool(tmp_path, confirm_fn=lambda prompt: prompts.append(prompt) or False)
    update.service.calendar_service = lambda: calendar
    assert update.run("event-1", title="New planning") == {"error": "Event not updated: confirmation denied or not provided."}
    delete = plugin.CalendarDeleteEventTool(tmp_path, confirm_fn=lambda prompt: prompts.append(prompt) or False)
    delete.service.calendar_service = lambda: calendar
    assert delete.run("event-1") == {"error": "Event not deleted: confirmation denied or not provided."}
    assert any("Planning" in prompt and "2026-09-14T10:00:00+05:00" in prompt for prompt in prompts)


def test_recurring_event_builds_rrule(tmp_path):
    calendar = _Calendar([])
    tool = plugin.CalendarCreateRecurringEventTool(tmp_path, confirm_fn=lambda _: True)
    tool.service.calendar_service = lambda: calendar
    result = tool.run("Standup", "2026-09-14T09:00:00+05:00", "2026-09-14T09:30:00+05:00", frequency="weekly", count=4)
    assert result["recurrence"] == "RRULE:FREQ=WEEKLY;INTERVAL=1;COUNT=4"
    assert calendar._events.inserted[0]["body"]["recurrence"] == [result["recurrence"]]


def test_email_to_task_uses_existing_tasks_table(tmp_path):
    _tool_config(tmp_path)
    db_path = tmp_path / "data" / "sqlite.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteStore(db_path):
        pass
    tool = plugin.EmailToTaskTool(tmp_path)
    tool.message_tool.fetch = lambda message_id: {"subject": "Please review", "from": "Owner <owner@example.com>"}
    result = tool.run("message-1")
    assert result["status"] == "created"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT description, status FROM tasks WHERE id = ?", (result["task_id"],)).fetchone()
    assert row == ("Email: Please review (from owner@example.com)", "open")
