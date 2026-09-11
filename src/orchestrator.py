"""The orchestrator is the one place that knows how to: load context, call the
model, run a tool-use loop until the model is done, and write results back to
memory. Every interface (CLI, scheduler, a future Telegram bot, etc.) should
call `handle_message()` and nothing else — don't reimplement the tool loop
elsewhere.
"""
from __future__ import annotations

import time
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
If asked what you can do, what features or tools you have, or similar meta-questions about
your own capabilities, answer directly from your knowledge of the tools available in this
conversation. Do not call tools to answer capability questions. Only call tools when the
user is asking you to actually do or look up something.
"""

MAX_TOOL_ITERATIONS = 10
MAX_TOOL_LOOP_SECONDS = 45


def _summarize_tool_result(value: Any, limit: int = 300) -> str:
    text = str(value)
    if text.lower().strip().startswith("{\"error\"") or "error" in text.lower() and "confirmation denied" not in text.lower():
        return ""
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


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
        start = time.monotonic()
        for _ in range(MAX_TOOL_ITERATIONS):
            elapsed = time.monotonic() - start
            if elapsed > MAX_TOOL_LOOP_SECONDS:
                return self._stopped_tool_loop_response(messages, elapsed_seconds=elapsed)

            try:
                result: ChatResult = self.router.chat(
                    tier,
                    messages,
                    tools=tools,
                    system=SYSTEM_PROMPT,
                    timeout_seconds=MAX_TOOL_LOOP_SECONDS - elapsed,
                )
            except TimeoutError:
                return self._stopped_tool_loop_response(
                    messages,
                    elapsed_seconds=time.monotonic() - start,
                )

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
            confirmation_denied_action = None
            for tc in result.tool_calls:
                output = self.tools.call(tc.name, **tc.input)
                output_str = str(output)
                if len(output_str) > 4000:
                    output_str = output_str[:4000] + "... [truncated]"
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": output_str,
                    }
                )

                # Check if this tool call was denied due to confirmation
                if "confirmation denied or not provided" in output_str:
                    confirmation_denied_action = (tc.name, tc.input)

            # If any tool call was denied due to confirmation, stop and ask for approval
            if confirmation_denied_action:
                tool_name, tool_input = confirmation_denied_action
                action_desc = self._describe_tool_action(tool_name, tool_input)
                return f"Waiting for your approval on: {action_desc}. Approve or deny it to continue."

            messages.append({"role": "user", "content": tool_result_blocks})

        return self._stopped_tool_loop_response(messages)

    @staticmethod
    def _stopped_tool_loop_response(
        messages: list[dict[str, Any]],
        elapsed_seconds: float | None = None,
    ) -> str:
        partial_results: list[str] = []
        for message in messages:
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                text = str(block.get("content", ""))
                if not text:
                    continue
                lower = text.lower()
                if "error" in lower and "confirmation denied" not in lower:
                    continue
                summary = _summarize_tool_result(text, 300)
                if summary:
                    partial_results.append(summary)

        if elapsed_seconds is not None:
            stop_message = f"Stopped after {elapsed_seconds:.0f}s to avoid a long hang"
        else:
            stop_message = "Stopped after reaching the tool-call iteration limit"

        if partial_results:
            summary_block = [f"{stop_message} — here's what was gathered so far:"]
            summary_block.extend(f"- {entry}" for entry in partial_results[:5])
            return "\n".join(summary_block) + "\nTask may be incomplete."

        return f"{stop_message} — task may be incomplete."

    def _describe_tool_action(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Extract a human-readable description of a tool action from its name and input."""
        if tool_name == "run_shell_command":
            return f"the shell command: `{tool_input.get('command', 'unknown')}`"
        elif tool_name == "send_email":
            to = tool_input.get("to", "unknown")
            subject = tool_input.get("subject", "")
            return f"sending email to {to} with subject '{subject}'"
        elif tool_name == "create_event":
            title = tool_input.get("title", "unknown")
            return f"creating calendar event: {title}"
        elif tool_name == "terminate_process":
            pid = tool_input.get("pid", "unknown")
            return f"terminating process {pid}"
        else:
            # Generic fallback
            return f"the {tool_name} action"
