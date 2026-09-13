"""Browser automation plugin using a shared, recoverable Playwright session."""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import yaml

from src.tools.base import Tool

ConfirmFn = Callable[[str], bool]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DENIED = {"error": "Action not performed: confirmation denied or not provided."}
_CRASH_SIGNATURES = (
    "has been closed",
    "target page, context or browser has been closed",
    "browser has been closed",
    "page has been closed",
    "target closed",
)


class BrowserConfig:
    def __init__(self, root: Path):
        self.root = root
        self._raw = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        config_path = self.root / "config.yaml"
        if not config_path.exists():
            return {}
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
            return raw if isinstance(raw, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}

    @property
    def browser_data(self) -> dict[str, Any]:
        value = self._raw.get("browser", {})
        return value if isinstance(value, dict) else {}

    @property
    def headless(self) -> bool:
        value = self.browser_data.get("headless", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def workspace_root(self) -> Path:
        filesystem = self._raw.get("filesystem_tool", {})
        filesystem = filesystem if isinstance(filesystem, dict) else {}
        configured = filesystem.get("workspace_root", "workspace")
        path = Path(str(configured)).expanduser()
        return path if path.is_absolute() else self.root / path

    @property
    def profile_dir(self) -> Path:
        """Dedicated Playwright profile; never reuse the user's normal Chrome profile."""
        configured = self.browser_data.get("profile_dir", "data/browser_profile")
        path = Path(str(configured)).expanduser()
        return path if path.is_absolute() else self.root / path


class BrowserRecoveryError(RuntimeError):
    """Raised only after a crashed browser was restarted and retried once."""


def _is_crash_error(exc: Exception) -> bool:
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError:
        return False
    if not isinstance(exc, PlaywrightError):
        return False
    message = str(exc).lower()
    return any(signature in message for signature in _CRASH_SIGNATURES)


class BrowserSession:
    _instance: Optional["BrowserSession"] = None

    def __init__(self, root: Path):
        self.root = root
        self.config = BrowserConfig(root)
        self._playwright = None
        self._browser = None
        self._context = None
        self._tabs: dict[str, Any] = {}
        self._active_tab_id: str | None = None
        self._next_tab_id = 1
        self._lock = threading.RLock()

    @classmethod
    def get_instance(cls, root: Path) -> "BrowserSession":
        if cls._instance is None:
            cls._instance = cls(root)
        return cls._instance

    def _launch(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        profile_dir = self.config.profile_dir
        profile_dir.mkdir(parents=True, exist_ok=True)
        # A persistent context retains WhatsApp's browser storage after the
        # first QR scan. It is deliberately project-local rather than the
        # user's regular Chrome profile, which could be locked or expose
        # unrelated browser data to automation.
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=self.config.headless,
            accept_downloads=True,
        )
        self._browser = getattr(self._context, "browser", None)
        self._tabs = {}
        self._active_tab_id = None
        self._next_tab_id = 1
        self._adopt_context_pages()

    def _track_page(self, page: Any, make_active: bool = True) -> tuple[str, Any]:
        for tab_id, tracked_page in self._tabs.items():
            if tracked_page is page:
                if make_active:
                    self._active_tab_id = tab_id
                return tab_id, page
        tab_id = str(self._next_tab_id)
        self._next_tab_id += 1
        self._tabs[tab_id] = page
        if make_active:
            self._active_tab_id = tab_id
        return tab_id, page

    def _adopt_context_pages(self) -> None:
        if self._context is None:
            return
        for page in self._context.pages:
            self._track_page(page, make_active=self._active_tab_id is None)

    def _new_tab(self) -> tuple[str, Any]:
        with self._lock:
            if self._context is None:
                self._launch()
            return self._track_page(self._context.new_page())

    def get_page(self):
        with self._lock:
            if self._context is None or self._playwright is None:
                self._launch()
            self._adopt_context_pages()
            if self._active_tab_id is None or self._active_tab_id not in self._tabs:
                if self._tabs:
                    self._active_tab_id = next(iter(self._tabs))
                else:
                    return self._new_tab()[1]
            page = self._tabs[self._active_tab_id]
            try:
                if page.is_closed():
                    del self._tabs[self._active_tab_id]
                    self._active_tab_id = None
                    return self._new_tab()[1]
            except Exception:
                pass
            return page

    def active_tab_id(self) -> str | None:
        return self._active_tab_id

    def page_for_open(self, new_tab: bool) -> tuple[str, Any]:
        if new_tab and self._tabs:
            return self._new_tab()
        if not self._tabs:
            return self._new_tab()
        return str(self._active_tab_id), self.get_page()

    def page_for_site(self, url_fragment: str) -> tuple[str, Any]:
        """Reuse a matching site tab, then a blank page, before opening a tab.

        This keeps a single WhatsApp Web tab instead of leaving an extra blank
        tab behind when a previous browser action already created one.
        """
        with self._lock:
            self.get_page()  # Ensures the persistent context and tab map exist.
            needle = url_fragment.casefold()
            for tab_id, page in self._tabs.items():
                try:
                    if needle in str(page.url).casefold() and not page.is_closed():
                        self._active_tab_id = tab_id
                        page.bring_to_front()
                        return tab_id, page
                except Exception:
                    continue
            active_page = self._tabs.get(self._active_tab_id or "")
            if active_page is not None:
                try:
                    if str(active_page.url).lower() in {"", "about:blank"}:
                        return str(self._active_tab_id), active_page
                except Exception:
                    pass
            for tab_id, page in self._tabs.items():
                try:
                    if str(page.url).lower() in {"", "about:blank"} and not page.is_closed():
                        self._active_tab_id = tab_id
                        page.bring_to_front()
                        return tab_id, page
                except Exception:
                    continue
            return self._new_tab()

    def switch_tab(self, tab_id: str) -> bool:
        tab_id = str(tab_id)
        with self._lock:
            if tab_id not in self._tabs:
                return False
            self._active_tab_id = tab_id
            try:
                self._tabs[tab_id].bring_to_front()
            except Exception:
                pass
            return True

    def close_tab(self, tab_id: str) -> bool:
        tab_id = str(tab_id)
        with self._lock:
            page = self._tabs.get(tab_id)
            if page is None:
                return False
            try:
                page.close()
            except Exception:
                pass
            del self._tabs[tab_id]
            if self._active_tab_id == tab_id:
                self._active_tab_id = next(iter(self._tabs), None)
            return True

    def list_tabs(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for tab_id, page in list(self._tabs.items()):
            try:
                rows.append({
                    "tab_id": tab_id,
                    "url": page.url,
                    "title": page.title(),
                    "is_active": tab_id == self._active_tab_id,
                })
            except Exception as exc:
                rows.append({
                    "tab_id": tab_id,
                    "url": "",
                    "title": "",
                    "is_active": tab_id == self._active_tab_id,
                    "error": str(exc),
                })
        return rows

    def run_with_recovery(self, operation: Callable[[Any], Any]) -> Any:
        """Run an action and retry it once after a closed/crashed target."""
        try:
            return operation(self.get_page())
        except Exception as exc:
            if not _is_crash_error(exc):
                raise
            original = str(exc)
            print(f"browser: detected a closed/crashed target; restarting browser and retrying once ({original})")
            self.restart()
            try:
                return operation(self.get_page())
            except Exception as retry_exc:
                raise BrowserRecoveryError(
                    f"Browser crashed and was restarted, but the action still failed: {original}."
                ) from retry_exc

    def restart(self) -> None:
        with self._lock:
            for page in list(self._tabs.values()):
                try:
                    page.close()
                except Exception:
                    pass
            self._tabs = {}
            self._active_tab_id = None
            if self._context is not None:
                try:
                    self._context.close()
                except Exception:
                    pass
            if self._playwright is not None:
                try:
                    self._playwright.stop()
                except Exception:
                    pass
            self._browser = None
            self._context = None
            self._playwright = None
            self._next_tab_id = 1

    def close(self) -> None:
        self.restart()


def _page_summary(page: Any, tab_id: str) -> dict[str, Any]:
    body_text = page.locator("body").inner_text(timeout=15000)
    visible_text = " ".join((body_text or "").split())[:3000]
    links = page.locator("a[href]").evaluate_all(
        """
        (elements) => elements.slice(0, 20).map((element) => ({
            text: (element.textContent || '').trim(),
            href: (element.href || element.getAttribute('href') || '').trim(),
        })).filter((link) => link.text || link.href)
        """
    )
    return {"tab_id": tab_id, "title": page.title(), "url": page.url, "text": visible_text, "links": links}


class BrowserOpenPageTool(Tool):
    name = "browser_open_page"
    description = "Open or navigate a URL in the active shared browser tab; set new_tab=true to open a separate active tab."
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open in the browser."},
            "new_tab": {"type": "boolean", "description": "Open a separate tab instead of navigating the active tab.", "default": False},
        },
        "required": ["url"],
    }

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self, url: str, new_tab: bool = False) -> Any:
        try:
            def action(_active_page: Any) -> Any:
                tab_id, page = self.session.page_for_open(bool(new_tab))
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                return _page_summary(page, tab_id)

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an element on the active browser tab by CSS selector or visible text. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {"target": {"type": "string", "description": "CSS selector or visible text."}},
        "required": ["target"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, target: str) -> Any:
        if not self.confirm_fn(f"Click '{target}' on the active browser tab?"):
            return dict(_DENIED)
        try:
            def action(page: Any) -> Any:
                try:
                    locator = page.locator(target)
                    if locator.count() > 0:
                        locator.first.click(timeout=15000)
                        return {"status": "clicked", "target": target, "tab_id": self.session.active_tab_id()}
                except Exception:
                    pass
                text_locator = page.get_by_text(target, exact=False)
                if text_locator.count() > 0:
                    text_locator.first.click(timeout=15000)
                    return {"status": "clicked", "target": target, "tab_id": self.session.active_tab_id()}
                return {"error": f"Browser action failed: no element matched '{target}'"}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class BrowserFillFieldTool(Tool):
    name = "browser_fill_field"
    description = "Fill a form field on the active browser tab by CSS selector and optionally press Enter. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string"},
            "value": {"type": "string"},
            "submit": {"type": "boolean", "default": False},
        },
        "required": ["selector", "value"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, selector: str, value: str, submit: bool = False) -> Any:
        if not self.confirm_fn(f"Fill '{selector}' on the active browser tab?"):
            return dict(_DENIED)
        try:
            def action(page: Any) -> Any:
                field = page.locator(selector)
                if field.count() == 0:
                    return {"error": f"Browser action failed: no element matched '{selector}'"}
                field.fill(value, timeout=15000)
                if submit:
                    field.press("Enter")
                return {"status": "filled", "selector": selector, "submitted": submit, "tab_id": self.session.active_tab_id()}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class ListBrowserTabsTool(Tool):
    name = "list_browser_tabs"
    description = "List open shared browser tabs with tab ID, URL, title, and active state."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self) -> Any:
        try:
            if not self.session._tabs:
                self.session.get_page()
            return {"active_tab_id": self.session.active_tab_id(), "tabs": self.session.list_tabs()}
        except Exception as exc:
            return {"error": f"Could not list browser tabs: {exc}"}


