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
    model_used: str | None = None


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


class OpenAICompatibleBackend:
    """Backend for providers exposing an OpenAI-compatible chat endpoint."""

    supports_tools = True

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        max_tokens: int = 2048,
        provider_name: str = "OpenAI-compatible",
        api_key_name: str = "OPENAI_COMPATIBLE_API_KEY",
        api_key_url: str | None = None,
    ):
        if not api_key:
            suffix = f" (get one at {api_key_url})" if api_key_url else ""
            raise RuntimeError(f"{api_key_name} is not set. Add it to .env{suffix}.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.provider_name = provider_name

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        timeout_seconds: float | None = None,
        model: str | None = None,
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
            "messages": openai_messages,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = _anthropic_tools_to_ollama(tools)  # same shape OpenAI expects

        # Retry logic for rate limits (429) with exponential backoff or Retry-After header
        max_retries = 2
        backoff_base = 2
        max_backoff_seconds = 4
        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None

        selected_model = model or self.model
        if not selected_model:
            raise ValueError(f"A {self.provider_name} model is required.")
        payload["model"] = selected_model
        for attempt in range(max_retries):
            request_timeout = 60.0
            if deadline is not None:
                request_timeout = deadline - time.monotonic()
                if request_timeout <= 0:
                    raise TimeoutError(f"{self.provider_name} request exceeded the tool-loop time budget.")
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=min(60.0, request_timeout),
                )
            except requests.Timeout as exc:
                raise TimeoutError(
                    f"{self.provider_name} request exceeded the tool-loop time budget."
                ) from exc

            if resp.status_code == 429:
                if attempt < max_retries - 1:
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        sleep_time = float(retry_after) if retry_after else backoff_base ** (attempt + 1)
                    except ValueError:
                        sleep_time = backoff_base ** (attempt + 1)
                    sleep_time = min(sleep_time, max_backoff_seconds)
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError(
                                f"{self.provider_name} retry exceeded the tool-loop time budget."
                            )
                        sleep_time = min(sleep_time, remaining)
                    time.sleep(sleep_time)
                continue

            if not (200 <= resp.status_code < 300):
                resp.raise_for_status()

            data = resp.json()
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
                model_used=selected_model,
            )

        raise RuntimeError(
            f"{self.provider_name} model {selected_model} exhausted after "
            f"{max_retries} attempts."
        )


class GroqBackend(OpenAICompatibleBackend):
    """Compatibility wrapper for the historical ``GroqBackend(settings)`` API."""

    def __init__(self, settings: Settings, model: str | None = None):
        cfg = settings.free_api_model
        chain = cfg.get("chain", [])
        configured_model = next(
            (entry.get("model") for entry in chain if entry.get("provider") == "groq"),
            None,
        )
        super().__init__(
            base_url=settings.raw.get("models", {}).get("groq", {}).get(
                "base_url", "https://api.groq.com/openai/v1"
            ),
            api_key=settings.groq_api_key,
            model=model or configured_model or "openai/gpt-oss-120b",
            max_tokens=cfg.get("max_tokens", 2048),
            provider_name="Groq",
            api_key_name="GROQ_API_KEY",
            api_key_url="https://console.groq.com/keys",
        )


