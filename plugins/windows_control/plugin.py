"""Windows desktop-control tools, guarded by explicit confirmation for actions."""
from __future__ import annotations

import ctypes
import platform
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.tools.base import Tool

ConfirmFn = Callable[[str], bool]
_DENIED = {"error": "Action not performed: confirmation denied or not provided."}
_WINDOWS = platform.system() == "Windows"
_IMPORT_ERROR: Exception | None = None

if _WINDOWS:
    try:
        import psutil
        import pyautogui
        from pywinauto import Application, Desktop
        from pywinauto.controls.hwndwrapper import HwndWrapper

        # This is intentionally enabled: moving the pointer to a screen corner
        # aborts pyautogui's fallback action.
        pyautogui.FAILSAFE = True
    except Exception as exc:  # pragma: no cover - depends on host dependencies
        _IMPORT_ERROR = exc


def _plugin_config(root: Path) -> dict[str, Any]:
    config_path = root / "config.yaml"
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return raw.get("windows_control", {})


def _visible_windows() -> list[Any]:
    desktop = Desktop(backend="uia")
    windows: list[Any] = []
    for window in desktop.windows():
        try:
            if window.is_visible() and window.window_text().strip():
                windows.append(window)
        except Exception:
            continue
    return windows


def _find_window(title: str) -> Any | None:
    wanted = title.strip().casefold()
    if not wanted:
        return None
    windows = _visible_windows()
    for window in windows:
        try:
            if window.window_text().strip().casefold() == wanted:
                return window
        except Exception:
            continue
    for window in windows:
        try:
            if wanted in window.window_text().strip().casefold():
                return window
        except Exception:
            continue
    return None


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "unknown"


def _window_controls(window: Any) -> list[dict[str, str]]:
    """Return visible, user-facing controls from a pywinauto element tree."""
    controls: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for control in window.descendants():
        try:
            if not control.is_visible():
                continue
            control_type = str(getattr(control.element_info, "control_type", "Unknown"))
            text = control.window_text().strip()
            if not text or control_type not in {
                "Text", "Button", "Edit", "ComboBox", "CheckBox", "RadioButton", "ListItem"
            }:
                continue
            key = (control_type, text)
            if key not in seen:
                seen.add(key)
                controls.append({"control_type": control_type, "text": text})
        except Exception:
            continue
    return controls


def _active_window() -> Any | None:
    """Find the visible pywinauto top-level wrapper for the foreground HWND."""
    foreground = int(ctypes.windll.user32.GetForegroundWindow())
    if not foreground:
        return None
    for window in _visible_windows():
        try:
            if int(window.handle) == foreground:
                return window
        except Exception:
            continue
    return None


class ListOpenWindowsTool(Tool):
    name = "list_open_windows"
    description = "List all visible top-level Windows desktop windows with title, process name, PID, and active status."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        try:
            active_handle = ctypes.windll.user32.GetForegroundWindow()
            rows = []
            for window in _visible_windows():
                pid = int(window.process_id())
                rows.append(
                    {
                        "title": window.window_text().strip(),
                        "process_name": _process_name(pid),
                        "pid": pid,
                        "is_active": int(window.handle) == int(active_handle),
                    }
                )
            return {"count": len(rows), "windows": rows}
        except Exception as exc:
            return {"error": f"Could not list open windows: {exc}"}


class ReadWindowTextTool(Tool):
    name = "read_window_text"
    description = "Read visible labels, buttons, and text fields from a top-level Windows desktop window matched by exact or partial title."
    input_schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "Exact or partial title of the window."}},
        "required": ["title"],
    }

    def run(self, title: str) -> Any:
        try:
            window = _find_window(title)
            if window is None:
                return {"error": f"No window matching '{title}' found."}

            return {"title": window.window_text().strip(), "controls": _window_controls(window)}
        except Exception as exc:
            return {"error": f"Could not read window text: {exc}"}


class _ConfirmedWindowsTool(Tool):
    def __init__(self, confirm_fn: Optional[ConfirmFn] = None):
        self.confirm_fn = confirm_fn or (lambda _: False)

    def _confirmed(self, message: str) -> bool:
        return bool(self.confirm_fn(message))


class OpenApplicationTool(_ConfirmedWindowsTool):
    name = "open_application"
    description = "Launch a Windows application by executable name or program path, following the global approval mode."
    input_schema = {
        "type": "object",
        "properties": {"application": {"type": "string", "description": "Program path or known executable name, for example notepad.exe."}},
        "required": ["application"],
    }

    def run(self, application: str) -> Any:
        if not self._confirmed(f"Open Windows application '{application}'?"):
            return _DENIED
        try:
            Application(backend="uia").start(application)
            return {"status": "launched", "application": application}
        except Exception as exc:
            return {"error": f"Could not open application '{application}': {exc}"}