class SwitchBrowserTabTool(Tool):
    name = "switch_browser_tab"
    description = "Switch the active browser tab used by subsequent browser actions."
    input_schema = {"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self, tab_id: str) -> Any:
        if not self.session.switch_tab(tab_id):
            return {"error": f"Browser tab '{tab_id}' was not found."}
        return {"status": "switched", "active_tab_id": str(tab_id)}


class CloseBrowserTabTool(Tool):
    name = "close_browser_tab"
    description = "Close a browser tab and switch to another if needed. Closing a tab may lose page state and requires confirmation."
    input_schema = {"type": "object", "properties": {"tab_id": {"type": "string"}}, "required": ["tab_id"]}

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, tab_id: str) -> Any:
        if str(tab_id) not in self.session._tabs:
            return {"error": f"Browser tab '{tab_id}' was not found."}
        if not self.confirm_fn(f"Close browser tab '{tab_id}'? Unsaved page state could be lost."):
            return dict(_DENIED)
        if not self.session.close_tab(tab_id):
            return {"error": f"Browser tab '{tab_id}' was not found."}
        return {"status": "closed", "closed_tab_id": str(tab_id), "active_tab_id": self.session.active_tab_id()}


class BrowserGetPageTextTool(Tool):
    name = "browser_get_page_text"
    description = "Return up to 20,000 characters of full visible text from the active browser tab."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self) -> Any:
        try:
            def action(page: Any) -> Any:
                text = page.locator("body").inner_text(timeout=15000) or ""
                text = text[:20000] + ("... [truncated]" if len(text) > 20000 else "")
                return {"tab_id": self.session.active_tab_id(), "url": page.url, "text": text}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class BrowserFindLinksTool(Tool):
    name = "browser_find_links"
    description = "Return up to 100 links from the active browser tab, optionally filtered by text or URL substring."
    input_schema = {
        "type": "object",
        "properties": {"filter": {"type": "string", "description": "Optional substring filter for link text or href."}},
        "required": [],
    }

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self, filter: str = "") -> Any:
        try:
            needle = str(filter or "").casefold()

            def action(page: Any) -> Any:
                links = page.locator("a[href]").evaluate_all(
                    """
                    (elements) => elements.map((element) => ({
                        text: (element.textContent || '').trim(),
                        href: (element.href || element.getAttribute('href') || '').trim(),
                    })).filter((link) => link.text || link.href)
                    """
                )
                if needle:
                    links = [link for link in links if needle in f"{link.get('text', '')} {link.get('href', '')}".casefold()]
                return {"tab_id": self.session.active_tab_id(), "count": min(len(links), 100), "links": links[:100]}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class BrowserWaitForTextTool(Tool):
    name = "browser_wait_for_text"
    description = "Wait for text to appear on the active tab. Timeout defaults to 10 seconds and is hard-capped at 30 seconds."
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "timeout": {"type": "number", "default": 10, "maximum": 30}},
        "required": ["text"],
    }

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self, text: str, timeout: float = 10) -> Any:
        try:
            effective_timeout = max(0.1, min(float(timeout), 30.0))

            def action(page: Any) -> Any:
                page.get_by_text(text, exact=False).first.wait_for(state="visible", timeout=int(effective_timeout * 1000))
                return {"status": "found", "text": text, "tab_id": self.session.active_tab_id()}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            if "timeout" in str(exc).lower():
                return {"error": f"Text '{text}' did not appear within {effective_timeout:g}s."}
            return {"error": f"Browser action failed: {exc}"}