class GeminiBackend:
    """Free Gemini tier using Google's generateContent REST API."""

    supports_tools = True

    def __init__(self, settings: Settings):
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Add it to .env to use the free_api tier "
                "(get one free at https://aistudio.google.com/apikey)."
            )
        self.api_key = settings.gemini_api_key
        self.max_tokens = settings.free_api_model.get("max_tokens", 2048)

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        timeout_seconds: float | None = None,
        model: str | None = None,
    ) -> ChatResult:
        if not model:
            raise ValueError("A Gemini model is required.")
        payload: dict[str, Any] = {
            "contents": self._contents(messages),
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = [{
                "function_declarations": [
                    {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object", "properties": {}}),
                    }
                    for tool in tools
                ]
            }]

        timeout = timeout_seconds or 60.0
        for attempt in range(3):
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={self.api_key}",
                json=payload,
                timeout=min(60.0, timeout),
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
            exhausted = resp.status_code == 429 or data.get("error", {}).get("status") == "RESOURCE_EXHAUSTED"
            if exhausted:
                if attempt < 2:
                    time.sleep(min(2 ** (attempt + 1), 4))
                continue
            if not (200 <= resp.status_code < 300):
                resp.raise_for_status()

            candidate = data.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            text_parts = [part["text"] for part in parts if "text" in part]
            tool_calls = []
            for index, part in enumerate(parts):
                function_call = part.get("functionCall")
                if function_call:
                    tool_calls.append(
                        ToolCall(
                            id=f"gemini-{index}-{int(time.time())}",
                            name=function_call.get("name", ""),
                            input=function_call.get("args", {}),
                        )
                    )
            return ChatResult(
                text="\n".join(text_parts).strip(),
                tool_calls=tool_calls,
                stop_reason=candidate.get("finishReason"),
                raw=data,
                model_used=model,
            )
        raise RuntimeError(f"Gemini model {model} exhausted after 3 attempts.")

    @staticmethod
    def _contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        tool_names: dict[str, str] = {}
        contents = []
        for message in messages:
            role = "model" if message.get("role") == "assistant" else "user"
            raw_content = message.get("content", "")
            blocks = raw_content if isinstance(raw_content, list) else [{"type": "text", "text": raw_content}]
            parts = []
            for block in blocks:
                if not isinstance(block, dict):
                    parts.append({"text": str(block)})
                elif block.get("type") == "text":
                    parts.append({"text": block.get("text", "")})
                elif block.get("type") == "tool_use":
                    tool_names[block.get("id", "")] = block.get("name", "")
                    parts.append({"functionCall": {"name": block.get("name", ""), "args": block.get("input", {})}})
                elif block.get("type") == "tool_result":
                    parts.append({"functionResponse": {
                        "name": tool_names.get(block.get("tool_use_id", ""), ""),
                        "response": {"content": block.get("content", "")},
                    }})
            if parts:
                contents.append({"role": role, "parts": parts})
        return contents


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
        self._free_api: dict[str, OpenAICompatibleBackend | GeminiBackend] = {}

    def _get_premium(self) -> AnthropicBackend:
        if self._premium is None:
            self._premium = AnthropicBackend(self.settings)
        return self._premium

    def _get_local(self) -> OllamaBackend:
        if self._local is None:
            self._local = OllamaBackend(self.settings)
        return self._local

    def _get_free_api(
        self, provider: str, model: str | None = None
    ) -> OpenAICompatibleBackend | GeminiBackend:
        """Return a backend for one chain entry, cached by provider and model."""
        cache_key = f"{provider}:{model or ''}"
        if cache_key not in self._free_api:
            if provider == "groq":
                self._free_api[cache_key] = GroqBackend(self.settings, model=model)
            elif provider == "gemini":
                self._free_api[cache_key] = GeminiBackend(self.settings)
            elif provider == "openrouter":
                self._free_api[cache_key] = OpenAICompatibleBackend(
                    base_url="https://openrouter.ai/api/v1",
                    api_key=self.settings.openrouter_api_key,
                    model=model or "",
                    max_tokens=self.settings.free_api_model.get("max_tokens", 2048),
                    provider_name="OpenRouter",
                    api_key_name="OPENROUTER_API_KEY",
                    api_key_url="https://openrouter.ai/keys",
                )
            else:
                raise ValueError(f"Unsupported free_api provider: {provider}")
        return self._free_api[cache_key]

    def pick_tier(self, latest_user_message: str) -> str:
        routing = self.settings.routing
        msg_lower = latest_user_message.lower()

        if len(latest_user_message) > routing.get("long_message_char_threshold", 400):
            selected = "premium"
        else:
            selected = next(
                ("premium" for kw in routing.get("premium_keywords", []) if kw.lower() in msg_lower),
                routing.get("default_tier", "free_api"),
            )

        if selected == "premium" and not self.settings.allow_premium:
            return "free_api"
        return selected

    def backend_for(self, tier: str):
        if tier == "premium":
            return self._get_premium()
        if tier == "local":
            return self._get_local()
        if tier == "free_api":
            chain = self.settings.free_api_model.get("chain", [])
            if not chain:
                raise RuntimeError("Free API model chain is empty. Add models.free_api.chain to config.yaml.")
            return self._get_free_api(chain[0]["provider"], chain[0]["model"])
        raise ValueError(f"Unknown tier: {tier}")

    def chat(
        self,
        tier: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        system: str | None = None,
        timeout_seconds: float | None = None,
        force_chain_start_index: int | None = None,
    ) -> ChatResult:
        if tier == "free_api":
            chain = self.settings.free_api_model.get("chain", [])
            if not chain:
                raise RuntimeError("Free API model chain is empty. Add models.free_api.chain to config.yaml.")
            start_index = force_chain_start_index if force_chain_start_index is not None else 0
            if start_index < 0 or start_index >= len(chain):
                raise ValueError(f"Invalid free_api chain start index: {start_index}")
            tried = []
            for index in range(start_index, len(chain)):
                entry = chain[index]
                provider = entry["provider"]
                model = entry["model"]
                tried.append(f"{provider}:{model}")
                try:
                    backend = self._get_free_api(provider, model)
                    result = backend.chat(
                        messages,
                        tools=tools,
                        system=system,
                        timeout_seconds=timeout_seconds,
                        model=model,
                    )
                    result.model_used = f"{provider}:{model}"
                    return result
                except TimeoutError:
                    raise
                except RuntimeError as exc:
                    if "API_KEY is not set" in str(exc):
                        raise
                    if index < len(chain) - 1:
                        next_entry = chain[index + 1]
                        print(
                            f"model {provider}:{model} exhausted, falling back to "
                            f"{next_entry['provider']}:{next_entry['model']}"
                        )
                except Exception:
                    if index < len(chain) - 1:
                        next_entry = chain[index + 1]
                        print(
                            f"model {provider}:{model} exhausted, falling back to "
                            f"{next_entry['provider']}:{next_entry['model']}"
                        )
            raise RuntimeError(
                "Free API daily limit exhausted for all configured models: "
                f"{', '.join(tried)}. Wait for the limits to reset or temporarily set "
                "routing.allow_premium to true in config.yaml."
            )
        if tier == "premium" and not self.settings.allow_premium:
            raise ValueError("Premium tier is disabled by routing.allow_premium: false in config.yaml.")
        backend = self.backend_for(tier)
        return backend.chat(messages, tools=tools, system=system)
