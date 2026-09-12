"""Offline smoke, schema, and confirmation tests for external-boundary plugins."""
from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock, patch

import pytest

from src.tools.base import Tool


PLUGIN_NAMES = [
    "browser",
    "clipboard",
    "dev_tools",
    "google_workspace",
    "image_generation",
    "screen_reader",
    "windows_control",
]


def _load_plugin(name):
    if name == "screen_reader":
        import pytesseract

        with patch.object(pytesseract, "get_tesseract_version", return_value="mock-version"):
            return importlib.import_module("plugins.screen_reader.plugin")
    return importlib.import_module(f"plugins.{name}.plugin")


def _register(name, confirm_fn=None):
    module = _load_plugin(name)
    if name == "windows_control":
        with patch.object(module, "_WINDOWS", True), patch.object(module, "_IMPORT_ERROR", None):
            return module.register(confirm_fn=confirm_fn)
    return module.register(confirm_fn=confirm_fn)


@pytest.mark.parametrize("plugin_name", PLUGIN_NAMES)
def test_plugin_register_returns_tools(plugin_name):
    tools = _register(plugin_name)

    assert tools
    assert all(isinstance(tool, Tool) for tool in tools)


@pytest.mark.parametrize("plugin_name", PLUGIN_NAMES)
def test_plugin_schemas_are_well_formed_json(plugin_name):
    for tool in _register(plugin_name):
        schema = tool.to_anthropic_schema()
        assert schema["name"]
        assert schema["description"]
        assert schema["input_schema"]["type"] == "object"
        json.dumps(schema)


def test_browser_confirm_gated_tools_deny_before_playwright_access():
    confirm = MagicMock(return_value=False)
    tools = {tool.name: tool for tool in _register("browser", confirm)}
    with patch.object(tools["browser_click"].session, "get_page") as get_page:
        click = tools["browser_click"].run(target="Submit")
        fill = tools["browser_fill_field"].run(selector="#query", value="hello")

    assert "confirmation denied or not provided." in click["error"]
    assert "confirmation denied or not provided." in fill["error"]
    get_page.assert_not_called()


def test_google_confirm_gated_tools_deny_before_api_client_access():
    confirm = MagicMock(return_value=False)
    tools = {tool.name: tool for tool in _register("google_workspace", confirm)}
    with patch.object(tools["gmail_send_email"].service, "gmail_service") as gmail, patch.object(
        tools["calendar_create_event"].service, "calendar_service"
    ) as calendar:
        email = tools["gmail_send_email"].run(to="a@example.com", subject="Hi", body="Body")
        event = tools["calendar_create_event"].run(
            title="Meeting", start="2026-01-01T10:00:00Z", end="2026-01-01T11:00:00Z"
        )

    assert "confirmation denied or not provided." in email["error"]
    assert "confirmation denied or not provided." in event["error"]
    gmail.assert_not_called()
    calendar.assert_not_called()


def test_windows_risky_tools_deny_before_desktop_access():
    module = _load_plugin("windows_control")
    confirm = MagicMock(return_value=False)
    with patch.object(module, "_WINDOWS", True), patch.object(module, "_IMPORT_ERROR", None), patch.object(
        module, "_plugin_config", return_value={"force_manual_confirmation": False}
    ):
        tools = {tool.name: tool for tool in module.register(confirm_fn=confirm)}
    with patch.object(module, "_find_window") as find_window, patch.object(module.pyautogui, "write") as write:
        closed = tools["close_window"].run(title="Untitled")
        typed = tools["send_keystrokes_fallback"].run(text="hello")

    assert "confirmation denied or not provided." in closed["error"]
    assert "confirmation denied or not provided." in typed["error"]
    find_window.assert_not_called()
    write.assert_not_called()


def test_dev_tools_manual_actions_deny_before_writing(tmp_path):
    module = _load_plugin("dev_tools")
    confirm = MagicMock(return_value=False)
    tool = module.WriteAnyFileTool(confirm_fn=confirm, protected_prefixes=[])
    target = tmp_path / "outside-workspace.txt"

    result = tool.run(str(target), "do not write")

    assert result == {"error": "Action not performed: confirmation denied or not provided."}
    assert not target.exists()


def test_dev_tools_protected_path_is_refused_before_confirmation(tmp_path):
    module = _load_plugin("dev_tools")
    confirm = MagicMock(return_value=True)
    protected = tmp_path / "protected"
    tool = module.WriteAnyFileTool(confirm_fn=confirm, protected_prefixes=[str(protected)])

    result = tool.run(str(protected / "blocked.txt"), "blocked")

    assert result == {"error": "Refused: this path is in the protected-paths blocklist in config.yaml."}
    confirm.assert_not_called()


