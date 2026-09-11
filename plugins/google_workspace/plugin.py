"""Google Workspace plugin for Gmail and Calendar tools."""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from cryptography.fernet import Fernet
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from src.tools.base import Tool

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
        return raw.get("google_workspace", {})

    @property
    def credentials_path(self) -> Path:
        configured = self.data.get("credentials_path", "credentials.json")
        path = Path(configured)
        return path if path.is_absolute() else self.root / path

    @property
    def scopes(self) -> list[str]:
        configured = self.data.get("scopes", ["gmail.readonly", "gmail.send", "calendar"])
        items = configured if isinstance(configured, list) else [configured]
        normalized: list[str] = []
        for item in items:
            if item.startswith("https://") or item.startswith("http://"):
                normalized.append(item)
                continue
            mapping = {
                "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
                "gmail.send": "https://www.googleapis.com/auth/gmail.send",
                "calendar": "https://www.googleapis.com/auth/calendar",
            }
            normalized.append(mapping.get(item, item))
        return normalized


class GoogleWorkspaceService:
    def __init__(self, root: Path):
        self.root = root
        self.config = GoogleWorkspaceConfig(root)
        self.token_path = self.root / "data" / "google_token.json"
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

    def _token_key(self) -> bytes:
        token_key = os.getenv("GOOGLE_TOKEN_KEY")
        if not token_key:
            raise RuntimeError(
                "GOOGLE_TOKEN_KEY is not set. Generate one with Fernet.generate_key() and add it to .env."
            )
        return token_key.encode("utf-8")

    def _load_cached_token(self) -> Optional[Credentials]:
        if not self.token_path.exists():
            return None
        encrypted = self.token_path.read_bytes()
        try:
            token_json = Fernet(self._token_key()).decrypt(encrypted)
        except Exception as exc:  # pragma: no cover - runtime auth failure path
            raise RuntimeError(f"Failed to decrypt cached Google token: {exc}") from exc
        try:
            payload = json.loads(token_json.decode("utf-8"))
        except json.JSONDecodeError as exc:  # pragma: no cover - runtime auth failure path
            raise RuntimeError(f"Cached Google token is malformed: {exc}") from exc
        return Credentials.from_authorized_user_info(payload)

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
            raise FileNotFoundError(
                f"Google OAuth credentials not found at {self.config.credentials_path}. "
                "Download credentials.json from Google Cloud Console and place it in the project root."
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.config.credentials_path),
            self.config.scopes,
        )
        credentials = flow.run_local_server(port=0)
        self._save_cached_token(credentials)
        return credentials

    def gmail_service(self):
        return build("gmail", "v1", credentials=self.get_credentials())

    def calendar_service(self):
        return build("calendar", "v3", credentials=self.get_credentials())


class GmailSearchTool(Tool):
    name = "gmail_search_messages"
    description = (
        "Search the user's Gmail account by query and return sender, subject, snippet, and date for matching messages."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Gmail search query, such as 'from:alice subject:project'"},
            "max_results": {"type": "integer", "description": "Maximum number of results to return.", "default": 10},
        },
        "required": ["query"],
    }

    def __init__(self, root: Path):
        self.service = GoogleWorkspaceService(root)

    def run(self, query: str, max_results: int = 10) -> Any:
        try:
            gmail = self.service.gmail_service()
            result = gmail.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            messages = result.get("messages", [])
            results: list[dict[str, str]] = []
            for message in messages:
                msg = gmail.users().messages().get(
                    userId="me",
                    id=message["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                ).execute()
                headers = {header["name"]: header.get("value", "") for header in msg.get("payload", {}).get("headers", [])}
                results.append(
                    {
                        "id": msg.get("id", ""),
                        "sender": headers.get("From", ""),
                        "subject": headers.get("Subject", ""),
                        "snippet": msg.get("snippet", ""),
                        "date": headers.get("Date", ""),
                    }
                )
            return {"query": query, "count": len(results), "results": results}
        except Exception as exc:  # pragma: no cover - depends on credentials + network
            return {"error": f"Gmail search failed: {exc}"}


class GmailSendTool(Tool):
    name = "gmail_send_email"
    description = "Send an email through Gmail after explicit confirmation from the user."
    input_schema = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Email subject line."},
            "body": {"type": "string", "description": "Plain-text email body."},
        },
        "required": ["to", "subject", "body"],
    }

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service = GoogleWorkspaceService(root)
        self.confirm_fn = confirm_fn or (lambda message: False)

    def run(self, to: str, subject: str, body: str) -> Any:
        if not self.confirm_fn(f"Send email to {to} with subject '{subject}'?"):
            return {"error": "Email not sent: confirmation denied or not provided."}
        try:
            message = {
                "raw": base64.urlsafe_b64encode(
                    f"To: {to}\nSubject: {subject}\n\n{body}".encode("utf-8")
                ).decode("ascii")
            }
            gmail = self.service.gmail_service()
            result = gmail.users().messages().send(userId="me", body=message).execute()
            return {"status": "sent", "to": to, "subject": subject, "message_id": result.get("id")}
        except Exception as exc:  # pragma: no cover - depends on credentials + network
            return {"error": f"Gmail send failed: {exc}"}


