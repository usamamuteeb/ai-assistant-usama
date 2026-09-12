"""Read and write the system clipboard."""
from __future__ import annotations

from typing import Any

import pyperclip

from src.tools.base import Tool


class ReadClipboardTool(Tool):
    name = "read_clipboard"
    description = "Read the current system clipboard contents as text."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        return {"content": pyperclip.paste()}


class WriteClipboardTool(Tool):
    name = "write_clipboard"
    description = "Replace the system clipboard contents with the provided text."
    input_schema = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to place on the clipboard."}},
        "required": ["text"],
    }

    def run(self, text: str) -> Any:
        pyperclip.copy(text)
        return {"status": "written", "length": len(text)}


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    return [ReadClipboardTool(), WriteClipboardTool()]
