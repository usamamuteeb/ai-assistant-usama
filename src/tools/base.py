"""Base class every tool (built-in or plugin) implements.

Copy any existing tool as a template for a new one. Keep run() side-effect-safe
and return a JSON-serializable result — it gets fed straight back to the model.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    name: str
    description: str
    input_schema: dict[str, Any]

    @abstractmethod
    def run(self, **kwargs: Any) -> Any:
        """Execute the tool and return a JSON-serializable result."""
        raise NotImplementedError

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }
