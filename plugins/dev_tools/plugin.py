"""Explicitly unsandboxed developer tools for local debugging and repair."""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.tools.base import Tool

ConfirmFn = Callable[[str], bool]
_DENIED = {"error": "Action not performed: confirmation denied or not provided."}
_MAX_SEARCH_RESULTS = 200
_PROCESS_TIMEOUT_SECONDS = 120
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _plugin_config(root: Path) -> dict[str, Any]:
    """Load only this plugin's settings, keeping plugin discovery independent."""
    try:
        with (root / "config.yaml").open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        section = config.get("dev_tools", {}) if isinstance(config, dict) else {}
        return section if isinstance(section, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"dev_tools: could not read config.yaml ({exc}); using safe defaults.")
        return {}


def _absolute_path(value: str, allowed_root: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("An absolute path is required.")
    resolved = path.resolve(strict=False)
    if not _is_within_root(resolved, allowed_root):
        raise ValueError(f"Access outside the permitted root '{allowed_root}' is not allowed.")
    return resolved


def _is_within_root(path: Path, allowed_root: str) -> bool:
    """Use path-aware comparison so C:\\Users is not confused with C:\\Users2."""
    target = os.path.normcase(os.path.abspath(path))
    root = os.path.normcase(os.path.abspath(Path(allowed_root).expanduser()))
    try:
        return os.path.commonpath([target, root]) == root
    except ValueError:  # Different Windows drives cannot share a common path.
        return False


def _is_protected_path(path: Path, protected_prefixes: list[str]) -> bool:
    """Check a path against configured prefixes without substring false positives."""
    target = os.path.normcase(os.path.abspath(path))
    for raw_prefix in protected_prefixes:
        prefix = os.path.normcase(os.path.abspath(Path(raw_prefix).expanduser()))
        try:
            if os.path.commonpath([target, prefix]) == prefix:
                return True
        except ValueError:  # Different Windows drives cannot share a common path.
            continue
    return False


def _read_text_file(path: Path) -> str | dict[str, str]:
    try:
        with path.open("rb") as handle:
            if b"\x00" in handle.read(8192):
                return {"error": "Binary file — not readable as text."}
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": "Binary file — not readable as text."}
    except OSError as exc:
        return {"error": f"Could not read '{path}': {exc}"}


class ReadAnyFileTool(Tool):
    name = "read_file_anywhere"
    description = "Read a UTF-8 text file at an absolute path anywhere on the local machine. Binary files are refused."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute path to a text file."}},
        "required": ["path"],
    }

    def __init__(self, allowed_root: str = "C:\\"):
        self.allowed_root = allowed_root

    def run(self, path: str) -> Any:
        try:
            target = _absolute_path(path, self.allowed_root)
            if not target.exists():
                return {"error": f"File '{target}' does not exist."}
            if not target.is_file():
                return {"error": f"Path '{target}' is not a file."}
            content = _read_text_file(target)
            return content if isinstance(content, dict) else {"path": str(target), "content": content}
        except Exception as exc:
            return {"error": f"Could not read file: {exc}"}


class ListAnyDirectoryTool(Tool):
    name = "list_directory_anywhere"
    description = "List file and folder entries at an absolute directory path anywhere on the local machine."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute directory path."}},
        "required": ["path"],
    }

    def __init__(self, allowed_root: str = "C:\\"):
        self.allowed_root = allowed_root

    def run(self, path: str) -> Any:
        try:
            target = _absolute_path(path, self.allowed_root)
            if not target.exists():
                return {"error": f"Directory '{target}' does not exist."}
            if not target.is_dir():
                return {"error": f"Path '{target}' is not a directory."}
            entries: list[dict[str, Any]] = []
            for entry in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
                try:
                    entries.append({"name": entry.name, "is_dir": entry.is_dir(), "size": entry.stat().st_size})
                except OSError:
                    entries.append({"name": entry.name, "is_dir": entry.is_dir(), "size": None})
            return {"path": str(target), "count": len(entries), "entries": entries}
        except Exception as exc:
            return {"error": f"Could not list directory: {exc}"}


