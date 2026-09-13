"""Local WhatsApp Web tools using the shared visible Playwright session.

This plugin intentionally uses the user's already-authenticated WhatsApp Web
session rather than the paid Cloud API. The first run may require scanning the
WhatsApp Web QR code. Sending is always confirmation-gated and the prompt
contains the target contact plus the exact message so the user can edit or
deny it before anything is sent.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any, Callable, Optional

from src.tools.base import Tool

from plugins.browser.plugin import BrowserSession, BrowserRecoveryError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ConfirmFn = Callable[[str], bool]


def _error(message: str) -> dict[str, str]:
    return {"error": message}


def _is_whatsapp_url(url: str) -> bool:
    return "web.whatsapp.com" in str(url or "").lower()


def _page_login_state(page: Any) -> str | None:
    """Return a useful setup state without treating the QR screen as a crash."""
    try:
        body = (page.locator("body").inner_text(timeout=5000) or "").casefold()
    except Exception:
        return None
    if "scan qr code" in body or "link with phone number" in body:
        return "awaiting_qr_scan"
    return None


def _is_ready(page: Any) -> bool:
    """Detect the authenticated WhatsApp Web shell across UI revisions."""
    selectors = (
        "#pane-side",
        "[data-testid='chat-list']",
        "[data-testid='chat-list-search']",
        "[data-testid='cell-frame-container']",
    )
    try:
        return any(page.locator(selector).count() > 0 for selector in selectors)
    except Exception:
        return False


def _wait_for_initial_state(page: Any, timeout_ms: int = 8000) -> None:
    """Give an already-linked WhatsApp Web page a short, bounded boot window."""
    if _is_ready(page) or _page_login_state(page) == "awaiting_qr_scan":
        return
    try:
        page.locator(
            "#pane-side, [data-testid='chat-list'], [data-testid='chat-list-search']"
        ).first.wait_for(state="visible", timeout=timeout_ms)
    except Exception:
        # The caller turns the resulting state into a useful loading/login
        # response; never spend the orchestrator's whole tool-loop budget here.
        pass


def _require_ready(page: Any) -> dict[str, str] | None:
    if not _is_whatsapp_url(page.url):
        return _error("WhatsApp Web is not open. Use whatsapp_open first.")
    if not _is_ready(page):
        if _page_login_state(page) == "awaiting_qr_scan":
            return _error(
                "WhatsApp Web is waiting for login. Scan the QR code with WhatsApp on your phone, then try again."
            )
        return _error(
            "WhatsApp Web is open but not ready yet. Finish login in the browser window and try again."
        )
    return None


def _chat_rows(page: Any) -> list[dict[str, Any]]:
    """Extract chat summaries using stable-ish WhatsApp list markers."""
    selector = "[data-testid='cell-frame-container']"
    if page.locator(selector).count() == 0:
        selector = "[role='listitem']"
    return page.locator(selector).evaluate_all(
        """
        (elements) => elements.slice(0, 500).map((element) => {
          const text = (element.innerText || '').trim();
          const titleNode = element.querySelector('[title], [aria-label]');
          const name = (titleNode?.getAttribute('title') ||
                        titleNode?.getAttribute('aria-label') ||
                        element.querySelector('span[dir="auto"]')?.textContent ||
                        '').trim();
          const unreadNode = element.querySelector(
            '[data-testid*="unread"], [aria-label*="unread" i], ' +
            'span[aria-label*="new message" i]'
          );
          const dataId = element.getAttribute('data-id') ||
                         element.querySelector('[data-id]')?.getAttribute('data-id') || '';
          return {
            name: name || text.split('\\n')[0] || 'Unknown chat',
            preview: text,
            is_unread: Boolean(unreadNode),
            chat_id: dataId,
          };
        }).filter((row) => row.preview)
        """
    )


def _find_visible_chat(page: Any, contact: str) -> Any | None:
    """Find a currently visible chat row by name/number."""
    needle = str(contact or "").strip().casefold()
    if not needle:
        return None
    for selector in ("[data-testid='cell-frame-container']", "[role='listitem']"):
        rows = page.locator(selector)
        count = min(rows.count(), 500)
        for index in range(count):
            row = rows.nth(index)
            try:
                text = (row.inner_text(timeout=2000) or "").casefold()
            except Exception:
                continue
            if needle in text:
                return row
    return None


def _find_search_box(page: Any) -> Any | None:
    for selector in (
        "[data-testid='chat-list-search']",
        "input[placeholder*='Search' i]",
        "[contenteditable='true'][data-tab='3']",
    ):
        boxes = page.locator(selector)
        for index in range(boxes.count()):
            box = boxes.nth(index)
            try:
                if box.is_visible() and box.is_editable():
                    return box
            except Exception:
                continue
    return None


def _find_chat(page: Any, contact: str) -> Any | None:
    """Find a chat in the list, searching WhatsApp Web when necessary."""
    row = _find_visible_chat(page, contact)
    if row is not None:
        return row
    search_box = _find_search_box(page)
    if search_box is None:
        return None
    try:
        search_box.click(timeout=5000)
        search_box.fill(str(contact), timeout=10000)
        page.wait_for_timeout(800)
    except Exception:
        return None
    return _find_visible_chat(page, contact)


def _phone_digits(value: str) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _open_chat_by_phone(page: Any, contact: str) -> bool:
    """Open a direct WhatsApp chat URL for a number absent from the chat list."""
    digits = _phone_digits(contact)
    if len(digits) < 7:
        return False
    page.goto(
        f"https://web.whatsapp.com/send?phone={digits}",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    page.wait_for_timeout(1200)
    try:
        body = (page.locator("body").inner_text(timeout=5000) or "").casefold()
    except Exception:
        body = ""
    if "phone number shared via url is invalid" in body or "isn't on whatsapp" in body:
        return False
    return page.locator("[contenteditable='true']").count() > 0


def _recent_messages(page: Any, limit: int) -> list[dict[str, str]]:
    selector = "[data-testid='msg-container']"
    messages = page.locator(selector)
    result: list[dict[str, str]] = []
    if messages.count() > 0:
        for index in range(max(0, messages.count() - limit), messages.count()):
            node = messages.nth(index)
            try:
                text = (node.inner_text(timeout=2000) or "").strip()
            except Exception:
                continue
            if text:
                result.append({"text": text})
        return result

    # Fallback for UI revisions that do not expose msg-container.
    for selector in (
        "[data-testid='conversation-panel-body']",
        "[data-testid='conversation-panel-messages']",
    ):
        panel = page.locator(selector)
        if panel.count() > 0:
            text = (panel.last.inner_text(timeout=5000) or "").strip()
            if text:
                return [{"text": text[-20000:]}]
    return []


def _active_chat_header(page: Any) -> str:
    for selector in (
        "[data-testid='conversation-header']",
        "header",
    ):
        locator = page.locator(selector)
        if locator.count() > 0:
            try:
                text = (locator.last.inner_text(timeout=2000) or "").strip()
            except Exception:
                text = ""
            if text:
                return " ".join(text.split())
    return ""


def _last_outgoing_message_status(page: Any) -> dict[str, str] | None:
    """Read WhatsApp's visible receipt icon; never infer a receipt from text."""
    messages = page.locator("[data-testid='msg-container']")
    try:
        count = messages.count()
    except Exception:
        return None
    for index in range(count - 1, -1, -1):
        try:
            status = messages.nth(index).evaluate(
                """
                (element) => {
                  const outgoing = element.matches('.message-out') ||
                    Boolean(element.querySelector('.message-out, [data-testid="msg-check"], ' +
                    '[data-testid="msg-dblcheck"], [data-testid="msg-dblcheck-ack"], ' +
                    '[data-icon="msg-check"], [data-icon="msg-dblcheck"], [data-icon="msg-dblcheck-ack"]'));
                  if (!outgoing) return null;
                  const has = (selector) => Boolean(element.querySelector(selector));
                  let delivery = 'unknown';
                  if (has('[data-testid="msg-dblcheck-ack"], [data-icon="msg-dblcheck-ack"]')) {
                    delivery = 'read';
                  } else if (has('[data-testid="msg-dblcheck"], [data-icon="msg-dblcheck"]')) {
                    delivery = 'delivered';
                  } else if (has('[data-testid="msg-check"], [data-icon="msg-check"]')) {
                    delivery = 'sent';
                  }
                  const text = (element.innerText || '').trim();
                  const timestamp = element.querySelector('[data-pre-plain-text]')?.getAttribute('data-pre-plain-text') || '';
                  return {delivery, text, timestamp};
                }
                """
            )
        except Exception:
            continue
        if status:
            return status
    return None