class CalendarListEventsTool(Tool):
    name = "calendar_list_events"
    description = "List upcoming calendar events in a date range."
    input_schema = {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "Start of the date range in ISO 8601 format."},
            "end": {"type": "string", "description": "End of the date range in ISO 8601 format."},
            "max_results": {"type": "integer", "description": "Maximum events to return.", "default": 10},
        },
        "required": ["start", "end"],
    }

    def __init__(self, root: Path):
        self.service = GoogleWorkspaceService(root)

    def _event_time(self, value: Any) -> str:
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    def run(self, start: str, end: str, max_results: int = 10) -> Any:
        try:
            calendar = self.service.calendar_service()
            results = (
                calendar.events()
                .list(
                    calendarId="primary",
                    timeMin=self._event_time(start),
                    timeMax=self._event_time(end),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=max_results,
                )
                .execute()
            )
            events = []
            for event in results.get("items", []):
                start_value = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
                end_value = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
                events.append(
                    {
                        "id": event.get("id"),
                        "title": event.get("summary", "(no title)"),
                        "start": start_value,
                        "end": end_value,
                        "description": event.get("description", ""),
                        "location": event.get("location", ""),
                    }
                )
            return {"count": len(events), "events": events}
        except Exception as exc:  # pragma: no cover - depends on credentials + network
            return {"error": f"Calendar event listing failed: {exc}"}


class CalendarCreateEventTool(Tool):
    name = "calendar_create_event"
    description = "Create a calendar event after explicit confirmation from the user."
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title."},
            "start": {"type": "string", "description": "Event start time in ISO 8601 format."},
            "end": {"type": "string", "description": "Event end time in ISO 8601 format."},
            "description": {"type": "string", "description": "Optional event description."},
        },
        "required": ["title", "start", "end"],
    }

    def __init__(self, root: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.service = GoogleWorkspaceService(root)
        self.confirm_fn = confirm_fn or (lambda message: False)

    def run(self, title: str, start: str, end: str, description: str = "") -> Any:
        if not self.confirm_fn(f"Create calendar event '{title}' from {start} to {end}?"):
            return {"error": "Event not created: confirmation denied or not provided."}
        try:
            calendar = self.service.calendar_service()
            event = {
                "summary": title,
                "description": description,
                "start": {"dateTime": start},
                "end": {"dateTime": end},
            }
            created = calendar.events().insert(calendarId="primary", body=event).execute()
            return {
                "status": "created",
                "event_id": created.get("id"),
                "title": title,
                "start": created.get("start", {}).get("dateTime"),
                "end": created.get("end", {}).get("dateTime"),
            }
        except Exception as exc:  # pragma: no cover - depends on credentials + network
            return {"error": f"Calendar event creation failed: {exc}"}


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    root = Path(__file__).resolve().parents[2]
    return [
        GmailSearchTool(root=root),
        GmailSendTool(root=root, confirm_fn=confirm_fn),
        CalendarListEventsTool(root=root),
        CalendarCreateEventTool(root=root, confirm_fn=confirm_fn),
    ]
