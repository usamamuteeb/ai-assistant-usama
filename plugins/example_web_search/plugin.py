"""Example plugin — proves the extension pattern described in the README.

Any folder under plugins/ with a plugin.py exposing register() is auto-loaded
by src/tools/registry.py at startup. No core code needs to change.

This one is a stub (no real network call, since this environment has none) —
swap the body of run() for a real request (e.g. to a search API, or your own
scraping code) and it becomes a working tool with zero other changes needed.
"""
from __future__ import annotations

from typing import Any

from src.tools.base import Tool


class WebSearchStubTool(Tool):
    name = "web_search_stub"
    description = "Placeholder web search tool returning a stub result until a real search API is connected."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    def run(self, query: str) -> Any:
        return {
            "note": "This is a stub. Implement a real search call here.",
            "query": query,
        }


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    return [WebSearchStubTool()]