class SearchFilesTool(Tool):
    name = "search_files"
    description = "Recursively find files by filename glob and/or UTF-8 text content below an absolute root directory. Returns at most 200 matches."
    input_schema = {
        "type": "object",
        "properties": {
            "root_directory": {"type": "string", "description": "Absolute directory to search recursively."},
            "filename_glob": {"type": "string", "description": "Optional filename glob, for example '*.py'."},
            "text": {"type": "string", "description": "Optional text to find in UTF-8 file contents."},
        },
        "required": ["root_directory"],
    }

    def __init__(self, allowed_root: str = "C:\\"):
        self.allowed_root = allowed_root

    def run(self, root_directory: str, filename_glob: str = "", text: str = "") -> Any:
        try:
            root = _absolute_path(root_directory, self.allowed_root)
            if not root.exists() or not root.is_dir():
                return {"error": f"Search root '{root}' is not an existing directory."}
            glob = filename_glob.strip()
            needle = text if text else ""
            if not glob and not needle:
                return {"error": "Provide filename_glob and/or text."}

            matches: list[str] = []
            for path in root.rglob(glob or "*"):
                if len(matches) >= _MAX_SEARCH_RESULTS:
                    break
                try:
                    if not path.is_file() or (glob and not path.match(glob)):
                        continue
                    if needle:
                        content = _read_text_file(path)
                        if isinstance(content, dict) or needle not in content:
                            continue
                    matches.append(str(path))
                except OSError:
                    continue
            return {"root_directory": str(root), "count": len(matches), "matches": matches, "capped": len(matches) >= _MAX_SEARCH_RESULTS}
        except Exception as exc:
            return {"error": f"Could not search files: {exc}"}


class _ConfirmedDevTool(Tool):
    def __init__(
        self,
        confirm_fn: Optional[ConfirmFn] = None,
        protected_prefixes: Optional[list[str]] = None,
        allowed_root: str = "C:\\",
    ):
        self.confirm_fn = confirm_fn or (lambda _: False)
        self.protected_prefixes = protected_prefixes or []
        self.allowed_root = allowed_root

    def _confirmed(self, message: str) -> bool:
        return bool(self.confirm_fn(message))

    def _protected(self, path: Path) -> bool:
        return _is_protected_path(path, self.protected_prefixes)


class WriteAnyFileTool(_ConfirmedDevTool):
    name = "write_file_anywhere"
    description = "Write or overwrite a UTF-8 text file at an absolute path anywhere on the machine. Always requires manual approval."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to write."},
            "content": {"type": "string", "description": "Complete UTF-8 text content to write."},
        },
        "required": ["path", "content"],
    }

    def run(self, path: str, content: str) -> Any:
        try:
            target = _absolute_path(path, self.allowed_root)
            if self._protected(target):
                return {"error": "Refused: this path is in the protected-paths blocklist in config.yaml."}
            if not self._confirmed(f"Write or overwrite file '{target}'?"):
                return _DENIED
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return {"status": "written", "path": str(target), "bytes": len(content.encode("utf-8"))}
        except Exception as exc:
            return {"error": f"Could not write file: {exc}"}


class DeleteAnyFileTool(_ConfirmedDevTool):
    name = "delete_file_anywhere"
    description = "Delete one file at an absolute path anywhere on the machine. Directories are refused. Always requires manual approval."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute path of the file to delete."}},
        "required": ["path"],
    }

    def run(self, path: str) -> Any:
        try:
            target = _absolute_path(path, self.allowed_root)
            if self._protected(target):
                return {"error": "Refused: this path is in the protected-paths blocklist in config.yaml."}
            if target.exists() and target.is_dir():
                return {"error": "Refused: delete_file_anywhere only deletes files, not directories."}
            if not target.exists():
                return {"error": f"File '{target}' does not exist."}
            if not self._confirmed(f"Delete file '{target}' permanently?"):
                return _DENIED
            target.unlink()
            return {"status": "deleted", "path": str(target)}
        except Exception as exc:
            return {"error": f"Could not delete file: {exc}"}


