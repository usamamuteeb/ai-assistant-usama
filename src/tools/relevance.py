"""Cheap local selection of tool schemas relevant to the current request."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_STOPWORDS = {
    "a", "am", "an", "and", "are", "can", "could", "do", "does", "for", "from",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "please", "the", "to",
    "what", "when", "where", "which", "who", "with", "you", "your",
}
_WORD_RE = re.compile(r"\w+")


def _keywords(text: str) -> set[str]:
    return {word for word in _WORD_RE.findall(text.lower()) if word not in _STOPWORDS}


def select_relevant_tools(
    user_message: str,
    all_tools: list[dict],
    max_tools: int,
    min_tools: int,
    core_tool_names: set[str],
) -> list[dict]:
    """Select relevant schemas without changing any schema contents."""
    message_keywords = _keywords(user_message)
    core = [tool for tool in all_tools if tool.get("name") in core_tool_names]
    remaining = [tool for tool in all_tools if tool.get("name") not in core_tool_names]
    ranked = sorted(
        remaining,
        key=lambda tool: (
            -len(message_keywords & _keywords(f"{tool.get('name', '')} {tool.get('description', '')}")),
            all_tools.index(tool),
        ),
    )
    # Core tools are mandatory, while non-core tools fill the configured floor
    # without allowing the total selection to exceed max_tools.
    target_total = min(max_tools, max(min_tools, len(core)))
    remaining_slots = max(0, target_total - len(core))
    selected = core + ranked[:remaining_slots]
    selected_names = {id(tool) for tool in selected}
    return [tool for tool in all_tools if id(tool) in selected_names]


def log_selection(
    user_message: str,
    all_tools: list[dict],
    selected_tools: list[dict],
) -> None:
    """Log the selected tool names and estimated schema-token savings."""
    all_tokens = sum(len(json.dumps(tool)) for tool in all_tools) // 4
    selected_tokens = sum(len(json.dumps(tool)) for tool in selected_tools) // 4
    saved_tokens = max(0, all_tokens - selected_tokens)
    logger.info(
        "Tool filter: sent %d/%d tools for message %r (saved ~%d tokens, est.); selected: %s",
        len(selected_tools),
        len(all_tools),
        user_message,
        saved_tokens,
        ", ".join(tool.get("name", "") for tool in selected_tools),
    )
    print(
        f"Tool filter: sent {len(selected_tools)}/{len(all_tools)} tools for message "
        f"{user_message!r} (saved ~{saved_tokens} tokens, est.); selected: "
        f"{', '.join(tool.get('name', '') for tool in selected_tools)}"
    )
