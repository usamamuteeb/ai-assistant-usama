"""Lets the model deliberately search its own long-term memory instead of relying
only on whatever the orchestrator auto-injects into context."""
from __future__ import annotations

from typing import Any

from .base import Tool


class MemorySearchTool(Tool):
    name = "search_memory"
    description = (
        "Search long-term memory for past notes, facts, or conversation snippets "
        "relevant to a query. Use this when you need something you were told "
        "previously but it isn't in the current conversation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "k": {"type": "integer", "description": "Number of results, default 5."},
        },
        "required": ["query"],
    }

    def __init__(self, vector_store):
        self.vector_store = vector_store

    def run(self, query: str, k: int = 5) -> Any:
        results = self.vector_store.search(query, k=k)
        return {"results": results}