def test_dev_tools_searches_text_without_confirmation(tmp_path):
    module = _load_plugin("dev_tools")
    (tmp_path / "nested").mkdir()
    expected = tmp_path / "nested" / "target.py"
    expected.write_text("NEEDLE = 'find me'", encoding="utf-8")
    (tmp_path / "ignored.bin").write_bytes(b"\x00NEEDLE")

    result = module.SearchFilesTool().run(str(tmp_path), filename_glob="*.py", text="NEEDLE")

    assert result["count"] == 1
    assert result["matches"] == [str(expected)]


def test_dev_tools_rejects_paths_outside_configured_allowed_root(tmp_path):
    module = _load_plugin("dev_tools")
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()

    result = module.ListAnyDirectoryTool(allowed_root=str(allowed)).run(str(outside))

    assert "outside the permitted root" in result["error"]


def test_dev_tools_code_error_keeps_stdout_stderr_shape(tmp_path):
    module = _load_plugin("dev_tools")
    tool = module.RunCodeTool(confirm_fn=MagicMock(return_value=True))
    expected = {"exit_code": 1, "stdout": "", "stderr": "Traceback: test"}

    with patch.object(module, "_run_process", return_value=dict(expected)):
        result = tool.run("python", "raise RuntimeError('test')", str(tmp_path))

    assert result == {**expected, "language": "python", "working_directory": str(tmp_path)}


def test_playwright_session_start_is_mocked(tmp_path):
    module = _load_plugin("browser")
    session = module.BrowserSession(tmp_path)
    fake_playwright = MagicMock()
    fake_page = MagicMock()
    fake_playwright.chromium.launch.return_value.new_page.return_value = fake_page
    with patch("playwright.sync_api.sync_playwright") as sync_playwright:
        sync_playwright.return_value.start.return_value = fake_playwright
        page = session.get_page()

    assert page is fake_page
    fake_playwright.chromium.launch.assert_called_once()


def test_clipboard_calls_are_mocked():
    module = _load_plugin("clipboard")
    tools = {tool.name: tool for tool in module.register()}
    with patch.object(module.pyperclip, "paste", return_value="copied") as paste, patch.object(
        module.pyperclip, "copy"
    ) as copy:
        assert tools["read_clipboard"].run() == {"content": "copied"}
        assert tools["write_clipboard"].run("new") == {"status": "written", "length": 3}

    paste.assert_called_once()
    copy.assert_called_once_with("new")


def test_google_search_shapes_mocked_api_response():
    tools = {tool.name: tool for tool in _register("google_workspace")}
    gmail = MagicMock()
    messages_api = gmail.users.return_value.messages.return_value
    messages_api.list.return_value.execute.return_value = {"messages": []}
    with patch.object(tools["gmail_search_messages"].service, "gmail_service", return_value=gmail):
        result = tools["gmail_search_messages"].run(query="is:unread", max_results=5)

    assert result == {"query": "is:unread", "count": 0, "results": []}


def test_image_generation_handles_mocked_quota_response():
    module = _load_plugin("image_generation")
    tool = module.GenerateImageTool()
    tool.api_key = "fake-key"
    response = MagicMock(status_code=429, text="quota")
    response.json.return_value = {"error": {"message": "quota"}}
    with patch.object(module.requests, "post", return_value=response) as post:
        result = tool.run("draw a test image")

    assert "isn't available" in result["error"]
    post.assert_called_once()


def test_screen_reader_uses_mocked_capture_and_ocr():
    module = _load_plugin("screen_reader")
    tool = module.CaptureScreenTextTool()
    fake_image = object()
    with patch.object(module, "_OCR_IMPORT_ERROR", None), patch.object(
        module.ImageGrab, "grab", return_value=fake_image
    ) as grab, patch.object(module.pytesseract, "image_to_string", return_value="Visible text") as ocr:
        result = tool.run()

    assert result == {"text": "Visible text"}
    grab.assert_called_once()
    ocr.assert_called_once_with(fake_image)


def test_windows_window_listing_uses_mocked_desktop_and_psutil():
    module = _load_plugin("windows_control")
    window = MagicMock(handle=123)
    window.is_visible.return_value = True
    window.window_text.return_value = "Editor"
    window.process_id.return_value = 42
    process = MagicMock()
    process.name.return_value = "editor.exe"
    with patch.object(module, "Desktop") as desktop, patch.object(
        module.ctypes.windll.user32, "GetForegroundWindow", return_value=123
    ), patch.object(module.psutil, "Process", return_value=process):
        desktop.return_value.windows.return_value = [window]
        result = module.ListOpenWindowsTool().run()

    assert result == {
        "count": 1,
        "windows": [{"title": "Editor", "process_name": "editor.exe", "pid": 42, "is_active": True}],
    }
