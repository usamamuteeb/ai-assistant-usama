"""Offline model-router coverage; all HTTP boundaries are mocked."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from src.model_router import ChatResult, GeminiBackend, GroqBackend, ModelRouter


def _settings(*, chain=None, allow_premium=True):
    return SimpleNamespace(
        groq_api_key="test-groq-key",
        gemini_api_key="test-gemini-key",
        free_api_model={"max_tokens": 128, "chain": chain or []},
        raw={"models": {"groq": {"base_url": "https://groq.test/v1"}}},
        routing={
            "premium_keywords": ["analyze"],
            "long_message_char_threshold": 400,
            "default_tier": "free_api",
        },
        allow_premium=allow_premium,
    )


def _response(status_code, data=None, headers=None):
    response = MagicMock(status_code=status_code, headers=headers or {})
    response.json.return_value = data or {}
    response.raise_for_status.return_value = None
    return response


def test_groq_chat_parses_text_tool_calls_and_model_used():
    backend = GroqBackend(_settings())
    payload = {
        "choices": [{
            "finish_reason": "tool_calls",
            "message": {
                "content": "I will check that.",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "lookup", "arguments": '{"query":"status"}'},
                }],
            },
        }],
    }
    with patch("src.model_router.requests.post", return_value=_response(200, payload)) as post:
        result = backend.chat([{"role": "user", "content": "check"}], model="test-model")

    assert result.text == "I will check that."
    assert result.model_used == "test-model"
    assert [(tool.id, tool.name, tool.input) for tool in result.tool_calls] == [
        ("call-1", "lookup", {"query": "status"})
    ]
    assert post.call_args.kwargs["json"]["model"] == "test-model"


def test_groq_429_retries_then_signals_exhaustion_without_sleeping():
    backend = GroqBackend(_settings())
    limited = _response(429, headers={"Retry-After": "0"})
    with patch("src.model_router.requests.post", return_value=limited) as post, patch(
        "src.model_router.time.sleep"
    ) as sleep:
        with pytest.raises(RuntimeError, match="exhausted after 2 attempts"):
            backend.chat([{"role": "user", "content": "check"}], model="rate-limited")

    assert post.call_count == 2
    sleep.assert_called_once()


def test_gemini_chat_parses_function_call():
    backend = GeminiBackend(_settings())
    payload = {
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [
                {"text": "Searching now."},
                {"functionCall": {"name": "lookup", "args": {"query": "weather"}}},
            ]},
        }],
    }
    with patch("src.model_router.requests.post", return_value=_response(200, payload)):
        result = backend.chat([{"role": "user", "content": "weather"}], model="gemini-test")

    assert result.text == "Searching now."
    assert result.model_used == "gemini-test"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "lookup"
    assert result.tool_calls[0].input == {"query": "weather"}


def test_model_router_walks_chain_until_third_model_succeeds():
    chain = [
        {"provider": "first", "model": "one"},
        {"provider": "second", "model": "two"},
        {"provider": "third", "model": "three"},
    ]
    router = ModelRouter(_settings(chain=chain))
    backend = MagicMock()

    def chat(*_args, model, **_kwargs):
        if model != "three":
            raise RuntimeError("429 exhausted")
        return ChatResult(text="done", model_used=model)

    backend.chat.side_effect = chat
    with patch.object(router, "_get_free_api", return_value=backend):
        result = router.chat("free_api", [{"role": "user", "content": "go"}])

    assert [item.kwargs["model"] for item in backend.chat.call_args_list] == ["one", "two", "three"]
    assert result.model_used == "third:three"


def test_force_chain_start_index_skips_earlier_entries():
    chain = [
        {"provider": "first", "model": "one"},
        {"provider": "second", "model": "two"},
        {"provider": "third", "model": "three"},
    ]
    router = ModelRouter(_settings(chain=chain))
    backend = MagicMock()
    backend.chat.return_value = ChatResult(text="done")
    with patch.object(router, "_get_free_api", return_value=backend):
        result = router.chat(
            "free_api", [{"role": "user", "content": "go"}], force_chain_start_index=1
        )

    backend.chat.assert_called_once()
    assert backend.chat.call_args.kwargs["model"] == "two"
    assert result.model_used == "second:two"


def test_allow_premium_false_never_selects_or_accepts_premium():
    router = ModelRouter(_settings(allow_premium=False))

    assert router.pick_tier("analyze this") == "free_api"
    with pytest.raises(ValueError, match="Premium tier is disabled"):
        router.chat("premium", [{"role": "user", "content": "analyze this"}])