class ClickWindowControlTool(_ConfirmedWindowsTool):
    name = "click_window_control"
    description = "Click a Windows control matched by window title plus control text or automation ID, following the global approval mode."
    input_schema = {
        "type": "object",
        "properties": {
            "window_title": {"type": "string", "description": "Exact or partial title of the target window."},
            "control_text": {"type": "string", "description": "Visible control text (exact or partial)."},
            "automation_id": {"type": "string", "description": "Optional UI Automation ID of the control."},
        },
        "required": ["window_title"],
    }

    def run(self, window_title: str, control_text: str = "", automation_id: str = "") -> Any:
        if not control_text.strip() and not automation_id.strip():
            return {"error": "Provide control_text or automation_id."}
        target_description = automation_id or control_text
        if not self._confirmed(f"Click control '{target_description}' in window '{window_title}'?"):
            return _DENIED
        try:
            window = _find_window(window_title)
            if window is None:
                return {"error": f"No window matching '{window_title}' found."}
            controls = window.descendants()
            target = None
            if automation_id.strip():
                target = next(
                    (c for c in controls if str(getattr(c.element_info, "automation_id", "")) == automation_id),
                    None,
                )
            else:
                wanted = control_text.strip().casefold()
                target = next((c for c in controls if c.window_text().strip().casefold() == wanted), None)
                if target is None:
                    target = next((c for c in controls if wanted in c.window_text().strip().casefold()), None)
            if target is None:
                return {"error": f"No control matching '{target_description}' found in '{window.window_text().strip()}'."}
            target.click_input()
            return {
                "status": "clicked",
                "window_title": window.window_text().strip(),
                "control_text": target.window_text().strip(),
                "automation_id": str(getattr(target.element_info, "automation_id", "")),
            }
        except Exception as exc:
            return {"error": f"Could not click window control: {exc}"}


class FocusWindowTool(_ConfirmedWindowsTool):
    name = "focus_window"
    description = "Bring a visible Windows desktop window matched by exact or partial title to the foreground, following the global approval mode."
    input_schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "Exact or partial window title."}},
        "required": ["title"],
    }

    def run(self, title: str) -> Any:
        try:
            window = _find_window(title)
            if window is None:
                return {"error": f"No window matching '{title}' found."}
            if not self._confirmed(f"Focus window '{window.window_text().strip()}'?"):
                return _DENIED
            window.set_focus()
            return {"status": "focused", "title": window.window_text().strip(), "handle": int(window.handle)}
        except Exception as exc:
            return {"error": f"Could not focus window '{title}': {exc}"}


