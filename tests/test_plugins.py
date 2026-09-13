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
    "file_automation",
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


def test_image_generation_reports_unavailable_local_server():
    module = _load_plugin("image_generation")
    tool = module.GenerateImageTool()
    tool.config = {"auto_start": False, "start_timeout_seconds": 1}
    with patch.object(module.requests, "get", side_effect=module.requests.ConnectionError("offline")):
        result = tool.run("draw a test image")

    assert "Local image generation is unavailable" in result["error"]


def test_image_generation_refuses_when_free_memory_is_below_configured_minimum():
    module = _load_plugin("image_generation")
    tool = module.GenerateImageTool()
    tool.image_generation_config = {"generate_image_min_free_ram_gb": 1.5}
    memory = MagicMock(available=1024**3)
    with patch.object(module.psutil, "virtual_memory", return_value=memory), patch.object(
        tool, "_ensure_server_ready"
    ) as ready:
        result = tool.run("draw a test image")

    assert "Only 1.0GB RAM free" in result["error"]
    assert "at least 1.5GB" in result["error"]
    ready.assert_not_called()


def test_image_generation_starts_one_local_setup_process(tmp_path):
    module = _load_plugin("image_generation")
    tool = module.GenerateImageTool(root=tmp_path)
    tool.config = {"auto_start": True, "auto_install": True, "install_dir": "data/comfyui"}
    with patch.object(tool, "_server_ready", return_value=False), patch.object(
        module.subprocess, "Popen"
    ) as start:
        start.return_value.pid = 1234
        tool.start_if_configured()

    assert start.call_args.args[0][-1] == "src.comfyui_setup"
    assert (tmp_path / "data" / "comfyui" / ".setup.lock").is_file()


def test_image_generation_exposes_failed_setup_status(tmp_path):
    module = _load_plugin("image_generation")
    setup_dir = tmp_path / "data" / "comfyui"
    setup_dir.mkdir(parents=True)
    (setup_dir / "setup_status.json").write_text(
        '{"state": "failed", "phase": "source", "detail": "Connection reset", "updated_at": "now"}',
        encoding="utf-8",
    )
    (setup_dir / "setup.log").write_text("clone failed\nconnection reset\n", encoding="utf-8")
    tool = module.GenerateImageTool(root=tmp_path)
    tool.config = {"install_dir": "data/comfyui"}
    with patch.object(tool, "_server_ready", return_value=False):
        status = tool.get_setup_status()

    assert status["state"] == "failed"
    assert status["phase"] == "source"
    assert status["can_retry"] is True
    assert status["log_tail"] == ["clone failed", "connection reset"]


def test_image_generation_does_not_wait_for_background_setup(tmp_path):
    module = _load_plugin("image_generation")
    tool = module.GenerateImageTool(root=tmp_path)
    tool.config = {"auto_start": True, "start_timeout_seconds": 120}
    with patch.object(tool, "_server_ready", return_value=False), patch.object(
        tool, "start_if_configured"
    ) as start:
        tool._start_error = "Initial local ComfyUI setup has started."
        result = tool._ensure_server_ready()

    assert "Initial local ComfyUI setup has started" in result
    start.assert_called_once()


def test_image_generation_saves_mocked_local_comfyui_output(tmp_path):
    module = _load_plugin("image_generation")
    tool = module.GenerateImageTool(root=tmp_path)
    tool.config = {
        "url": "http://127.0.0.1:8188",
        "checkpoint": "test.safetensors",
        "timeout_seconds": 1,
    }
    prompt_response = MagicMock(status_code=200)
    prompt_response.json.return_value = {"prompt_id": "request-1"}
    history_response = MagicMock(status_code=200)
    history_response.json.return_value = {
        "request-1": {"outputs": {"7": {"images": [{"filename": "result.png", "subfolder": "", "type": "output"}]}}}
    }
    image_response = MagicMock(status_code=200, content=b"fake-image")
    image_response.headers = {"content-type": "image/png"}
    ready_response = MagicMock(status_code=200)
    with patch.object(module.requests, "post", return_value=prompt_response), patch.object(
        module.requests, "get", side_effect=[ready_response, history_response, image_response]
    ):
        result = tool.run("a local test image")

    assert result["backend"] == "local_comfyui"
    assert result["model"] == "test.safetensors"
    assert (tmp_path / "workspace" / result["path"]).read_bytes() == b"fake-image"


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
