"""The orchestrator is the one place that knows how to: load context, call the
model, run a tool-use loop until the model is done, and write results back to
memory. Every interface (CLI, scheduler, a future Telegram bot, etc.) should
call `handle_message()` and nothing else — don't reimplement the tool loop
elsewhere.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time
from typing import Any, Optional

from .config import Settings
from .memory.store import SqliteStore
from .memory.vector_store import VectorMemory
from .model_router import ChatResult, ModelRouter
from .tools.relevance import log_selection, select_relevant_tools
from .tools.registry import ToolRegistry, build_registry

SYSTEM_PROMPT = """You are a personal AI assistant running locally on the user's machine.
You have tools to read/write files in a sandboxed workspace, run shell commands (with the
user's confirmation), and search long-term memory. You also have developer tools for
explicitly requested files and folders anywhere under C:\\ (for example Documents, Downloads,
and Pictures), outside the project workspace. Use the developer tools for those C:\\ paths;
they enforce their configured C:\\ root and require manual approval for write, delete, and
code-execution actions. Use tools when a task requires real action or information you don't
already have. Be direct and concise. When you're not sure whether to act or ask, ask.
If asked what you can do, what features or tools you have, or similar meta-questions about
your own capabilities, answer directly from your knowledge of the tools available in this
conversation. Do not call tools to answer capability questions. Only call tools when the
user is asking you to actually do or look up something.
Tools are either confirmation-gated or not. If a tool is available and is not confirmation-
gated (most read actions and content-generation actions such as image generation are not),
call it directly. Do not ask the user "shall I proceed?" before attempting it. Only the
tool's own confirmation mechanism should ever pause for approval; never add an extra manual
approval step on top of it.
Only call a tool when it's necessary to directly fulfill what the user asked. Do not proactively
call additional tools to gather extra context, verify assumptions, or check related information
the user did not request — if something is ambiguous, ask the user instead of investigating via tools.
For questions about documents already ingested into the knowledge base, use search_knowledge_base
for semantic retrieval. Do not use filesystem tools to read or dump raw PDF bytes for a knowledge-
base question unless the user explicitly asks for the document's raw contents or file operations.
You have tools to remember durable facts (remember_fact), log significant events or decisions
(log_episode), and recall past task procedures (recall_procedure). Use these sparingly and with
real judgment — call remember_fact only for information that will matter in future conversations
(stated preferences, standing project facts), not routine details. Call log_episode only for
genuinely significant outcomes (a decision made, a problem solved, a milestone reached), not every
exchange. Before starting a multi-step task, consider calling recall_procedure to check if a similar
task has a known successful sequence — but always adapt it to the current request rather than blindly
repeating it.
"""

MAX_TOOL_ITERATIONS = 10
MAX_TOOL_LOOP_SECONDS = 45
TOOL_CALL_HARD_TIMEOUT_SECONDS = 150
_IMAGE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.(?:png|jpe?g|gif|webp)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _image_paths_from_result(value: Any) -> list[str]:
    """Find workspace-relative image paths in a tool result recursively."""
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            for match in _IMAGE_PATH_RE.findall(item):
                normalized = match.replace("\\", "/")
                if not normalized.startswith(("/", "//")) and not re.match(r"^[A-Za-z]:/", normalized):
                    if normalized not in found:
                        found.append(normalized)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return found


def _summarize_tool_result(value: Any, limit: int = 300) -> str:
    text = str(value)
    if text.lower().strip().startswith("{\"error\"") or "error" in text.lower() and "confirmation denied" not in text.lower():
        return ""
    return text[:limit].rstrip() + ("..." if len(text) > limit else "")


def _turn_importance(user_message: str, assistant_reply: str, tool_trace: list[str]) -> int:
    """Assign a small local importance signal without another model call."""
    score = 1
    combined_length = len(user_message.strip()) + len(assistant_reply.strip())
    if combined_length >= 300:
        score += 1
    if tool_trace:
        score += 1
    if len(set(tool_trace)) >= 2:
        score += 1
    reply_lower = assistant_reply.lower()
    if "[error]" in reply_lower or "task may be incomplete" in reply_lower:
        score += 1
    return min(5, score)


class Orchestrator:
    def __init__(self, settings: Settings, confirm_fn: Optional[Any] = None):
        self.settings = settings
        self.router = ModelRouter(settings)
        self.store = SqliteStore(settings.sqlite_path())
        self.vector_memory = VectorMemory(
            settings.chroma_path(), settings.memory.get("chroma_collection", "assistant_memory")
        )
        self.semantic_memory = VectorMemory(settings.chroma_path(), "semantic_memory")
        self.episodic_memory = VectorMemory(settings.chroma_path(), "episodic_memory")
        self.procedure_signatures = VectorMemory(
            settings.chroma_path(), "procedure_signatures", distance_space="cosine"
        )
        self._memory_retrieval = settings.memory_retrieval
        self._procedure_config = self._memory_retrieval.get("procedural_memory", {})
        self.tools: ToolRegistry = build_registry(
            settings,
            self.vector_memory,
            confirm_fn=confirm_fn,
            semantic_memory=self.semantic_memory,
            episodic_memory=self.episodic_memory,
            procedure_signatures=self.procedure_signatures,
            store=self.store,
        )
        self._tool_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        self._last_model_used: dict[str, str] = {}
        self._last_images: dict[str, list[str]] = {}
        tool_schemas = self.tools.anthropic_tools()
        schema_tokens = sum(len(json.dumps(schema)) for schema in tool_schemas) // 4
        print(
            f"Tool schema overhead: ~{schema_tokens} tokens sent on every request "
            f"across {len(tool_schemas)} tools."
        )

    def handle_message(
        self,
        session_id: str,
        user_message: str,
        force_tier: Optional[str] = None,
        force_chain_start_index: int | None = None,
    ) -> str:
        tier = force_tier or self.router.pick_tier(user_message)
        # Image metadata is per-turn. Clear it before work so an exception or a
        # tool-free response can never expose the previous turn's images.
        self._last_images[session_id] = []
        self.store.log_model_call(tier, user_message)
        self.store.log_message(session_id, "user", user_message)

        history = self.store.recent_messages(
            session_id, limit=self.settings.memory.get("max_history_messages", 20)
        )
        messages: list[dict[str, Any]] = [{"role": h["role"], "content": h["content"]} for h in history]

        anthropic_tools = self.tools.anthropic_tools()
        image_paths: list[str] = []
        tool_call_trace: list[str] = []
        final_result = self._run_tool_loop(
            tier,
            messages,
            anthropic_tools,
            relevance_context=user_message,
            force_chain_start_index=force_chain_start_index,
            image_paths=image_paths,
            tool_call_trace=tool_call_trace,
        )
        self._last_model_used[session_id] = final_result.model_used or "unknown"
        self._last_images[session_id] = list(dict.fromkeys(image_paths))
        final_text = final_result.text

        self.store.log_message(session_id, "assistant", final_text)
        # Keep a lightweight semantic trace so search_memory has something to find later.
        self.vector_memory.add_memory(
            f"User: {user_message}\nAssistant: {final_text}",
            metadata={
                "session_id": session_id,
                "importance": _turn_importance(user_message, final_text, tool_call_trace),
            },
        )
        if self._is_successful_multitool_turn(final_text, tool_call_trace):
            self._record_procedure(user_message, tool_call_trace)
        return final_text

    def get_last_model_used(self, session_id: str) -> str | None:
        return self._last_model_used.get(session_id)

    def get_last_images(self, session_id: str) -> list[str]:
        return list(self._last_images.get(session_id, []))

    def _run_tool_loop(
        self,
        tier: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        relevance_context: str = "",
        force_chain_start_index: int | None = None,
        image_paths: list[str] | None = None,
        tool_call_trace: list[str] | None = None,
    ) -> ChatResult:
        start = time.monotonic()
        executed_results: dict[tuple[str, Any], str] = {}
        called_tool_names: set[str] = set()
        last_model_used: str | None = None
        relevance_cfg = self.settings.tool_relevance_filter
        loop_deadline = start + MAX_TOOL_LOOP_SECONDS
        for _ in range(MAX_TOOL_ITERATIONS):
            elapsed = time.monotonic() - start
            if elapsed > loop_deadline - start:
                return ChatResult(
                    text=self._stopped_tool_loop_response(messages, elapsed_seconds=elapsed),
                    model_used=last_model_used,
                )

            if relevance_cfg["enabled"]:
                selected_tools = select_relevant_tools(
                    relevance_context,
                    tools,
                    relevance_cfg["max_tools"],
                    relevance_cfg["min_tools"],
                    relevance_cfg["core_tools"] | called_tool_names,
                )
                # Retrieval tools are deliberately one-shot per turn.  A
                # model that keeps asking the same semantic search for slightly
                # different ``k`` values can otherwise spend the entire wall
                # clock budget retrieving the same document instead of
                # answering from the results it already received.
                selected_tools = [
                    tool
                    for tool in selected_tools
                    if not (
                        tool.get("name") in {"search_memory", "search_knowledge_base"}
                        and tool.get("name") in called_tool_names
                    )
                ]
                if "search_knowledge_base" in called_tool_names:
                    # Once indexed retrieval has supplied document context,
                    # keep the model from falling back to expensive/raw file
                    # inspection tools in the answer-generation pass.
                    selected_tools = [
                        tool
                        for tool in selected_tools
                        if tool.get("name")
                        not in {
                            "filesystem",
                            "search_files",
                            "read_file_anywhere",
                            "list_directory_anywhere",
                            "search_memory",
                        }
                    ]
                log_selection(relevance_context, tools, selected_tools)
            else:
                selected_tools = tools

            try:
                result: ChatResult = self.router.chat(
                    tier,
                    messages,
                    tools=selected_tools,
                    system=SYSTEM_PROMPT,
                    timeout_seconds=max(1, loop_deadline - start - elapsed),
                    force_chain_start_index=force_chain_start_index,
                )
                last_model_used = result.model_used
            except TimeoutError:
                return ChatResult(
                    text=self._stopped_tool_loop_response(
                        messages,
                        elapsed_seconds=time.monotonic() - start,
                    ),
                    model_used=last_model_used,
                )

            if not result.tool_calls:
                return ChatResult(
                    text=result.text or "(no response)",
                    tool_calls=result.tool_calls,
                    stop_reason=result.stop_reason,
                    raw=result.raw,
                    model_used=result.model_used,
                )

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
                slow_tool_timeout = self._slow_tool_timeout_seconds(tc.name)
                tool_timeout = slow_tool_timeout or TOOL_CALL_HARD_TIMEOUT_SECONDS
                # The model has now selected a known slow tool. Extend only
                # this turn's deadline to that explicit tool budget; all
                # unlisted tools remain constrained to the normal 45 seconds.
                if slow_tool_timeout is not None:
                    loop_deadline = max(loop_deadline, start + slow_tool_timeout)
                try:
                    input_key: Any = frozenset(tc.input.items())
                except TypeError:
                    input_key = json.dumps(tc.input, sort_keys=True)
                cache_key = (tc.name, input_key)
                if cache_key in executed_results:
                    output_str = executed_results[cache_key]
                else:
                    future = self._tool_executor.submit(self.tools.call, tc.name, **tc.input)
                    try:
                        output = future.result(timeout=tool_timeout)
                    except concurrent.futures.TimeoutError:
                        output = {
                            "error": (
                                f"Tool '{tc.name}' did not respond within "
                                f"{tool_timeout}s and was abandoned. "
                                "It may still be running in the background."
                            )
                        }
                    output_str = str(output)
                    executed_results[cache_key] = output_str
                    if tool_call_trace is not None:
                        tool_call_trace.append(tc.name)
                if image_paths is not None:
                    image_paths.extend(_image_paths_from_result(output_str))
                if len(output_str) > 4000:
                    output_str = output_str[:4000] + "... [truncated]"
                called_tool_names.add(tc.name)
                relevance_context += f"\nTool {tc.name} result: {output_str[:300]}"
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
                return ChatResult(
                    text=f"Waiting for your approval on: {action_desc}. Approve or deny it to continue.",
                    model_used=last_model_used,
                )

            messages.append({"role": "user", "content": tool_result_blocks})

        return ChatResult(text=self._stopped_tool_loop_response(messages), model_used=last_model_used)

    @staticmethod
    def _is_successful_multitool_turn(final_text: str, tool_call_trace: list[str]) -> bool:
        """Only learn from completed, genuinely multi-tool turns."""
        reply = final_text.strip().lower()
        return (
            len(set(tool_call_trace)) >= 2
            and bool(reply)
            and "[error]" not in reply
            and "task may be incomplete" not in reply
            and "waiting for your approval" not in reply
        )

    def _record_procedure(self, user_message: str, tool_call_trace: list[str]) -> None:
        """Persist a successful tool sequence without another LLM request."""
        try:
            signature = user_message.strip()[:500]
            if not signature:
                return
            threshold = float(
                getattr(self, "_procedure_config", {}).get("similarity_threshold", 0.85)
            )
            candidates = self.procedure_signatures.search(signature, k=5)
            matching_id: int | None = None
            for candidate in candidates:
                if float(candidate.get("raw_similarity", 0.0)) < threshold:
                    continue
                procedure_id = (candidate.get("metadata") or {}).get("procedure_id")
                if procedure_id is not None:
                    matching_id = int(procedure_id)
                    break
            if matching_id is not None:
                self.store.mark_procedure_used(matching_id)
                return
            procedure_id = self.store.add_procedure(signature, tool_call_trace)
            self.procedure_signatures.add_memory(
                signature,
                metadata={"procedure_id": procedure_id, "importance": 3},
            )
        except Exception as exc:
            # Procedure learning must never change a successful user-facing reply.
            print(f"[memory] Could not record procedure: {exc}")

    def _slow_tool_timeout_seconds(self, tool_name: str) -> int | None:
        """Return only an explicitly configured slow-tool budget, if any."""
        slow_tools = self.settings.raw.get("slow_tools", {})
        configured = slow_tools.get(tool_name, {}) if isinstance(slow_tools, dict) else {}
        if not isinstance(configured, dict):
            return None
        try:
            return max(1, int(configured["max_seconds"])) if "max_seconds" in configured else None
        except (TypeError, ValueError):
            return None

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
