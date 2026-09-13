"""Google Workspace plugin for Gmail and Calendar tools."""
from __future__ import annotations

import base64
import html
import json
import os
import re
import sqlite3
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src.config import Settings
from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ConfirmFn = Callable[[str], bool]


class GoogleWorkspaceConfig:
    def __init__(self, root: Path):
        self.root = root
        self.data = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        config_path = self.root / "config.yaml"
        if not config_path.exists():
            return {}
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        values = raw.get("google_workspace", {}) if isinstance(raw, dict) else {}
        return values if isinstance(values, dict) else {}

    @property
    def credentials_path(self) -> Path:
        configured = self.data.get("credentials_path", "credentials.json")
        path = Path(configured)
        return path if path.is_absolute() else self.root / path

    @property
    def scopes(self) -> list[str]:
        configured = self.data.get("scopes", ["gmail.readonly", "gmail.send", "calendar"])
        items = configured if isinstance(configured, list) else [configured]
        mapping = {
            "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
            "gmail.send": "https://www.googleapis.com/auth/gmail.send",
            "gmail.modify": "https://www.googleapis.com/auth/gmail.modify",
            "calendar": "https://www.googleapis.com/auth/calendar",
        }
        return [mapping.get(str(item), str(item)) for item in items]


class GoogleWorkspaceService:
    def __init__(self, root: Path):
        self.root = root
        self.config = GoogleWorkspaceConfig(root)
        self.token_path = self.root / "data" / "google_token.json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

    def _token_key(self) -> bytes:
        token_key = os.getenv("GOOGLE_TOKEN_KEY")
        if not token_key:
            raise RuntimeError("GOOGLE_TOKEN_KEY is not set. Generate one with Fernet.generate_key() and add it to .env.")
        return token_key.encode("utf-8")

    def _load_cached_token(self) -> Optional[Credentials]:
        if not self.token_path.exists():
            return None
        try:
            token_json = Fernet(self._token_key()).decrypt(self.token_path.read_bytes())
            return Credentials.from_authorized_user_info(json.loads(token_json.decode("utf-8")))
        except Exception as exc:
            raise RuntimeError(f"Failed to load cached Google token: {exc}") from exc

    def _save_cached_token(self, creds: Credentials) -> None:
        payload = json.loads(creds.to_json())
        encrypted = Fernet(self._token_key()).encrypt(json.dumps(payload).encode("utf-8"))
        self.token_path.write_bytes(encrypted)

    def get_credentials(self) -> Credentials:
        credentials = self._load_cached_token()
        if credentials and credentials.valid:
            return credentials
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            self._save_cached_token(credentials)
            return credentials
        if not self.config.credentials_path.exists():
            raise FileNotFoundError(f"Google OAuth credentials not found at {self.config.credentials_path}. Download credentials.json from Google Cloud Console and place it in the project root.")
        flow = InstalledAppFlow.from_client_secrets_file(str(self.config.credentials_path), self.config.scopes)
        credentials = flow.run_local_server(port=0)
        self._save_cached_token(credentials)
        return credentials

    def gmail_service(self):
        return build("gmail", "v1", credentials=self.get_credentials())

    def calendar_service(self):
        return build("calendar", "v3", credentials=self.get_credentials())


def _settings_for(root: Path) -> Settings:
    path = root / "config.yaml"
    if not path.exists():
        return Settings(raw={}, root=root)
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return Settings(raw=raw if isinstance(raw, dict) else {}, root=root)


def _workspace_root(root: Path) -> Path:
    return _settings_for(root).workspace_root()


def _decode_b64(value: str | None) -> str:
    if not value:
        return ""
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode("utf-8", errors="replace")
    except (ValueError, UnicodeError):
        return ""


def _payload_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = payload.get("parts") or []
    if not parts:
        return [payload]
    result: list[dict[str, Any]] = []
    for part in parts:
        result.extend(_payload_parts(part))
    return result


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", html.unescape(value)).strip()


