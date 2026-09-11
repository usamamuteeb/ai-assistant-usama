"""Read/write/list files, sandboxed to a workspace folder so the model can never
touch anything outside it."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Tool


class FilesystemTool(Tool):
    name = "filesystem"
    description = (
        "Read, write, or list files inside the local workspace folder. "
        "action must be one of: 'read', 'write', 'list', 'delete'. "
        "Paths are relative to the workspace root — you cannot access anything outside it."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "list", "delete"]},
            "path": {"type": "string", "description": "Path relative to the workspace root."},
            "content": {"type": "string", "description": "Content to write (only for action='write')."},
        },
        "required": ["action", "path"],
    }

    def __init__(self, workspace_root: Path):
        self.root = workspace_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, rel_path: str) -> Path:
        candidate = (self.root / rel_path).resolve()
        if self.root not in candidate.parents and candidate != self.root:
            raise PermissionError(f"Path '{rel_path}' escapes the workspace root — refused.")
        return candidate

    def run(self, action: str, path: str, content: str | None = None) -> Any:
        try:
            target = self._resolve(path)
        except PermissionError as e:
            return {"error": str(e)}

        if action == "read":
            if not target.exists():
                return {"error": f"'{path}' does not exist."}
            if target.is_dir():
                return {"error": f"'{path}' is a directory, not a file."}
            return {"content": target.read_text(encoding="utf-8", errors="replace")}

        if action == "write":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
            return {"status": "written", "path": str(target.relative_to(self.root))}

        if action == "list":
            if not target.exists():
                return {"error": f"'{path}' does not exist."}
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
            return {"entries": entries}

        if action == "delete":
            if not target.exists():
                return {"error": f"'{path}' does not exist."}
            if target.is_dir():
                return {"error": "Refusing to delete a directory — delete files individually."}
            target.unlink()
            return {"status": "deleted", "path": path}

        return {"error": f"Unknown action '{action}'."}
