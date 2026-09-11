"""The orchestrator is the one place that knows how to: load context, call the
model, run a tool-use loop until the model is done, and write results back to
memory. Every interface (CLI, scheduler, a future Telegram bot, etc.) should
call `handle_message()` and nothing else — don't reimplement the tool loop
elsewhere.
"""
from __future__ import annotations

from typing import Any, Optional

from .config import Settings
from .memory.store import SqliteStore
from .memory.vector_store import VectorMemory
from .model_router import ChatResult, ModelRouter
from .tools.registry import ToolRegistry, build_registry

SYSTEM_PROMPT = """You are a personal AI assistant running locally on the user's machine.
You have tools to read/write files in a sandboxed workspace, run shell commands (with the
user's confirmation), and search long-term memory. Use tools when a task requires real
action or information you don't already have. Be direct and concise. When you're not sure
whether to act or ask, ask.
"""

MAX_TOOL_ITERATIONS = 6


class Orchestrator:
    def __init__(self, settings: Settings, confirm_fn: Optional[Any] = None):
        self.settings = settings
        self.router = ModelRouter(settings)
        self.store = SqliteStore(settings.sqlite_path())
        self.vector_memory = VectorMemory(
            settings.chroma_path(), settings.memory.get("chroma_collection", "assistant_memory")
        )
        self.tools: ToolRegistry = build_registry(settings, self.vector_memory, confirm_fn=confirm_fn)

    def handle_message(
        self,
        session_id: str,
        user_message: str,
        force_tier: Optional[str] = None,
    ) -> str:
        tier = force_tier or self.router.pick_tier(user_message)
        self.store.log_model_call(tier, user_message)
        self.store.log_message(session_id, "user", user_message)

        history = self.store.recent_messages(
            session_id, limit=self.settings.memory.get("max_history_messages", 20)
        )
        messages: list[dict[str, Any]] = [{"role": h["role"], "content": h["content"]} for h in history]

        anthropic_tools = self.tools.anthropic_tools()
        final_text = self._run_tool_loop(tier, messages, anthropic_tools)

        self.store.log_message(session_id, "assistant", final_text)
        # Keep a lightweight semantic trace so search_memory has something to find later.
        self.vector_memory.add_memory(
            f"User: {user_message}\nAssistant: {final_text}",
            metadata={"session_id": session_id},
        )
        return final_text

    def _run_tool_loop(
        self,
        tier: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> str:
        for _ in range(MAX_TOOL_ITERATIONS):
            result: ChatResult = self.router.chat(tier, messages, tools=tools, system=SYSTEM_PROMPT)

            if not result.tool_calls:
                return result.text or "(no response)"

            # Record the assistant's tool-use turn, then feed back tool results.
            assistant_content = []
            if result.text:
                assistant_content.append({"type": "text", "text": result.text})
            for tc in result.tool_calls:
                assistant_content.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            messages.append({"role": "assistant", "content": assistant_content})

            tool_result_blocks = []
            for tc in result.tool_calls:
                output = self.tools.call(tc.name, **tc.input)
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": str(output),
                    }
                )
            messages.append({"role": "user", "content": tool_result_blocks})

        return "Stopped after reaching the tool-call iteration limit — task may be incomplete."