def _fetch_full_message(gmail: Any, message_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    message = gmail.users().messages().get(userId="me", id=message_id, format="full").execute()
    payload = message.get("payload", {})
    headers = {str(header.get("name", "")).casefold(): str(header.get("value", "")) for header in payload.get("headers", [])}
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in _payload_parts(payload):
        filename = str(part.get("filename", "") or "")
        body = part.get("body", {}) or {}
        if filename:
            attachments.append({"filename": filename, "part": part})
        decoded = _decode_b64(body.get("data"))
        mime = str(part.get("mimeType", "")).lower()
        if decoded and not filename:
            if mime == "text/plain":
                plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
    body = "\n\n".join(plain_parts).strip() or _strip_html("\n\n".join(html_parts))
    return {
        "id": message.get("id", message_id),
        "thread_id": message.get("threadId", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "reply_to": headers.get("reply-to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id": headers.get("message-id", ""),
        "references": headers.get("references", ""),
        # Return the decoded body, not Gmail's short snippet. An empty body is
        # preferable to silently mislabeling a preview as the full message.
        "body": body,
        "attachments": [str(item["filename"]) for item in attachments],
    }, attachments


def _send_plain_message(gmail: Any, *, to: str, subject: str, body: str,
                        headers: dict[str, str] | None = None, thread_id: str | None = None) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    for name, value in (headers or {}).items():
        if value:
            message[name] = value
    message.set_content(body)
    request: dict[str, Any] = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    if thread_id:
        request["threadId"] = thread_id
    return gmail.users().messages().send(userId="me", body=request).execute()


class GmailSearchTool(Tool):
    name = "gmail_search_messages"
    description = "Search the user's Gmail account by query and return sender, subject, snippet, and date for matching messages."
    input_schema = {"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 10}}, "required": ["query"]}

    def __init__(self, root: Path):
        self.service = GoogleWorkspaceService(root)

    def run(self, query: str, max_results: int = 10) -> Any:
        try:
            gmail = self.service.gmail_service()
            result = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            results = []
            for message in result.get("messages", []):
                msg = gmail.users().messages().get(userId="me", id=message["id"], format="metadata", metadataHeaders=["From", "Subject", "Date"]).execute()
                headers = {header["name"]: header.get("value", "") for header in msg.get("payload", {}).get("headers", [])}
                results.append({"id": msg.get("id", ""), "sender": headers.get("From", ""), "subject": headers.get("Subject", ""), "snippet": msg.get("snippet", ""), "date": headers.get("Date", "")})
            return {"query": query, "count": len(results), "results": results}
        except Exception as exc:
            return {"error": f"Gmail search failed: {exc}"}


class GmailGetMessageTool(Tool):
    name = "gmail_get_message"
    description = "Fetch a Gmail message's full headers, body text, thread ID, and attachment filenames."
    input_schema = {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}

    def __init__(self, root: Path):
        self.service = GoogleWorkspaceService(root)

    def fetch(self, message_id: str, gmail: Any | None = None) -> dict[str, Any]:
        service = gmail or self.service.gmail_service()
        details, _ = _fetch_full_message(service, message_id)
        return details

    def run(self, message_id: str) -> Any:
        try:
            return self.fetch(message_id)
        except Exception as exc:
            return {"error": f"Gmail message retrieval failed: {exc}"}


class GmailSendTool(Tool):
    name = "gmail_send_email"
    description = "Send an email through Gmail after explicit confirmation from the user."
    input_schema = {"type": "object", "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, "required": ["to", "subject", "body"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service = GoogleWorkspaceService(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, to: str, subject: str, body: str) -> Any:
        if not self.confirm_fn(f"Send email to {to} with subject '{subject}'?"):
            return {"error": "Email not sent: confirmation denied or not provided."}
        try:
            result = _send_plain_message(self.service.gmail_service(), to=to, subject=subject, body=body)
            return {"status": "sent", "to": to, "subject": subject, "message_id": result.get("id")}
        except Exception as exc:
            return {"error": f"Gmail send failed: {exc}"}


class GmailReplyTool(Tool):
    name = "gmail_reply"
    description = "Reply in the same Gmail thread after confirmation."
    input_schema = {"type": "object", "properties": {"message_id": {"type": "string"}, "body": {"type": "string"}}, "required": ["message_id", "body"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service, self.confirm_fn = GoogleWorkspaceService(root), confirm_fn or (lambda _: False)

    def run(self, message_id: str, body: str) -> Any:
        try:
            gmail = self.service.gmail_service()
            original, _ = _fetch_full_message(gmail, message_id)
            recipient = original.get("reply_to") or original.get("from", "")
            recipient = parseaddr(recipient)[1] or recipient
            subject = original.get("subject", "")
            if not self.confirm_fn(f"Reply to '{subject}' from {recipient} in the same thread?"):
                return {"error": "Email not sent: confirmation denied or not provided."}
            message_id_header = original.get("message_id", "")
            references = " ".join(filter(None, [original.get("references", ""), message_id_header]))
            result = _send_plain_message(gmail, to=recipient, subject=subject if subject.lower().startswith("re:") else f"Re: {subject}", body=body, headers={"In-Reply-To": message_id_header, "References": references}, thread_id=original.get("thread_id"))
            return {"status": "sent", "message_id": result.get("id"), "thread_id": result.get("threadId") or original.get("thread_id")}
        except Exception as exc:
            return {"error": f"Gmail reply failed: {exc}"}


class GmailForwardTool(Tool):
    name = "gmail_forward"
    description = "Forward a Gmail message after confirmation. Original attachments are NOT automatically re-attached; this is an intentional scope limitation."
    input_schema = {"type": "object", "properties": {"message_id": {"type": "string"}, "to": {"type": ["string", "array"]}, "note": {"type": "string"}}, "required": ["message_id", "to"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service, self.confirm_fn = GoogleWorkspaceService(root), confirm_fn or (lambda _: False)

    def run(self, message_id: str, to: str | list[str], note: str = "") -> Any:
        try:
            gmail = self.service.gmail_service()
            original, _ = _fetch_full_message(gmail, message_id)
            recipients = ", ".join(to) if isinstance(to, list) else str(to)
            if not self.confirm_fn(f"Forward '{original.get('subject', '')}' to {recipients}?"):
                return {"error": "Email not forwarded: confirmation denied or not provided."}
            # Attachments are deliberately not re-fetched/re-encoded here; the
            # forward contains the quoted original text only by design.
            quoted = f"---------- Forwarded message ----------\nFrom: {original.get('from', '')}\nDate: {original.get('date', '')}\nSubject: {original.get('subject', '')}\nTo: {original.get('to', '')}\n\n{original.get('body', '')}"
            body = f"{note.strip()}\n\n{quoted}" if note.strip() else quoted
            result = _send_plain_message(gmail, to=recipients, subject=f"Fwd: {original.get('subject', '')}", body=body)
            return {"status": "sent", "message_id": result.get("id"), "attachments_forwarded": False}
        except Exception as exc:
            return {"error": f"Gmail forward failed: {exc}"}


class GmailArchiveMessageTool(Tool):
    name = "gmail_archive_message"
    description = "Archive a Gmail message by removing its INBOX label after confirmation."
    input_schema = {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service, self.confirm_fn = GoogleWorkspaceService(root), confirm_fn or (lambda _: False)

    def run(self, message_id: str) -> Any:
        try:
            gmail = self.service.gmail_service()
            message, _ = _fetch_full_message(gmail, message_id)
            if not self.confirm_fn(f"Archive '{message.get('subject', '(no subject)')}' from {message.get('date', '(unknown time)')}?"):
                return {"error": "Message not archived: confirmation denied or not provided."}
            result = gmail.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}).execute()
            return {"status": "archived", "message_id": result.get("id", message_id)}
        except Exception as exc:
            return {"error": f"Gmail archive failed: {exc}"}


class GmailDownloadAttachmentTool(Tool):
    name = "gmail_download_attachment"
    description = "Download an email attachment into workspace/gmail_attachments without overwriting an existing file."
    input_schema = {"type": "object", "properties": {"message_id": {"type": "string"}, "filename": {"type": "string"}, "index": {"type": "integer"}}, "required": ["message_id"]}

    def __init__(self, root: Path):
        self.root, self.service = root, GoogleWorkspaceService(root)

    def run(self, message_id: str, filename: str | None = None, index: int | None = None) -> Any:
        try:
            gmail = self.service.gmail_service()
            message, attachments = _fetch_full_message(gmail, message_id)
            if filename:
                matches = [item for item in attachments if item["filename"] == filename]
            else:
                matches = attachments
            if index is not None:
                if index < 0 or index >= len(matches):
                    return {"error": f"Attachment index {index} is out of range."}
                chosen = matches[index]
            elif len(matches) == 1:
                chosen = matches[0]
            elif not matches:
                return {"error": f"No attachment named '{filename}' found in message '{message_id}'."}
            else:
                return {"error": "Multiple matching attachments found; provide an index."}
            part = chosen["part"]
            body = part.get("body", {}) or {}
            if body.get("attachmentId"):
                data = gmail.users().messages().attachments().get(userId="me", messageId=message_id, id=body["attachmentId"]).execute().get("data")
            else:
                data = body.get("data")
            raw = base64.urlsafe_b64decode(str(data or "") + "=" * (-len(str(data or "")) % 4))
            output_dir = _workspace_root(self.root) / "gmail_attachments"
            output_dir.mkdir(parents=True, exist_ok=True)
            original = Path(chosen["filename"]).name
            output = output_dir / original
            counter = 1
            while output.exists():
                output = output_dir / f"{Path(original).stem} ({counter}){Path(original).suffix}"
                counter += 1
            output.write_bytes(raw)
            return {"status": "downloaded", "filename": output.name, "path": output.relative_to(_workspace_root(self.root)).as_posix(), "size": len(raw), "message_id": message.get("id", message_id)}
        except Exception as exc:
            return {"error": f"Gmail attachment download failed: {exc}"}


class CalendarListEventsTool(Tool):
    name = "calendar_list_events"
    description = "List upcoming calendar events in a date range."
    input_schema = {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}, "max_results": {"type": "integer", "default": 10}}, "required": ["start", "end"]}

    def __init__(self, root: Path):
        self.service = GoogleWorkspaceService(root)

    def run(self, start: str, end: str, max_results: int = 10) -> Any:
        try:
            results = self.service.calendar_service().events().list(calendarId="primary", timeMin=_rfc3339(start), timeMax=_rfc3339(end), singleEvents=True, orderBy="startTime", maxResults=max_results).execute()
            events = [_event_view(event) for event in results.get("items", [])]
            return {"count": len(events), "events": events}
        except Exception as exc:
            return {"error": f"Calendar event listing failed: {exc}"}


def _parse_dt(value: str) -> datetime:
    normalized = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
    return parsed


def _rfc3339(value: str) -> str:
    return _parse_dt(value).isoformat()


def _event_view(event: dict[str, Any]) -> dict[str, Any]:
    return {"id": event.get("id"), "title": event.get("summary", "(no title)"), "start": event.get("start", {}).get("dateTime") or event.get("start", {}).get("date"), "end": event.get("end", {}).get("dateTime") or event.get("end", {}).get("date"), "description": event.get("description", ""), "location": event.get("location", "")}


class CalendarFindFreeTimeTool(Tool):
    name = "calendar_find_free_time"
    description = "Find calendar slots of a requested duration within working hours, avoiding existing events."
    input_schema = {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}, "duration_minutes": {"type": "integer"}, "work_hours_start": {"type": "string", "default": "09:00"}, "work_hours_end": {"type": "string", "default": "18:00"}}, "required": ["start", "end", "duration_minutes"]}

    def __init__(self, root: Path):
        self.service = GoogleWorkspaceService(root)

    def run(self, start: str, end: str, duration_minutes: int, work_hours_start: str = "09:00", work_hours_end: str = "18:00") -> Any:
        try:
            duration = int(duration_minutes)
            if duration <= 0:
                return {"error": "duration_minutes must be greater than zero."}
            start_dt, end_dt = _parse_dt(start), _parse_dt(end)
            if len(str(end).strip()) == 10:
                end_dt += timedelta(days=1)
            work_start = datetime.strptime(work_hours_start, "%H:%M").time()
            work_end = datetime.strptime(work_hours_end, "%H:%M").time()
            if work_start >= work_end or end_dt <= start_dt:
                return {"error": "Invalid date range or work hours."}
            response = self.service.calendar_service().events().list(calendarId="primary", timeMin=start_dt.isoformat(), timeMax=end_dt.isoformat(), singleEvents=True, orderBy="startTime", maxResults=2500).execute()
            events = [_event_view(event) for event in response.get("items", [])]
            busy = []
            for event in events:
                raw_start, raw_end = event.get("start"), event.get("end")
                event_start = _parse_dt(raw_start) if "T" in str(raw_start) else datetime.combine(date.fromisoformat(str(raw_start)), clock_time.min, tzinfo=start_dt.tzinfo)
                event_end = _parse_dt(raw_end) if "T" in str(raw_end) else datetime.combine(date.fromisoformat(str(raw_end)), clock_time.min, tzinfo=start_dt.tzinfo)
                busy.append((event_start, event_end))
            slots = []
            current_day = start_dt.date()
            # Include a same-day range (for example 09:00-17:00); using a
            # strict date comparison here would otherwise return no slots.
            while datetime.combine(current_day, clock_time.min, tzinfo=start_dt.tzinfo) < end_dt:
                window_start = max(start_dt, datetime.combine(current_day, work_start, tzinfo=start_dt.tzinfo))
                window_end = min(end_dt, datetime.combine(current_day, work_end, tzinfo=start_dt.tzinfo))
                cursor = window_start
                for busy_start, busy_end in sorted(busy):
                    if busy_end <= cursor or busy_start >= window_end:
                        continue
                    if busy_start > cursor and (busy_start - cursor).total_seconds() >= duration * 60:
                        slots.append({"start": cursor.isoformat(), "end": busy_start.isoformat()})
                    cursor = max(cursor, busy_end)
                if window_end > cursor and (window_end - cursor).total_seconds() >= duration * 60:
                    slots.append({"start": cursor.isoformat(), "end": window_end.isoformat()})
                current_day += timedelta(days=1)
            return {"start": start, "end": end, "duration_minutes": duration, "events": events, "free_slots": slots}
        except Exception as exc:
            return {"error": f"Calendar free-time search failed: {exc}"}


class CalendarCreateEventTool(Tool):
    name = "calendar_create_event"
    description = "Create a calendar event after explicit confirmation from the user."
    input_schema = {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}}, "required": ["title", "start", "end"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service, self.confirm_fn = GoogleWorkspaceService(root), confirm_fn or (lambda _: False)

    def run(self, title: str, start: str, end: str, description: str = "") -> Any:
        if not self.confirm_fn(f"Create calendar event '{title}' from {start} to {end}?"):
            return {"error": "Event not created: confirmation denied or not provided."}
        try:
            created = self.service.calendar_service().events().insert(calendarId="primary", body={"summary": title, "description": description, "start": {"dateTime": start}, "end": {"dateTime": end}}).execute()
            return {"status": "created", "event_id": created.get("id"), "title": title, "start": created.get("start", {}).get("dateTime"), "end": created.get("end", {}).get("dateTime")}
        except Exception as exc:
            return {"error": f"Calendar event creation failed: {exc}"}


class CalendarUpdateEventTool(Tool):
    name = "calendar_update_event"
    description = "Update only the supplied fields of a calendar event after confirmation."
    input_schema = {"type": "object", "properties": {"event_id": {"type": "string"}, "title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}}, "required": ["event_id"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service, self.confirm_fn = GoogleWorkspaceService(root), confirm_fn or (lambda _: False)

    def run(self, event_id: str, title: str | None = None, start: str | None = None, end: str | None = None, description: str | None = None) -> Any:
        try:
            calendar = self.service.calendar_service()
            current = calendar.events().get(calendarId="primary", eventId=event_id).execute()
            changes = {key: value for key, value in (("summary", title), ("description", description)) if value is not None}
            if start is not None:
                changes["start"] = {"dateTime": start}
            if end is not None:
                changes["end"] = {"dateTime": end}
            if not changes:
                return {"error": "At least one event field must be provided."}
            view = _event_view(current)
            if not self.confirm_fn(f"Update calendar event '{view['title']}' from {view['start']} to {view['end']} with {', '.join(changes)}?"):
                return {"error": "Event not updated: confirmation denied or not provided."}
            updated = calendar.events().patch(calendarId="primary", eventId=event_id, body=changes).execute()
            return {"status": "updated", "event": _event_view(updated)}
        except Exception as exc:
            return {"error": f"Calendar event update failed: {exc}"}


class CalendarDeleteEventTool(Tool):
    name = "calendar_delete_event"
    description = "Delete a calendar event after an informed confirmation showing its title and time."
    input_schema = {"type": "object", "properties": {"event_id": {"type": "string"}}, "required": ["event_id"]}

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service, self.confirm_fn = GoogleWorkspaceService(root), confirm_fn or (lambda _: False)

    def run(self, event_id: str) -> Any:
        try:
            calendar = self.service.calendar_service()
            current = calendar.events().get(calendarId="primary", eventId=event_id).execute()
            view = _event_view(current)
            if not self.confirm_fn(f"Delete calendar event '{view['title']}' from {view['start']} to {view['end']}?"):
                return {"error": "Event not deleted: confirmation denied or not provided."}
            calendar.events().delete(calendarId="primary", eventId=event_id).execute()
            return {"status": "deleted", "event_id": event_id, "title": view["title"], "start": view["start"], "end": view["end"]}
        except Exception as exc:
            return {"error": f"Calendar event deletion failed: {exc}"}


class CalendarCreateRecurringEventTool(CalendarCreateEventTool):
    name = "calendar_create_recurring_event"
    description = "Create a recurring calendar event after confirmation using simple daily, weekly, or monthly recurrence settings."
    input_schema = {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "description": {"type": "string"}, "frequency": {"type": "string", "enum": ["daily", "weekly", "monthly"]}, "interval": {"type": "integer", "default": 1}, "count": {"type": "integer"}, "until": {"type": "string"}}, "required": ["title", "start", "end", "frequency"]}

    def run(self, title: str, start: str, end: str, description: str = "", frequency: str = "weekly", interval: int = 1, count: int | None = None, until: str | None = None) -> Any:
        try:
            frequency = frequency.lower()
            if frequency not in {"daily", "weekly", "monthly"} or int(interval) <= 0 or ((count is None) == (until is None)):
                return {"error": "frequency must be daily, weekly, or monthly; interval must be positive; provide exactly one of count or until."}
            rule = f"RRULE:FREQ={frequency.upper()};INTERVAL={int(interval)}"
            if count is not None:
                if int(count) <= 0:
                    return {"error": "count must be greater than zero."}
                rule += f";COUNT={int(count)}"
            else:
                rule += f";UNTIL={_parse_dt(str(until)).astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            if not self.confirm_fn(f"Create recurring calendar event '{title}' from {start} to {end} with {rule}?"):
                return {"error": "Event not created: confirmation denied or not provided."}
            event = {"summary": title, "description": description, "start": {"dateTime": start}, "end": {"dateTime": end}, "recurrence": [rule]}
            created = self.service.calendar_service().events().insert(calendarId="primary", body=event).execute()
            return {"status": "created", "event_id": created.get("id"), "title": title, "recurrence": rule, "start": created.get("start", {}).get("dateTime"), "end": created.get("end", {}).get("dateTime")}
        except Exception as exc:
            return {"error": f"Recurring calendar event creation failed: {exc}"}


class EmailToTaskTool(Tool):
    name = "email_to_task"
    description = "Create a compatible local open task from a Gmail message; no external write is performed."
    input_schema = {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]}

    def __init__(self, root: Path):
        self.root, self.message_tool = root, GmailGetMessageTool(root)

    def run(self, message_id: str) -> Any:
        try:
            message = self.message_tool.fetch(message_id)
            sender = parseaddr(message.get("from", ""))[1] or message.get("from", "")
            description = f"Email: {message.get('subject', '(no subject)')} (from {sender})"
            settings = _settings_for(self.root)
            now = time.time()
            with sqlite3.connect(settings.sqlite_path()) as connection:
                cursor = connection.execute("INSERT INTO tasks (description, status, created_at, updated_at) VALUES (?, 'open', ?, ?)", (description, now, now))
                task_id = cursor.lastrowid
            return {"status": "created", "task_id": int(task_id), "description": description}
        except Exception as exc:
            return {"error": f"Could not create task from email: {exc}"}


class GetDailyAgendaTool(Tool):
    name = "get_daily_agenda"
    description = "Return today's calendar events, unread Gmail count, and likely urgent unread subjects on demand."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path):
        self.calendar_tool, self.gmail_tool = CalendarListEventsTool(root), GmailSearchTool(root)

    def run(self) -> Any:
        try:
            local_now = datetime.now().astimezone()
            start = datetime.combine(local_now.date(), clock_time.min, tzinfo=local_now.tzinfo)
            end = start + timedelta(days=1)
            calendar = self.calendar_tool.run(start.isoformat(), end.isoformat(), max_results=100)
            unread = self.gmail_tool.run("is:unread", max_results=50)
            errors = [item.get("error") for item in (calendar, unread) if isinstance(item, dict) and item.get("error")]
            messages = unread.get("results", []) if isinstance(unread, dict) else []
            urgent_words = re.compile(r"urgent|asap|action required|critical|deadline|important", re.I)
            urgent = [{"id": item.get("id"), "subject": item.get("subject", ""), "sender": item.get("sender", "")} for item in messages if urgent_words.search(f"{item.get('subject', '')} {item.get('snippet', '')}")]
            result = {"date": local_now.date().isoformat(), "events": calendar.get("events", []) if isinstance(calendar, dict) else [], "unread_count": len(messages), "unread_subjects": [item.get("subject", "") for item in messages], "urgent_unread": urgent}
            if errors:
                result["errors"] = errors
            return result
        except Exception as exc:
            return {"error": f"Daily agenda retrieval failed: {exc}"}


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    root = PROJECT_ROOT
    return [
        GmailSearchTool(root=root),
        GmailGetMessageTool(root=root),
        GmailSendTool(root=root, confirm_fn=confirm_fn),
        GmailReplyTool(root=root, confirm_fn=confirm_fn),
        GmailForwardTool(root=root, confirm_fn=confirm_fn),
        GmailArchiveMessageTool(root=root, confirm_fn=confirm_fn),
        GmailDownloadAttachmentTool(root=root),
        CalendarListEventsTool(root=root),
        CalendarFindFreeTimeTool(root=root),
        CalendarCreateEventTool(root=root, confirm_fn=confirm_fn),
        CalendarUpdateEventTool(root=root, confirm_fn=confirm_fn),
        CalendarDeleteEventTool(root=root, confirm_fn=confirm_fn),
        CalendarCreateRecurringEventTool(root=root, confirm_fn=confirm_fn),
        EmailToTaskTool(root=root),
        GetDailyAgendaTool(root=root),
    ]