def _run_process(command: list[str], working_directory: Path) -> dict[str, Any]:
    is_windows = platform.system() == "Windows"
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=str(working_directory),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
            start_new_session=not is_windows,
        )
    except OSError as exc:
        return {"error": f"Could not start code process: {exc}", "stdout": "", "stderr": str(exc), "exit_code": None}
    try:
        stdout, stderr = process.communicate(timeout=_PROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        if is_windows:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, text=True, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.kill()
        stdout, stderr = process.communicate()
        return {
            "error": f"Code timed out after {_PROCESS_TIMEOUT_SECONDS}s and was forcibly terminated (including child processes).",
            "stdout": stdout[-8000:],
            "stderr": stderr[-8000:],
            "exit_code": process.returncode,
        }
    return {"exit_code": process.returncode, "stdout": stdout[-8000:], "stderr": stderr[-8000:]}


class RunCodeTool(_ConfirmedDevTool):
    name = "run_code"
    description = "Run a Python, JavaScript, or shell code snippet in an optional absolute working directory. This executes arbitrary code and always requires manual approval."
    input_schema = {
        "type": "object",
        "properties": {
            "language": {"type": "string", "enum": ["python", "javascript", "shell"], "description": "Snippet language."},
            "code": {"type": "string", "description": "Code to execute."},
            "working_directory": {"type": "string", "description": "Optional absolute working directory; a temporary directory is used when omitted."},
        },
        "required": ["language", "code"],
    }

    def run(self, language: str, code: str, working_directory: str = "") -> Any:
        language = language.strip().lower()
        if language not in {"python", "javascript", "shell"}:
            return {"error": "language must be one of: python, javascript, shell."}
        if not self._confirmed(f"Run {language} code in '{working_directory or 'a temporary directory'}'?"):
            return _DENIED
        try:
            if working_directory:
                working_path = _absolute_path(working_directory, self.allowed_root)
                if not working_path.exists() or not working_path.is_dir():
                    return {"error": f"Working directory '{working_path}' is not an existing directory."}
            else:
                working_path = Path(tempfile.gettempdir())

            suffix = {"python": ".py", "javascript": ".js", "shell": ".ps1" if platform.system() == "Windows" else ".sh"}[language]
            with tempfile.TemporaryDirectory(prefix="personal-ai-assistant-") as temporary_directory:
                script_path = Path(temporary_directory) / f"snippet{suffix}"
                script_path.write_text(code, encoding="utf-8")
                if language == "python":
                    command = [sys.executable, str(script_path)]
                elif language == "javascript":
                    node = shutil.which("node")
                    if not node:
                        return {"error": "JavaScript execution requires Node.js, but the 'node' command was not found on PATH."}
                    command = [node, str(script_path)]
                elif platform.system() == "Windows":
                    command = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script_path)]
                else:
                    command = ["/bin/sh", str(script_path)]
                result = _run_process(command, working_path)
                result.update({"language": language, "working_directory": str(working_path)})
                return result
        except Exception as exc:
            return {"error": f"Could not run code: {exc}", "stdout": "", "stderr": str(exc), "exit_code": None}


class OpenInVSCodeTool(Tool):
    name = "open_in_vscode"
    description = "Open an existing absolute file or folder path in VS Code, optionally jumping to a one-based line number."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute file or folder path to open."},
            "line": {"type": "integer", "minimum": 1, "description": "Optional one-based line number for a file."},
        },
        "required": ["path"],
    }

    def __init__(self, allowed_root: str = "C:\\"):
        self.allowed_root = allowed_root

    def run(self, path: str, line: int | None = None) -> Any:
        try:
            target = _absolute_path(path, self.allowed_root)
            if not target.exists():
                return {"error": f"Path '{target}' does not exist."}
            code_cli = shutil.which("code")
            if not code_cli:
                return {"error": "VS Code's 'code' command was not found on PATH. In VS Code, run 'Shell Command: Install code command in PATH' from the Command Palette, then restart the assistant."}
            command = [code_cli]
            if line is not None:
                if not target.is_file():
                    return {"error": "A line number can only be used with a file path."}
                command.extend(["-g", f"{target}:{max(1, int(line))}"])
            else:
                command.append(str(target))
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"status": "opened", "path": str(target), "line": line}
        except Exception as exc:
            return {"error": f"Could not open VS Code: {exc}"}


def _forced_manual_confirmation(root: Path, confirm_fn: Optional[ConfirmFn]) -> ConfirmFn:
    config = _plugin_config(root)
    if not config.get("force_manual_confirmation", True):
        return confirm_fn or (lambda _: False)
    manual_confirm = getattr(confirm_fn, "manual_confirm", None)
    if callable(manual_confirm):
        return manual_confirm
    print("dev_tools: force_manual_confirmation is enabled but no manual confirmation bridge is available; actions will be denied.")
    return lambda _: False


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    config = _plugin_config(PROJECT_ROOT)
    allowed_root = str(config.get("allowed_root", "C:\\"))
    protected_prefixes = [str(path) for path in config.get("protected_path_prefixes", [])]
    manual_confirm = _forced_manual_confirmation(PROJECT_ROOT, confirm_fn)
    return [
        ReadAnyFileTool(allowed_root=allowed_root),
        ListAnyDirectoryTool(allowed_root=allowed_root),
        SearchFilesTool(allowed_root=allowed_root),
        WriteAnyFileTool(confirm_fn=manual_confirm, protected_prefixes=protected_prefixes, allowed_root=allowed_root),
        DeleteAnyFileTool(confirm_fn=manual_confirm, protected_prefixes=protected_prefixes, allowed_root=allowed_root),
        RunCodeTool(confirm_fn=manual_confirm, protected_prefixes=protected_prefixes, allowed_root=allowed_root),
        OpenInVSCodeTool(allowed_root=allowed_root),
    ]
