"""Model layer: local (free, Ollama) + premium (Anthropic) backends behind one interface,
plus a router that decides which tier to use for a given message.

Every backend exposes the same shape:
    chat(messages, tools=None, system=None) -> ChatResult

so the orchestrator never needs to know which backend answered.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import Settings


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None
    raw: Any = None


class AnthropicBackend:
    """Premium tier. Uses native Anthropic tool-use format."""

    supports_tools = True

    def __init__(self, settings: Settings):
        from anthropic import Anthropic  # imported lazily so local-only setups don't need it installed

        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to .env to use the premium tier, "
                "or force --tier local."
            )
        self.client = Anthropic(api_key=settings.anthropic_api_key)
        self.model = settings.premium_model["model"]
        self.max_tokens = settings.premium_model.get("max_tokens", 2048)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ChatResult:
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            messages=messages,
        )
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        resp = self.client.messages.create(**kwargs)

        text_parts = []
        tool_calls = []
        for block in resp.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))

        return ChatResult(
            text="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason,
            raw=resp,
        )


class GroqBackend:
    """Free tier via Groq's API (OpenAI-compatible endpoint, no cost, generous
    rate limits, no local install needed). Use this instead of Ollama when you
    don't want to run/manage a local model server, and instead of Anthropic
    when you don't want to spend paid credits.

    Get a free key at https://console.groq.com/keys and put it in .env as
    GROQ_API_KEY.
    """

    supports_tools = True

    def __init__(self, settings: Settings):
        if not settings.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add it to .env to use the free_api tier "
                "(get one free at https://console.groq.com/keys)."
            )
        self.api_key = settings.groq_api_key
        cfg = settings.free_api_model
        self.model = cfg["model"]
        self.max_tokens = cfg.get("max_tokens", 2048)
        self.base_url = cfg.get("base_url", "https://api.groq.com/openai/v1")

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ChatResult:
        openai_messages = []
        if system:
            openai_messages.append({"role": "system", "content": system})

        for m in messages:
            content = m["content"]
            if isinstance(content, list):
                # Anthropic-style content blocks -> OpenAI-style messages.
                if m["role"] == "assistant" and any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in content
                ):
                    text = "".join(b["text"] for b in content if b.get("type") == "text")
                    tool_calls = [
                        {
                            "id": b["id"],
                            "type": "function",
                            "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                        }
                        for b in content
                        if b.get("type") == "tool_use"
                    ]
                    msg: dict[str, Any] = {"role": "assistant", "content": text or ""}
                    if tool_calls:
                        msg["tool_calls"] = tool_calls
                    openai_messages.append(msg)
                    continue
                if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                    for b in content:
                        openai_messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": b["tool_use_id"],
                                "content": str(b["content"]),
                            }
                        )
                    continue
                text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
                openai_messages.append({"role": m["role"], "content": text})
            else:
                openai_messages.append({"role": m["role"], "content": content})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = _anthropic_tools_to_ollama(tools)  # same shape OpenAI expects

        # Retry logic for rate limits (429) with exponential backoff or Retry-After header
        max_retries = 3
        backoff_base = 2

        for attempt in range(max_retries):
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=60,
            )

            if resp.status_code == 429:
                # Rate limited; try to sleep before retrying
                if attempt < max_retries - 1:  # Don't sleep on the last attempt
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            sleep_time = float(retry_after)
                        except ValueError:
                            # If Retry-After is not a number, use exponential backoff
                            sleep_time = backoff_base ** (attempt + 1)
                    else:
                        sleep_time = backoff_base ** (attempt + 1)
                    time.sleep(sleep_time)
                continue

            # Check for other HTTP errors (4xx, 5xx)
            if not (200 <= resp.status_code < 300):
                resp.raise_for_status()

            # Success; we can proceed
            data = resp.json()
            break
        else:
            # Loop completed without break means all retries exhausted on 429
            raise RuntimeError(
                "Groq API rate limit (HTTP 429) hit after 3 retries. "
                "Check your usage at https://console.groq.com/account/limits."
            )

        choice = data["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""

        tool_calls = []
        for tc in message.get("tool_calls", []) or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), input=args))

        return ChatResult(
            text=text.strip(),
            tool_calls=tool_calls,
            stop_reason=choice.get("finish_reason"),
            raw=data,
        )


class OllamaBackend:
    """Free local tier via Ollama's HTTP API.

    Ollama's tool-calling support varies by model; this backend degrades gracefully:
    if the model doesn't emit a structured tool call, it just returns text and the
    orchestrator treats it as a final answer (no tool loop on this tier unless the
    local model you pulled genuinely supports function calling, e.g. llama3.1).
    """

    supports_tools = True

    def __init__(self, settings: Settings):
        cfg = settings.local_model
        self.host = cfg["host"].rstrip("/")
        self.model = cfg["model"]

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ChatResult:
        ollama_messages = []
        if system:
            ollama_messages.append({"role": "system", "content": system})
        for m in messages:
            content = m["content"]
            if isinstance(content, list):
                # Flatten Anthropic-style content blocks (tool_result etc.) to plain text
                # for models that don't understand structured blocks.
                text = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text.append(block["text"])
                    elif isinstance(block, dict) and "content" in block:
                        text.append(str(block["content"]))
                content = "\n".join(text)
            ollama_messages.append({"role": m["role"], "content": content})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": False,
        }
        if tools:
            payload["tools"] = _anthropic_tools_to_ollama(tools)

        resp = requests.post(f"{self.host}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        message = data.get("message", {})
        text = message.get("content", "") or ""
        tool_calls = []
        for i, tc in enumerate(message.get("tool_calls", []) or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(id=f"local-{i}-{int(time.time())}", name=fn.get("name", ""), input=args))

        return ChatResult(text=text.strip(), tool_calls=tool_calls, stop_reason=data.get("done_reason"), raw=data)


def _anthropic_tools_to_ollama(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ollama's /api/chat expects OpenAI-style tool schemas; convert from Anthropic's format."""
    converted = []
    for t in tools:
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
        )
    return converted


class ModelRouter:
    """Decides local vs premium and dispatches to the right backend.

    This is intentionally a simple, editable heuristic — see README for how to
    tune or replace it once you know your real usage patterns.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._premium: AnthropicBackend | None = None
        self._local: OllamaBackend | None = None
        self._free_api: GroqBackend | None = None

    def _get_premium(self) -> AnthropicBackend:
        if self._premium is None:
            self._premium = AnthropicBackend(self.settings)
        return self._premium

    def _get_local(self) -> OllamaBackend:
        if self._local is None:
            self._local = OllamaBackend(self.settings)
        return self._local

    def _get_free_api(self) -> GroqBackend:
        if self._free_api is None:
            self._free_api = GroqBackend(self.settings)
        return self._free_api

    def pick_tier(self, latest_user_message: str) -> str:
        routing = self.settings.routing
        msg_lower = latest_user_message.lower()

        if len(latest_user_message) > routing.get("long_message_char_threshold", 400):
            return "premium"

        for kw in routing.get("premium_keywords", []):
            if kw.lower() in msg_lower:
                return "premium"

        return routing.get("default_tier", "free_api")

    def backend_for(self, tier: str):
        if tier == "premium":
            return self._get_premium()
        if tier == "local":
            return self._get_local()
        if tier == "free_api":
            return self._get_free_api()
        raise ValueError(f"Unknown tier: {tier}")

    def chat(
        self,
        tier: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
    ) -> ChatResult:
        backend = self.backend_for(tier)
        return backend.chat(messages, tools=tools, system=system)