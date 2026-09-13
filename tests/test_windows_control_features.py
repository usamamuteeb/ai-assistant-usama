from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from plugins.windows_control import plugin


def _window(title="Notepad", handle=101, pid=42, rect=None):
    window = MagicMock()
    window.window_text.return_value = title
    window.handle = handle
    window.process_id.return_value = pid
    window.rectangle.return_value = rect or SimpleNamespace(left=10, top=20, width=lambda: 800, height=lambda: 600)
    return window


def test_register_includes_new_tools_and_keeps_risky_manual_split():
    normal = MagicMock(return_value=True)
    normal.manual_confirm = MagicMock(return_value=True)
    with patch.object(plugin, "_WINDOWS", True), patch.object(plugin, "_IMPORT_ERROR", None), patch.object(
        plugin, "_plugin_config", return_value={"force_manual_confirmation": True}
    ):
        tools = {tool.name: tool for tool in plugin.register(normal)}

    assert {"focus_window", "move_window", "launch_and_wait", "click_control_by_id", "type_into_window", "read_active_window"} <= tools.keys()
    assert tools["open_application"].confirm_fn is normal
    assert tools["focus_window"].confirm_fn is normal
    assert tools["close_window"].confirm_fn is normal.manual_confirm
    assert tools["send_keystrokes_fallback"].confirm_fn is normal.manual_confirm


def test_type_into_window_focuses_verified_window_before_typing():
    target = _window("Notepad - test")
    target.descendants.return_value = []
    confirm = MagicMock(return_value=True)
    tool = plugin.TypeIntoWindowTool(confirm)
    with patch.object(plugin, "_find_window", return_value=target):
        result = tool.run("Notepad", "Hello — world")

    assert result["status"] == "typed"
    target.set_focus.assert_called_once_with()
    target.type_keys.assert_called_once_with("Hello — world", with_spaces=True, pause=0.05)


def test_move_window_keeps_omitted_geometry_and_reports_rect():
    target = _window()
    confirm = MagicMock(return_value=True)
    tool = plugin.MoveWindowTool(confirm)
    wrapper = MagicMock()
    with patch.object(plugin, "_find_window", return_value=target), patch.object(
        plugin, "HwndWrapper", return_value=wrapper
    ) as hwnd_wrapper:
        result = tool.run("Notepad", x=100, width=900)

    assert result["status"] == "moved"
    hwnd_wrapper.assert_called_once_with(101)
    wrapper.move_window.assert_called_once_with(x=100, y=20, width=900, height=600)
    assert result["new_rect"] == {"x": 100, "y": 20, "width": 900, "height": 600}


def test_click_control_by_id_targets_automation_id():
    target_window = _window()
    control = MagicMock()
    control.element_info.automation_id = "saveButton"
    target_window.descendants.return_value = [control]
    tool = plugin.ClickControlByIdTool(MagicMock(return_value=True))
    with patch.object(plugin, "_find_window", return_value=target_window):
        result = tool.run("Notepad", "saveButton")

    assert result["status"] == "clicked"
    control.click_input.assert_called_once_with()


def test_launch_and_wait_has_bounded_timeout_error():
    confirm = MagicMock(return_value=True)
    tool = plugin.LaunchAndWaitTool(confirm)
    clock = iter([0.0, 0.0, 31.0])
    with patch.object(plugin, "Application") as application, patch.object(plugin, "_visible_windows", return_value=[]), patch.object(
        plugin.time, "monotonic", side_effect=lambda: next(clock)
    ), patch.object(plugin.time, "sleep"):
        result = tool.run("missing.exe", expected_title="Missing", timeout=50)

    application.return_value.start.assert_called_once_with("missing.exe")
    assert "within 30s" in result["error"]
