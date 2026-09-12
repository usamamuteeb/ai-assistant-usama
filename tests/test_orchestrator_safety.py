"""Offline tests for orchestrator safety and isolation mechanisms."""
from __future__ import annotations

import concurrent.futures
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.model_router import ChatResult, ToolCall
from src.orchestrator import Orchestrator


class _ScriptedRouter:
    def __init__(self, results):
        self.results = list(results)
        self.calls: list[list[dict]] = []

    def pick_tier(self, _message):
        return "free_api"

    def chat(self, _tier, messages, **_kwargs):
        self.calls.append(copy.deepcopy(messages))
        return self.results.pop(0)


class _ImmediateExecutor:
    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        future.set_result(fn(*args, **kwargs))
        return future


class _TimeoutFuture:
    def result(self, timeout=None):
        raise concurrent.futures.TimeoutError


class _TimeoutExecutor:
    def submit(self, *_args, **_kwargs):
        return _TimeoutFuture()


def _orchestrator(results, tool_output=None):
    instance = object.__new__(Orchestrator)
    instance.settings = SimpleNamespace(
        tool_relevance_filter={
            "enabled": False,
            "max_tools": 10,
            "min_tools": 1,
            "core_tools": set(),
        },
        raw={},
        memory={"max_history_messages": 20},
    )
    instance.router = _ScriptedRouter(results)
    instance.tools = MagicMock()
    instance.tools.anthropic_tools.return_value = [
        {"name": "fake_tool", "description": "test", "input_schema": {"type": "object"}}
    ]
    instance.tools.call.return_value = tool_output if tool_output is not None else {"status": "ok"}
    instance.store = MagicMock()
    instance.store.recent_messages.return_value = []
    instance.vector_memory = MagicMock()
    instance._tool_executor = _ImmediateExecutor()
    instance._last_model_used = {}
    instance._last_images = {}
    return instance


def _tool_result(tool_id="tool-1", payload=None):
    return ChatResult(
        text="",
        tool_calls=[ToolCall(id=tool_id, name="fake_tool", input=payload or {"value": 1})],
        model_used="fake:model",
    )


def _named_tool_result(name: str):
    return ChatResult(
        text="",
        tool_calls=[ToolCall(id="tool-1", name=name, input={"prompt": "test"})],
        model_used="fake:model",
    )


def test_confirmation_denial_stops_after_one_model_call():
    assistant = _orchestrator(
        [_tool_result()],
        {"error": "Action not performed: confirmation denied or not provided."},
    )

    result = assistant._run_tool_loop("free_api", [], assistant.tools.anthropic_tools())

    assert len(assistant.router.calls) == 1
    assert "Waiting for your approval" in result.text


def test_duplicate_tool_calls_are_executed_only_once():
    duplicate_calls = ChatResult(
        text="",
        tool_calls=[
            ToolCall(id="one", name="fake_tool", input={"value": 7}),
            ToolCall(id="two", name="fake_tool", input={"value": 7}),
        ],
        model_used="fake:model",
    )
    assistant = _orchestrator([duplicate_calls, ChatResult(text="done", model_used="fake:model")])

    result = assistant._run_tool_loop("free_api", [], assistant.tools.anthropic_tools())

    assert result.text == "done"
    assistant.tools.call.assert_called_once_with("fake_tool", value=7)


def test_wall_clock_deadline_returns_partial_results_without_waiting():
    assistant = _orchestrator([_tool_result()], {"status": "collected", "value": 42})

    with patch("src.orchestrator.time.monotonic", side_effect=[0.0, 0.0, 46.0]):
        result = assistant._run_tool_loop("free_api", [], assistant.tools.anthropic_tools())

    assert len(assistant.router.calls) == 1
    assert "Stopped after 46s" in result.text
    assert "here's what was gathered so far" in result.text


def test_per_tool_watchdog_reports_timeout_without_blocking():
    assistant = _orchestrator([_tool_result(), ChatResult(text="handled", model_used="fake:model")])
    assistant._tool_executor = _TimeoutExecutor()

    with patch("src.orchestrator.TOOL_CALL_HARD_TIMEOUT_SECONDS", 0.01):
        result = assistant._run_tool_loop("free_api", [], assistant.tools.anthropic_tools())

    assert result.text == "handled"
    second_request = assistant.router.calls[1]
    assert "did not respond within 0.01s" in str(second_request)


def test_only_configured_slow_tool_gets_extended_watchdog():
    assistant = _orchestrator([_named_tool_result("generate_image"), ChatResult(text="done")])
    assistant._tool_executor = _TimeoutExecutor()
    assistant.settings.raw = {"slow_tools": {"generate_image": {"max_seconds": 480}}}

    result = assistant._run_tool_loop("free_api", [], assistant.tools.anthropic_tools())

    assert result.text == "done"
    assert "did not respond within 480s" in str(assistant.router.calls[1])
    assert assistant._slow_tool_timeout_seconds("fake_tool") is None


def test_last_model_and_images_are_isolated_per_session():
    assistant = _orchestrator(
        [
            _tool_result(),
            ChatResult(text="session one", model_used="provider:model-one"),
            ChatResult(text="session two", model_used="provider:model-two"),
        ],
        {"path": "workspace/generated_images/one.png"},
    )

    assistant.handle_message("session-1", "make an image")
    assistant.handle_message("session-2", "hello")

    assert assistant.get_last_model_used("session-1") == "provider:model-one"
    assert assistant.get_last_model_used("session-2") == "provider:model-two"
    assert assistant.get_last_images("session-1") == ["workspace/generated_images/one.png"]
    assert assistant.get_last_images("session-2") == []
