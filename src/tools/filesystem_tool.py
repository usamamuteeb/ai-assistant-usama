"""Read/write/list files in the workspace and configured user folders."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable

from .base import Tool


class FilesystemTool(Tool):
    name = "filesystem"
    description = (
        "Read, write, list, or delete files in the workspace and configured Windows user folders. "
        "Absolute paths are supported there; deleting or replacing a file requires confirmation."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write", "list", "delete"]},
            "path": {
                "type": "string",
                "description": "A workspace-relative path or an absolute path inside a configured allowed folder.",
            },
            "content": {"type": "string", "description": "Content to write (only for action='write')."},
        },
        "required": ["action", "path"],
    }

    def __init__(
        self,
        workspace_root: Path,
        allowed_roots: Iterable[str | Path] | None = None,
        confirm_fn: Callable[[str], bool] | None = None,
    ):
        self.root = workspace_root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        configured_roots = [Path(value).expanduser().resolve() for value in (allowed_roots or [])]
        # The workspace remains available for relative paths even when no
        # extra folders are configured.
        self.allowed_roots = [self.root, *configured_roots]
        self.confirm_fn = confirm_fn or (lambda _: False)

    def _resolve(self, path_value: str) -> Path:
        supplied = Path(path_value).expanduser()
        candidate = (supplied if supplied.is_absolute() else self.root / supplied).resolve()
        # Path.parents comparisons are not reliably case-insensitive on Windows.
        # Normalize both sides and use commonpath so configured user folders such
        # as C:\\Users\\HP\\Downloads work consistently with absolute paths.
        def is_within(candidate_path: Path, root_path: Path) -> bool:
            try:
                return os.path.commonpath(
                    [os.path.normcase(os.path.abspath(str(candidate_path))),
                     os.path.normcase(os.path.abspath(str(root_path)))]
                ) == os.path.normcase(os.path.abspath(str(root_path)))
            except ValueError:
                # Different drives (or malformed paths) are never contained.
                return False

        if not any(is_within(candidate, root) for root in self.allowed_roots):
            roots = ", ".join(str(root) for root in self.allowed_roots)
            raise PermissionError(f"Path '{path_value}' is outside the configured filesystem roots ({roots}) — refused.")
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
            if target.exists() and target.is_dir():
                return {"error": f"'{path}' is a directory, not a file."}
            if target.exists() and not self.confirm_fn(f"Replace file '{target}'?"):
                return {"error": "Action not performed: confirmation denied or not provided."}
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
            try:
                reported_path = str(target.relative_to(self.root))
            except ValueError:
                reported_path = str(target)
            return {"status": "written", "path": reported_path}

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
            if not self.confirm_fn(f"Delete file '{target}' permanently?"):
                return {"error": "Action not performed: confirmation denied or not provided."}
            target.unlink()
            return {"status": "deleted", "path": path}

        return {"error": f"Unknown action '{action}'."}