class BrowserSelectOptionTool(Tool):
    name = "browser_select_option"
    description = "Select an option in a HTML select element on the active tab by value or visible label. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {"selector": {"type": "string"}, "value": {"type": "string"}, "label": {"type": "string"}},
        "required": ["selector"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, selector: str, value: str = "", label: str = "") -> Any:
        if not value and not label:
            return {"error": "Provide value or label for the option."}
        chosen = f"value={value}" if value else f"label={label}"
        if not self.confirm_fn(f"Select {chosen} in '{selector}' on the active browser tab?"):
            return dict(_DENIED)
        try:
            def action(page: Any) -> Any:
                field = page.locator(selector)
                if field.count() == 0:
                    return {"error": f"Browser action failed: no element matched '{selector}'"}
                selected = field.select_option(value=value) if value else field.select_option(label=label)
                return {"status": "selected", "selector": selector, "selected": selected, "tab_id": self.session.active_tab_id()}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class BrowserFillFormTool(Tool):
    name = "browser_fill_form"
    description = "Fill multiple form fields on the active tab in sequence, optionally pressing Enter on the last field. Requires one confirmation for the whole batch."
    input_schema = {
        "type": "object",
        "properties": {
            "fields": {"type": "object", "description": "Map of CSS selectors to values."},
            "submit": {"type": "boolean", "default": False},
        },
        "required": ["fields"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, fields: dict[str, Any], submit: bool = False) -> Any:
        if not isinstance(fields, dict) or not fields:
            return {"error": "fields must be a non-empty object of selector/value pairs."}
        description = ", ".join(f"{key}={value!r}" for key, value in fields.items())
        if not self.confirm_fn(f"Fill these fields on the active browser tab: {description}?"):
            return dict(_DENIED)
        try:
            def action(page: Any) -> Any:
                filled: list[str] = []
                last_field = None
                for selector, value in fields.items():
                    field = page.locator(str(selector))
                    if field.count() == 0:
                        return {"error": f"Browser action failed: no element matched '{selector}'", "filled": filled}
                    field.fill(str(value), timeout=15000)
                    filled.append(str(selector))
                    last_field = field
                if submit and last_field is not None:
                    last_field.press("Enter")
                return {"status": "filled", "fields": filled, "submitted": bool(submit), "tab_id": self.session.active_tab_id()}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


def _workspace_file(config: BrowserConfig, path: str) -> tuple[Path | None, str | None]:
    root = config.workspace_root.resolve()
    raw = Path(str(path or "")).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    candidate = candidate.resolve(strict=False)
    if root not in candidate.parents or not candidate.is_file():
        if root not in candidate.parents:
            return None, f"Refused: upload path must be inside the workspace root '{root}'."
        return None, f"File '{candidate}' does not exist."
    return candidate, None


class BrowserUploadFileTool(Tool):
    name = "browser_upload_file"
    description = "Upload a workspace-local file through a file input on the active tab. Paths outside the configured workspace root are refused. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {"selector": {"type": "string"}, "file_path": {"type": "string", "description": "File path inside the workspace root."}},
        "required": ["selector", "file_path"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.config = BrowserConfig(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, selector: str, file_path: str) -> Any:
        candidate, error = _workspace_file(self.config, file_path)
        if error:
            return {"error": error}
        if not self.confirm_fn(f"Upload workspace file '{candidate}' using '{selector}' on the active browser tab?"):
            return dict(_DENIED)
        try:
            def action(page: Any) -> Any:
                field = page.locator(selector)
                if field.count() == 0:
                    return {"error": f"Browser action failed: no element matched '{selector}'"}
                field.set_input_files(str(candidate))
                return {"status": "uploaded", "selector": selector, "file_path": str(candidate), "tab_id": self.session.active_tab_id()}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


class BrowserSaveAsPdfTool(Tool):
    name = "browser_save_as_pdf"
    description = "Save the active browser tab as a PDF under workspace/browser_pdfs. This only works in headless Chromium; headed mode returns a clear error."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.config = BrowserConfig(root)

    def run(self) -> Any:
        if not self.config.headless:
            return {"error": "PDF export is unavailable because browser.headless is false. Set browser.headless: true and restart the assistant."}
        try:
            pdf_dir = self.config.workspace_root / "browser_pdfs"
            pdf_dir.mkdir(parents=True, exist_ok=True)

            def action(page: Any) -> Any:
                title = page.title().strip() or "page"
                safe_title = re.sub(r"[^A-Za-z0-9._-]+", "_", title).strip("._")[:100] or "page"
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                output = pdf_dir / f"{timestamp}_{safe_title}.pdf"
                page.pdf(path=str(output), print_background=True)
                return {"status": "saved", "path": str(output.relative_to(self.config.root)).replace("\\", "/"), "tab_id": self.session.active_tab_id()}

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser PDF export failed: {exc}"}


class BrowserDownloadFileTool(Tool):
    name = "browser_download_file"
    description = "Trigger a download from a selector or URL on the active tab and save it under workspace/downloads. Reports start/completion, not live byte-by-byte progress. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {"target": {"type": "string", "description": "CSS selector, visible text, or direct download URL."}},
        "required": ["target"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.config = BrowserConfig(root)
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, target: str) -> Any:
        if not self.confirm_fn(f"Trigger and save a browser download from '{target}'?"):
            return dict(_DENIED)
        try:
            download_dir = self.config.workspace_root / "downloads"
            download_dir.mkdir(parents=True, exist_ok=True)

            def action(page: Any) -> Any:
                parsed = urlparse(target)
                locator = None
                if parsed.scheme not in {"http", "https", "file"}:
                    locator = page.locator(target)
                    if locator.count() == 0:
                        locator = page.get_by_text(target, exact=False)
                    if locator.count() == 0:
                        return {"error": f"Browser action failed: no download target matched '{target}'"}
                with page.expect_download(timeout=30000) as download_info:
                    if parsed.scheme in {"http", "https", "file"}:
                        print(f"Download started: {target}")
                        page.goto(target, wait_until="commit", timeout=30000)
                    else:
                        print(f"Download started: {target}")
                        locator.first.click(timeout=15000)
                download = download_info.value
                filename = Path(download.suggested_filename or "download").name or "download"
                print(f"Download started: {filename}")
                destination = download_dir / filename
                download.save_as(str(destination))
                size = destination.stat().st_size
                print(f"Download completed: {filename} ({size} bytes)")
                return {
                    "status": "completed",
                    "filename": filename,
                    "path": str(destination.relative_to(self.config.root)).replace("\\", "/"),
                    "size": size,
                    "progress": "start/completion reported; live byte-by-byte progress is not available",
                }

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser download failed: {exc}"}


class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Capture a PNG screenshot of the active browser tab and save it under workspace/screenshots."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.config = BrowserConfig(root)

    def run(self) -> Any:
        try:
            screenshots_dir = self.config.workspace_root / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)

            def action(page: Any) -> Any:
                timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
                screenshot_path = screenshots_dir / f"browser_{timestamp}.png"
                page.screenshot(path=str(screenshot_path), full_page=True)
                relative_path = screenshot_path.relative_to(self.config.root)
                screenshot_url = f"/screenshots/{screenshot_path.name}"
                return {
                    "path": str(relative_path).replace("\\", "/"),
                    "url": screenshot_url,
                    "markdown": f"![Browser screenshot]({screenshot_url})",
                    "tab_id": self.session.active_tab_id(),
                }

            return self.session.run_with_recovery(action)
        except Exception as exc:
            return {"error": f"Browser action failed: {exc}"}


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    return [
        BrowserOpenPageTool(),
        BrowserClickTool(confirm_fn=confirm_fn),
        BrowserFillFieldTool(confirm_fn=confirm_fn),
        ListBrowserTabsTool(),
        SwitchBrowserTabTool(),
        CloseBrowserTabTool(confirm_fn=confirm_fn),
        BrowserGetPageTextTool(),
        BrowserFindLinksTool(),
        BrowserWaitForTextTool(),
        BrowserSelectOptionTool(confirm_fn=confirm_fn),
        BrowserFillFormTool(confirm_fn=confirm_fn),
        BrowserUploadFileTool(confirm_fn=confirm_fn),
        BrowserSaveAsPdfTool(),
        BrowserDownloadFileTool(confirm_fn=confirm_fn),
        BrowserScreenshotTool(),
    ]