class MoveWindowTool(_ConfirmedWindowsTool):
    name = "move_window"
    description = "Reposition and/or resize a Windows desktop window. Omitted geometry values keep their current values; follows the global approval mode."
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Exact or partial window title."},
            "x": {"type": "integer", "description": "Optional left coordinate."},
            "y": {"type": "integer", "description": "Optional top coordinate."},
            "width": {"type": "integer", "description": "Optional width."},
            "height": {"type": "integer", "description": "Optional height."},
        },
        "required": ["title"],
    }

    def run(
        self,
        title: str,
        x: Optional[int] = None,
        y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> Any:
        try:
            window = _find_window(title)
            if window is None:
                return {"error": f"No window matching '{title}' found."}
            rect = window.rectangle()
            new_x = int(rect.left if x is None else x)
            new_y = int(rect.top if y is None else y)
            new_width = int(rect.width() if width is None else width)
            new_height = int(rect.height() if height is None else height)
            if new_width <= 0 or new_height <= 0:
                return {"error": "Window width and height must be positive."}
            if not self._confirmed(
                f"Move/resize window '{window.window_text().strip()}' to "
                f"({new_x}, {new_y}, {new_width}x{new_height})?"
            ):
                return _DENIED
            # UIAWrapper exposes reliable geometry reads but pywinauto's
            # move_window implementation is provided by its Win32 wrapper.
            # Re-wrap the same verified HWND instead of using coordinates or
            # looking up an unrelated window.
            HwndWrapper(int(window.handle)).move_window(
                x=new_x, y=new_y, width=new_width, height=new_height
            )
            return {
                "status": "moved",
                "title": window.window_text().strip(),
                "old_rect": {"x": int(rect.left), "y": int(rect.top), "width": int(rect.width()), "height": int(rect.height())},
                "new_rect": {"x": new_x, "y": new_y, "width": new_width, "height": new_height},
            }
        except Exception as exc:
            return {"error": f"Could not move window '{title}': {exc}"}


class LaunchAndWaitTool(_ConfirmedWindowsTool):
    name = "launch_and_wait"
    description = "Launch a Windows application and wait for a visible window matching its expected title or process name, following the global approval mode."
    input_schema = {
        "type": "object",
        "properties": {
            "application": {"type": "string", "description": "Executable name or program path."},
            "expected_title": {"type": "string", "description": "Optional exact or partial expected window title."},
            "expected_process": {"type": "string", "description": "Optional expected process name, with or without .exe."},
            "timeout": {"type": "number", "description": "Wait time in seconds, default 15, capped at 30."},
        },
        "required": ["application"],
    }

    def run(
        self,
        application: str,
        expected_title: str = "",
        expected_process: str = "",
        timeout: float = 15,
    ) -> Any:
        effective_timeout = max(0.0, min(float(timeout), 30.0))
        process_hint = expected_process.strip().casefold()
        if process_hint and not process_hint.endswith(".exe"):
            process_hint += ".exe"
        if not expected_title.strip() and not process_hint:
            process_hint = Path(application.strip().strip('"')).name.casefold()
        if not self._confirmed(f"Launch '{application}' and wait for its window?"):
            return _DENIED
        try:
            Application(backend="uia").start(application)
            deadline = time.monotonic() + effective_timeout
            while True:
                for window in _visible_windows():
                    try:
                        title = window.window_text().strip()
                        title_matches = bool(expected_title.strip()) and expected_title.strip().casefold() in title.casefold()
                        process_matches = bool(process_hint) and _process_name(int(window.process_id())).casefold() == process_hint
                        if title_matches or process_matches:
                            return {
                                "status": "ready",
                                "application": application,
                                "title": title,
                                "handle": int(window.handle),
                                "pid": int(window.process_id()),
                                "process_name": _process_name(int(window.process_id())),
                            }
                    except Exception:
                        continue
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
            return {
                "error": f"Launched '{application}' but no matching window appeared within {effective_timeout:g}s — it may still be starting, or the window title doesn't match what was expected."
            }
        except Exception as exc:
            return {"error": f"Could not launch application '{application}': {exc}"}


class ClickControlByIdTool(_ConfirmedWindowsTool):
    name = "click_control_by_id"
    description = "Click a Windows control by its pywinauto automation ID within a window matched by exact or partial title, following the global approval mode."
    input_schema = {
        "type": "object",
        "properties": {
            "window_title": {"type": "string", "description": "Exact or partial window title."},
            "automation_id": {"type": "string", "description": "The UI Automation automation_id of the target control."},
        },
        "required": ["window_title", "automation_id"],
    }

    def run(self, window_title: str, automation_id: str) -> Any:
        try:
            window = _find_window(window_title)
            if window is None:
                return {"error": f"No window matching '{window_title}' found."}
            target = next(
                (control for control in window.descendants()
                 if str(getattr(control.element_info, "automation_id", "")) == automation_id),
                None,
            )
            if target is None:
                return {"error": f"No control with automation_id '{automation_id}' found in '{window.window_text().strip()}'."}
            if not self._confirmed(f"Click automation ID '{automation_id}' in window '{window.window_text().strip()}'?"):
                return _DENIED
            target.click_input()
            return {"status": "clicked", "window_title": window.window_text().strip(), "automation_id": automation_id}
        except Exception as exc:
            return {"error": f"Could not click control by automation ID '{automation_id}': {exc}"}


class TypeIntoWindowTool(_ConfirmedWindowsTool):
    name = "type_into_window"
    description = "Focus a specifically verified Windows window by title and type into that window with pywinauto; it never types into an arbitrary currently focused app."
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Exact or partial title of the target window."},
            "text": {"type": "string", "description": "Text to type into the verified target window."},
        },
        "required": ["title", "text"],
    }

    def run(self, title: str, text: str) -> Any:
        try:
            window = _find_window(title)
            if window is None:
                return {"error": f"No window matching '{title}' found; text was not sent."}
            # Standard confirmation is deliberate: the title is verified and pywinauto
            # sends to that window, unlike the blind-focus fallback tool.
            if not self._confirmed(f"Type text into verified window '{window.window_text().strip()}'?"):
                return _DENIED
            # Prefer the document/edit element so modern apps such as Notepad
            # receive text in their editor, while the top-level title remains
            # the verified safety boundary.
            editable = next(
                (
                    control
                    for control in window.descendants()
                    if str(getattr(control.element_info, "control_type", "")) in {"Edit", "Document"}
                    and control.is_visible()
                ),
                None,
            )
            target = editable or window
            target.set_focus()
            # A small inter-key pause matters for UIA-backed editors (notably
            # current Notepad); without it, slow native controls can collapse
            # or repeat characters even though focus was correct.
            target.type_keys(text, with_spaces=True, pause=0.05)
            return {"status": "typed", "title": window.window_text().strip(), "characters": len(text)}
        except Exception as exc:
            return {"error": f"Could not type into verified window '{title}': {exc}"}