class _WhatsAppTool(Tool):
    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def _run_browser(self, operation: Callable[[Any], Any]) -> Any:
        try:
            return self.session.run_with_recovery(operation)
        except BrowserRecoveryError as exc:
            return _error(str(exc))
        except Exception as exc:
            return _error(f"WhatsApp Web action failed: {exc}")

    def _whatsapp_page(self) -> tuple[str, Any]:
        """Open or reuse one dedicated WhatsApp tab from the persistent profile."""
        tab_id, page = self.session.page_for_site("web.whatsapp.com")
        if not _is_whatsapp_url(page.url):
            page.goto(
                "https://web.whatsapp.com",
                wait_until="commit",
                timeout=15000,
            )
            page.wait_for_timeout(750)
        _wait_for_initial_state(page)
        return tab_id, page

    def _run_whatsapp(self, operation: Callable[[Any], Any]) -> Any:
        def action(_active_page: Any) -> Any:
            _tab_id, whatsapp_page = self._whatsapp_page()
            return operation(whatsapp_page)

        return self._run_browser(action)


class WhatsAppOpenTool(_WhatsAppTool):
    name = "whatsapp_open"
    description = (
        "Open WhatsApp Web in its single persistent browser tab. On the first use only, scan "
        "the QR code with the phone's WhatsApp app; the project-local browser profile keeps "
        "that login for later app launches until WhatsApp expires or revokes it."
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    def run(self) -> Any:
        def action(page: Any) -> Any:
            state = _page_login_state(page)
            ready = _is_ready(page)
            if state:
                return {
                    "status": state,
                    "message": "Scan the QR code in the visible browser with WhatsApp on your phone.",
                    "tab_id": self.session.active_tab_id(),
                    "url": page.url,
                }
            return {
                "status": "ready" if ready else "loading",
                "message": "WhatsApp Web is ready." if ready else "WhatsApp Web is still loading; retry shortly.",
                "tab_id": self.session.active_tab_id(),
                "url": page.url,
            }

        return self._run_whatsapp(action)


class WhatsAppListUnreadChatsTool(_WhatsAppTool):
    name = "whatsapp_list_unread_chats"
    description = "List pending unread WhatsApp Web chats and their visible previews. Read-only; no confirmation is needed."
    input_schema = {
        "type": "object",
        "properties": {"limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 500}},
        "required": [],
    }

    def run(self, limit: int = 100) -> Any:
        try:
            limit = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            return _error("limit must be a positive integer.")

        def action(page: Any) -> Any:
            not_ready = _require_ready(page)
            if not_ready:
                return not_ready
            chats = [row for row in _chat_rows(page) if row.get("is_unread")]
            chats = chats[:limit]
            return {
                "status": "ok",
                "count": len(chats),
                "unread_chats": chats,
                "tab_id": self.session.active_tab_id(),
            }

        return self._run_whatsapp(action)


class WhatsAppReadChatTool(_WhatsAppTool):
    name = "whatsapp_read_chat"
    description = "Open a WhatsApp chat by contact name or phone number and return its recent visible messages. Read-only; no confirmation is needed."
    input_schema = {
        "type": "object",
        "properties": {
            "contact": {"type": "string", "description": "Visible contact or group name, or phone number."},
            "max_messages": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
        },
        "required": ["contact"],
    }

    def run(self, contact: str, max_messages: int = 50) -> Any:
        clean_contact = str(contact or "").strip()
        if not clean_contact:
            return _error("contact cannot be empty.")
        try:
            max_messages = max(1, min(int(max_messages), 200))
        except (TypeError, ValueError):
            return _error("max_messages must be a positive integer.")

        def action(page: Any) -> Any:
            not_ready = _require_ready(page)
            if not_ready:
                return not_ready
            row = _find_chat(page, clean_contact)
            if row is not None:
                row.click(timeout=15000)
            elif not _open_chat_by_phone(page, clean_contact):
                return _error(f"No WhatsApp chat or phone number matched '{clean_contact}'.")
            page.wait_for_timeout(500)
            return {
                "status": "ok",
                "contact_requested": clean_contact,
                "chat": _active_chat_header(page) or clean_contact,
                "messages": _recent_messages(page, max_messages),
                "tab_id": self.session.active_tab_id(),
            }

        return self._run_whatsapp(action)


class WhatsAppSendMessageTool(_WhatsAppTool):
    name = "whatsapp_send_message"
    description = (
        "Send one WhatsApp message to any contact or group through the active WhatsApp Web session. "
        "The required confirmation shows the exact destination and full message; denying it leaves "
        "the message unsent so it can be edited or abandoned."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "contact": {"type": "string", "description": "Visible contact or group name, or phone number."},
            "message": {"type": "string", "description": "The exact message to send."},
        },
        "required": ["contact", "message"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        super().__init__(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, contact: str, message: str) -> Any:
        clean_contact = str(contact or "").strip()
        clean_message = str(message or "")
        if not clean_contact:
            return _error("contact cannot be empty.")
        if not clean_message.strip():
            return _error("message cannot be empty.")

        # Resolve the destination before confirmation so the approval prompt
        # describes the chat that will actually receive the message.
        target: dict[str, Any] = {}

        def locate(page: Any) -> Any:
            not_ready = _require_ready(page)
            if not_ready:
                return not_ready
            row = _find_chat(page, clean_contact)
            if row is not None:
                target["resolved_chat"] = " ".join((row.inner_text(timeout=2000) or clean_contact).split())[:300]
                target["chat_mode"] = "row"
            elif _open_chat_by_phone(page, clean_contact):
                target["resolved_chat"] = clean_contact
                target["chat_mode"] = "phone_url"
            else:
                return _error(f"No WhatsApp chat or phone number matched '{clean_contact}'. Nothing was sent.")
            return {"status": "resolved"}

        resolved = self._run_whatsapp(locate)
        if not isinstance(resolved, dict) or resolved.get("error") or "chat_mode" not in target:
            return resolved

        resolved_chat = target.get("resolved_chat", clean_contact)
        confirmation = (
            f"Send WhatsApp message to '{resolved_chat}' (requested contact: '{clean_contact}')?\n"
            f"Exact message:\n{clean_message}\n"
            "Approve to send it now. Deny to leave it unsent so it can be edited or cancelled."
        )
        if not self.confirm_fn(confirmation):
            return {"error": "WhatsApp message not sent: confirmation denied or not provided."}

        def send(page: Any) -> Any:
            # Re-find the row after confirmation because the page may have
            # changed while the approval dialog was visible. For a number
            # opened through /send?phone=..., keep the verified current chat.
            if target.get("chat_mode") == "row":
                row = _find_chat(page, clean_contact)
                if row is None:
                    return _error(f"No WhatsApp chat matched '{clean_contact}'. Nothing was sent.")
                row.click(timeout=15000)
                page.wait_for_timeout(400)
            composer = page.locator("[contenteditable='true']")
            if composer.count() == 0:
                return _error("WhatsApp chat opened, but its message composer was not found. Nothing was sent.")
            field = composer.last
            field.click(timeout=10000)
            field.fill(clean_message, timeout=10000)
            field.press("Enter")
            return {
                "status": "sent",
                "contact": resolved_chat,
                "message": clean_message,
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tab_id": self.session.active_tab_id(),
            }

        return self._run_whatsapp(send)


class WhatsAppLastMessageStatusTool(_WhatsAppTool):
    name = "whatsapp_get_last_message_status"
    description = (
        "Open a WhatsApp chat and inspect its most recent outgoing visible message's receipt icon. "
        "Returns sent (one check), delivered (two checks), read (blue two checks), or unknown when "
        "WhatsApp does not expose a recognizable receipt. Read-only; no confirmation is needed."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "contact": {"type": "string", "description": "Visible contact or group name, or phone number."},
        },
        "required": ["contact"],
    }

    def run(self, contact: str) -> Any:
        clean_contact = str(contact or "").strip()
        if not clean_contact:
            return _error("contact cannot be empty.")

        def action(page: Any) -> Any:
            not_ready = _require_ready(page)
            if not_ready:
                return not_ready
            row = _find_chat(page, clean_contact)
            if row is not None:
                row.click(timeout=15000)
            elif not _open_chat_by_phone(page, clean_contact):
                return _error(f"No WhatsApp chat or phone number matched '{clean_contact}'.")
            page.wait_for_timeout(500)
            status = _last_outgoing_message_status(page)
            if status is None:
                return {
                    "status": "unknown",
                    "contact": _active_chat_header(page) or clean_contact,
                    "message": "No visible outgoing message or delivery receipt was found in this chat.",
                    "tab_id": self.session.active_tab_id(),
                }
            return {
                "status": status["delivery"],
                "contact": _active_chat_header(page) or clean_contact,
                "last_message": status["text"],
                "receipt_timestamp": status["timestamp"],
                "tab_id": self.session.active_tab_id(),
            }

        return self._run_whatsapp(action)


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    return [
        WhatsAppOpenTool(),
        WhatsAppListUnreadChatsTool(),
        WhatsAppReadChatTool(),
        WhatsAppLastMessageStatusTool(),
        WhatsAppSendMessageTool(confirm_fn=confirm_fn),
    ]
