"""Browser automation plugin using Playwright."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.tools.base import Tool

ConfirmFn = Callable[[str], bool]
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BrowserConfig:
    def __init__(self, root: Path):
        self.root = root
        self._raw = self._load_config()

    def _load_config(self) -> dict[str, Any]:
        config_path = self.root / "config.yaml"
        if not config_path.exists():
            return {}
        with config_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def browser_data(self) -> dict[str, Any]:
        return self._raw.get("browser", {}) if isinstance(self._raw.get("browser", {}), dict) else {}

    @property
    def headless(self) -> bool:
        value = self.browser_data.get("headless", True)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @property
    def workspace_root(self) -> Path:
        filesystem = self._raw.get("filesystem_tool", {}) if isinstance(self._raw.get("filesystem_tool", {}), dict) else {}
        configured = filesystem.get("workspace_root", "workspace")
        path = Path(configured)
        return path if path.is_absolute() else self.root / path


class BrowserSession:
    _instance: Optional["BrowserSession"] = None

    def __init__(self, root: Path):
        self.root = root
        self.config = BrowserConfig(root)
        self._playwright = None
        self._browser = None
        self._page = None

    @classmethod
    def get_instance(cls, root: Path) -> "BrowserSession":
        if cls._instance is None:
            cls._instance = cls(root)
        return cls._instance

    def get_page(self):
        if self._page is not None and self._browser is not None:
            return self._page

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.config.headless)
        self._page = self._browser.new_page()
        return self._page

    def close(self) -> None:
        if self._page is not None:
            self._page.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._browser = None
        self._playwright = None


class BrowserOpenPageTool(Tool):
    name = "browser_open_page"
    description = "Open a page in the shared browser and return the title, visible text preview, and a few links."
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to open in the browser."},
        },
        "required": ["url"],
    }

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)

    def run(self, url: str) -> Any:
        try:
            page = self.session.get_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            title = page.title()
            body_text = page.locator("body").inner_text()
            visible_text = " ".join((body_text or "").split())[:3000]
            links = page.locator("a[href]").evaluate_all(
                """
                (elements) => elements.slice(0, 20).map((element) => ({
                    text: (element.textContent || '').trim(),
                    href: (element.getAttribute('href') || '').trim(),
                })).filter((link) => link.text || link.href)
                """
            )
            return {"title": title, "text": visible_text, "links": links}
        except Exception as exc:  # pragma: no cover - runtime browser/platform dependency path
            return {"error": f"Browser action failed: {exc}"}


class BrowserClickTool(Tool):
    name = "browser_click"
    description = "Click an element on the current page by CSS selector or by its visible text. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "CSS selector or visible text of the element to click.",
            }
        },
        "required": ["target"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda message: False)

    def run(self, target: str) -> Any:
        if not self.confirm_fn(f"Click '{target}' on the current browser page?"):
            return {"error": "Action not performed: confirmation denied or not provided."}

        try:
            page = self.session.get_page()
            locator = page.locator(target)
            if locator.count() > 0:
                locator.first.click(timeout=15000)
                return {"status": "clicked", "target": target}

            text_locator = page.get_by_text(target, exact=False)
            if text_locator.count() > 0:
                text_locator.first.click(timeout=15000)
                return {"status": "clicked", "target": target}

            return {"error": f"Browser action failed: no element matched '{target}'"}
        except Exception as exc:  # pragma: no cover - runtime browser/platform dependency path
            return {"error": f"Browser action failed: {exc}"}


class BrowserFillFieldTool(Tool):
    name = "browser_fill_field"
    description = "Fill a form field on the current page by CSS selector and optionally press Enter. Requires confirmation."
    input_schema = {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector for the input element."},
            "value": {"type": "string", "description": "Value to enter into the field."},
            "submit": {"type": "boolean", "description": "Press Enter after filling the field if true.", "default": False},
        },
        "required": ["selector", "value"],
    }

    def __init__(self, confirm_fn: Optional[ConfirmFn] = None, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.confirm_fn = confirm_fn or (lambda message: False)

    def run(self, selector: str, value: str, submit: bool = False) -> Any:
        if not self.confirm_fn(f"Fill '{selector}' on the current browser page?"):
            return {"error": "Action not performed: confirmation denied or not provided."}

        try:
            page = self.session.get_page()
            field = page.locator(selector)
            if field.count() == 0:
                return {"error": f"Browser action failed: no element matched '{selector}'"}
            field.fill(value, timeout=15000)
            if submit:
                field.press("Enter")
            return {"status": "filled", "selector": selector, "submitted": submit}
        except Exception as exc:  # pragma: no cover - runtime browser/platform dependency path
            return {"error": f"Browser action failed: {exc}"}


class BrowserScreenshotTool(Tool):
    name = "browser_screenshot"
    description = "Capture a PNG screenshot of the current browser page and save it under the configured workspace screenshots folder."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.session = BrowserSession.get_instance(root)
        self.config = BrowserConfig(root)

    def run(self) -> Any:
        try:
            page = self.session.get_page()
            workspace_root = self.config.workspace_root
            screenshots_dir = workspace_root / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            screenshot_path = screenshots_dir / f"browser_{timestamp}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)

            relative_path = screenshot_path.relative_to(self.config.root)
            # The NiceGUI app serves this directory at /browser-screenshots.
            # Keep the filesystem path for non-web callers, but return a
            # browser-safe URL too so an assistant response can embed it.
            screenshot_url = f"/browser-screenshots/{screenshot_path.name}"
            return {
                "path": str(relative_path).replace("\\", "/"),
                "url": screenshot_url,
                "markdown": f"![Browser screenshot]({screenshot_url})",
            }
        except Exception as exc:  # pragma: no cover - runtime browser/platform dependency path
            return {"error": f"Browser action failed: {exc}"}


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    return [
        BrowserOpenPageTool(),
        BrowserClickTool(confirm_fn=confirm_fn),
        BrowserFillFieldTool(confirm_fn=confirm_fn),
        BrowserScreenshotTool(),
    ]