class ReadActiveWindowTool(Tool):
    name = "read_active_window"
    description = "Read the foreground Windows desktop window's title, process details, and visible control text."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        try:
            window = _active_window()
            if window is None:
                return {"error": "No visible active Windows desktop window was found."}
            pid = int(window.process_id())
            return {
                "title": window.window_text().strip(),
                "process_name": _process_name(pid),
                "pid": pid,
                "handle": int(window.handle),
                "controls": _window_controls(window),
            }
        except Exception as exc:
            return {"error": f"Could not read the active window: {exc}"}


class CloseWindowTool(_ConfirmedWindowsTool):
    name = "close_window"
    description = "Close a Windows desktop window by title after explicit confirmation. Closing may discard unsaved work."
    input_schema = {
        "type": "object",
        "properties": {"title": {"type": "string", "description": "Exact or partial title of the window to close."}},
        "required": ["title"],
    }

    def run(self, title: str) -> Any:
        if not self._confirmed(f"Close window '{title}'? Unsaved work could be lost."):
            return _DENIED
        try:
            window = _find_window(title)
            if window is None:
                return {"error": f"No window matching '{title}' found."}
            matched_title = window.window_text().strip()
            window.close()
            return {"status": "closed", "title": matched_title}
        except Exception as exc:
            return {"error": f"Could not close window '{title}': {exc}"}


class SendKeystrokesFallbackTool(_ConfirmedWindowsTool):
    name = "send_keystrokes_fallback"
    description = "Fallback tool to type text or special keys into the currently focused Windows window after confirmation."
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Raw text to type into the currently focused window."},
            "special_keys": {"type": "array", "items": {"type": "string"}, "description": "Optional key names for pyautogui.hotkey, such as ['ctrl', 's']."},
        },
        "required": [],
    }

    def run(self, text: str = "", special_keys: Optional[list[str]] = None) -> Any:
        keys = special_keys or []
        if not text and not keys:
            return {"error": "Provide text or special_keys."}
        action = f"type text into the CURRENTLY FOCUSED window" if text else f"send keys {keys} to the CURRENTLY FOCUSED window"
        if not self._confirmed(f"Use keystroke fallback to {action}? This does not target a specific application."):
            return _DENIED
        try:
            pyautogui.FAILSAFE = True
            if text:
                pyautogui.write(text)
            if keys:
                pyautogui.hotkey(*keys)
            return {"status": "sent", "typed_text": bool(text), "special_keys": keys}
        except Exception as exc:
            return {"error": f"Could not send fallback keystrokes: {exc}"}


def _forced_manual_confirmation(root: Path, confirm_fn: Optional[ConfirmFn]) -> ConfirmFn:
    """Return the browser's real-manual path for the two high-risk tools only."""
    config = _plugin_config(root)
    if not config.get("force_manual_confirmation", True):
        return confirm_fn or (lambda _: False)

    manual_confirm = getattr(confirm_fn, "manual_confirm", None)
    if callable(manual_confirm):
        return manual_confirm

    # No browser/manual bridge was supplied (for example, an unattended job).
    # Fail closed rather than allowing an automatic callback to control the desktop.
    print("windows_control: force_manual_confirmation is enabled but no manual confirmation bridge is available; actions will be denied.")
    return lambda _: False


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    if not _WINDOWS:
        print("windows_control: plugin skipped because the host platform is not Windows.")
        return []
    if _IMPORT_ERROR is not None:
        print(f"windows_control: plugin skipped because dependencies could not be imported: {_IMPORT_ERROR}")
        return []

    root = Path(__file__).resolve().parents[2]
    normal_confirm = confirm_fn or (lambda _: False)
    risky_confirm = _forced_manual_confirmation(root, confirm_fn)
    return [
        ListOpenWindowsTool(),
        ReadWindowTextTool(),
        OpenApplicationTool(confirm_fn=normal_confirm),
        ClickWindowControlTool(confirm_fn=normal_confirm),
        FocusWindowTool(confirm_fn=normal_confirm),
        MoveWindowTool(confirm_fn=normal_confirm),
        LaunchAndWaitTool(confirm_fn=normal_confirm),
        ClickControlByIdTool(confirm_fn=normal_confirm),
        TypeIntoWindowTool(confirm_fn=normal_confirm),
        ReadActiveWindowTool(),
        CloseWindowTool(confirm_fn=risky_confirm),
        SendKeystrokesFallbackTool(confirm_fn=risky_confirm),
    ]
