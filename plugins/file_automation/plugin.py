"""File and document automation tools for the local machine.

The plugin deliberately accepts absolute paths anywhere on the configured
system drive so it can work with Desktop, Downloads, Documents, Pictures,
Videos, and Music. The protected-path list remains owned by ``dev_tools`` and
is read from that config section rather than duplicated here.
"""
from __future__ import annotations

import os
import platform
import shutil
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ConfirmFn = Callable[[str], bool]
_DENIED = {"error": "Action not performed: confirmation denied or not provided."}
_PROTECTED_ERROR = {"error": "Refused: this path is in the protected-paths blocklist in config.yaml."}
_PANDOC_TIMEOUT_SECONDS = 120


def _load_config(root: Path) -> dict[str, Any]:
    try:
        with (root / "config.yaml").open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return config if isinstance(config, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"file_automation: could not read config.yaml ({exc}); using safe defaults.")
        return {}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    if value is None:
        return default
    return bool(value)


def _is_within_root(path: Path, root: str | Path) -> bool:
    target = os.path.normcase(os.path.abspath(path))
    allowed = os.path.normcase(os.path.abspath(Path(root).expanduser()))
    try:
        return os.path.commonpath([target, allowed]) == allowed
    except ValueError:
        return False


def _is_protected_path(path: Path, protected_prefixes: list[str]) -> bool:
    target = os.path.normcase(os.path.abspath(path))
    for raw_prefix in protected_prefixes:
        prefix = os.path.normcase(os.path.abspath(Path(raw_prefix).expanduser()))
        try:
            if os.path.commonpath([target, prefix]) == prefix:
                return True
        except ValueError:
            continue
    return False


def _absolute_path(value: str, allowed_root: str | Path) -> Path:
    path = Path(str(value or "")).expanduser()
    if not path.is_absolute():
        raise ValueError("An absolute path is required. Use a full path such as C:\\Users\\HP\\Downloads\\file.txt.")
    resolved = path.resolve(strict=False)
    if not _is_within_root(resolved, allowed_root):
        raise ValueError(f"Access is limited to '{allowed_root}'.")
    return resolved


@dataclass(frozen=True)
class _Context:
    root: Path
    allowed_root: Path
    workspace_root: Path
    backup_root: Path
    protected_prefixes: list[str]
    normal_confirm: ConfirmFn
    forced_confirm: ConfirmFn

    def protected(self, *paths: Path) -> bool:
        return any(_is_protected_path(path, self.protected_prefixes) for path in paths)


def _context(root: Path, confirm_fn: Optional[ConfirmFn]) -> _Context:
    config = _load_config(root)
    file_config = config.get("file_automation", {})
    file_config = file_config if isinstance(file_config, dict) else {}
    dev_config = config.get("dev_tools", {})
    dev_config = dev_config if isinstance(dev_config, dict) else {}

    allowed_value = "C:\\" if platform.system() == "Windows" else os.path.abspath(os.sep)
    allowed_root = Path(str(file_config.get("allowed_root", allowed_value))).expanduser()
    workspace_value = config.get("filesystem_tool", {}).get("workspace_root", "workspace")
    workspace_root = Path(str(workspace_value))
    if not workspace_root.is_absolute():
        workspace_root = root / workspace_root
    backup_value = file_config.get("backup_folder", "file_backups")
    backup_root = Path(str(backup_value))
    if not backup_root.is_absolute():
        backup_root = workspace_root / backup_root

    normal_confirm = confirm_fn or (lambda _: False)
    if _as_bool(file_config.get("force_manual_confirmation"), default=True):
        manual_confirm = getattr(confirm_fn, "manual_confirm", None)
        if callable(manual_confirm):
            forced_confirm = manual_confirm
        else:
            print(
                "file_automation: force_manual_confirmation is enabled but no manual "
                "confirmation bridge is available; overwrite actions will be denied."
            )
            forced_confirm = lambda _: False
    else:
        forced_confirm = normal_confirm

    protected = dev_config.get("protected_path_prefixes", [])
    protected_prefixes = [str(prefix) for prefix in protected] if isinstance(protected, list) else []
    workspace_root.mkdir(parents=True, exist_ok=True)
    return _Context(
        root=root,
        allowed_root=allowed_root,
        workspace_root=workspace_root,
        backup_root=backup_root,
        protected_prefixes=protected_prefixes,
        normal_confirm=normal_confirm,
        forced_confirm=forced_confirm,
    )


def _guard(context: _Context, *paths: Path) -> dict[str, str] | None:
    if context.protected(*paths):
        return dict(_PROTECTED_ERROR)
    return None


def _backup_existing(context: _Context, original: Path) -> Path:
    """Copy an existing destination before an overwrite and return its path."""
    safe_absolute = str(original).replace("\\", "_").replace("/", "_").replace(":", "")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(":", "-")
    backup_dir = context.backup_root / safe_absolute
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{timestamp}_{original.name}"
    shutil.copy2(original, backup_path)
    return backup_path


def _dry_run(action: str, target: Path, overwrite: bool, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "dry_run": True,
        action: extra.pop("operation", f"{target}"),
        "would_create_backup": overwrite,
        "would_overwrite": overwrite,
    }
    result.update(extra)
    return result


def _destination_file(path: Path) -> dict[str, str] | None:
    if path.exists() and path.is_dir():
        return {"error": f"Destination '{path}' is an existing directory; provide a file path."}
    return None


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


class RenameFileTool(Tool):
    name = "rename_file"
    description = "Rename a file anywhere on the C: drive. Overwrites require confirmation and are backed up first; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the existing file."},
            "new_name": {"type": "string", "description": "New filename only, not a directory path."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["path", "new_name"],
    }

    def __init__(self, context: _Context):
        self.context = context

    def run(self, path: str, new_name: str, dry_run: bool = False) -> Any:
        try:
            source = _absolute_path(path, self.context.allowed_root)
            name_path = Path(str(new_name or "")).expanduser()
            if name_path.is_absolute() or name_path.name != str(new_name) or str(new_name) in {"", ".", ".."}:
                return {"error": "new_name must be a filename only, without directory components."}
            destination = source.parent / str(new_name)
            blocked = _guard(self.context, source, destination)
            if blocked:
                return blocked
            if not source.exists() or not source.is_file():
                return {"error": f"Source file '{source}' does not exist."}
            invalid = _destination_file(destination)
            if invalid:
                return invalid
            if source == destination:
                return {"error": "The new filename is the same as the current filename."}
            overwrite = destination.exists()
            if dry_run:
                return _dry_run(
                    "would_rename",
                    destination,
                    overwrite,
                    operation=f"{source} -> {destination}",
                    source=str(source),
                    destination=str(destination),
                )
            if not self.context.forced_confirm(
                f"Rename '{source}' to '{destination}'"
                + (" and overwrite the existing file?" if overwrite else "?")
            ):
                return dict(_DENIED)
            backup = _backup_existing(self.context, destination) if overwrite else None
            _ensure_parent(destination)
            if overwrite:
                destination.unlink()
            source.rename(destination)
            result = {"status": "renamed", "source": str(source), "destination": str(destination)}
            if backup:
                result["backup"] = str(backup)
            return result
        except Exception as exc:
            return {"error": f"Could not rename file: {exc}"}


class MoveFileTool(Tool):
    name = "move_file"
    description = "Move a file anywhere on the C: drive. Overwrites require confirmation and are backed up first; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Absolute source file path."},
            "destination": {"type": "string", "description": "Absolute destination file path."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["source", "destination"],
    }

    def __init__(self, context: _Context):
        self.context = context

    def run(self, source: str, destination: str, dry_run: bool = False) -> Any:
        try:
            source_path = _absolute_path(source, self.context.allowed_root)
            destination_path = _absolute_path(destination, self.context.allowed_root)
            blocked = _guard(self.context, source_path, destination_path)
            if blocked:
                return blocked
            if not source_path.exists() or not source_path.is_file():
                return {"error": f"Source file '{source_path}' does not exist."}
            invalid = _destination_file(destination_path)
            if invalid:
                return invalid
            if source_path == destination_path:
                return {"error": "Source and destination are the same file."}
            overwrite = destination_path.exists()
            if dry_run:
                return _dry_run(
                    "would_move",
                    destination_path,
                    overwrite,
                    operation=f"{source_path} -> {destination_path}",
                    source=str(source_path),
                    destination=str(destination_path),
                )
            if not self.context.forced_confirm(
                f"Move '{source_path}' to '{destination_path}'"
                + (" and overwrite the existing file?" if overwrite else "?")
            ):
                return dict(_DENIED)
            backup = _backup_existing(self.context, destination_path) if overwrite else None
            _ensure_parent(destination_path)
            if overwrite:
                destination_path.unlink()
            shutil.move(str(source_path), str(destination_path))
            result = {"status": "moved", "source": str(source_path), "destination": str(destination_path)}
            if backup:
                result["backup"] = str(backup)
            return result
        except Exception as exc:
            return {"error": f"Could not move file: {exc}"}


class CopyFileTool(Tool):
    name = "copy_file"
    description = "Copy a file anywhere on the C: drive. New destinations are additive; overwrites require confirmation and are backed up first; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Absolute source file path."},
            "destination": {"type": "string", "description": "Absolute destination file path."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["source", "destination"],
    }

    def __init__(self, context: _Context):
        self.context = context

    def run(self, source: str, destination: str, dry_run: bool = False) -> Any:
        try:
            source_path = _absolute_path(source, self.context.allowed_root)
            destination_path = _absolute_path(destination, self.context.allowed_root)
            blocked = _guard(self.context, source_path, destination_path)
            if blocked:
                return blocked
            if not source_path.exists() or not source_path.is_file():
                return {"error": f"Source file '{source_path}' does not exist."}
            invalid = _destination_file(destination_path)
            if invalid:
                return invalid
            if source_path == destination_path:
                return {"error": "Source and destination are the same file."}
            overwrite = destination_path.exists()
            if dry_run:
                return _dry_run(
                    "would_copy",
                    destination_path,
                    overwrite,
                    operation=f"{source_path} -> {destination_path}",
                    source=str(source_path),
                    destination=str(destination_path),
                )
            if overwrite and not self.context.forced_confirm(f"Copy '{source_path}' to '{destination_path}' and overwrite the existing file?"):
                return dict(_DENIED)
            backup = _backup_existing(self.context, destination_path) if overwrite else None
            _ensure_parent(destination_path)
            shutil.copy2(source_path, destination_path)
            result = {"status": "copied", "source": str(source_path), "destination": str(destination_path)}
            if backup:
                result["backup"] = str(backup)
            return result
        except Exception as exc:
            return {"error": f"Could not copy file: {exc}"}


class CreateFolderTool(Tool):
    name = "create_folder"
    description = "Create a folder anywhere on the C: drive. This additive action does not require confirmation."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute folder path to create."}},
        "required": ["path"],
    }

    def __init__(self, context: _Context):
        self.context = context

    def run(self, path: str) -> Any:
        try:
            target = _absolute_path(path, self.context.allowed_root)
            blocked = _guard(self.context, target)
            if blocked:
                return blocked
            existed = target.exists()
            if existed and not target.is_dir():
                return {"error": f"Path '{target}' exists and is not a folder."}
            target.mkdir(parents=True, exist_ok=True)
            return {"status": "already_exists" if existed else "created", "path": str(target)}
        except Exception as exc:
            return {"error": f"Could not create folder: {exc}"}


class _ReportTool(Tool):
    extension = ""

    def __init__(self, context: _Context):
        self.context = context

    def _target(self, path: str) -> Path:
        target = _absolute_path(path, self.context.allowed_root)
        if target.suffix.lower() != self.extension:
            target = target.with_suffix(self.extension) if target.suffix == "" else Path(str(target) + self.extension)
        return target

    def _prepare(self, target: Path, dry_run: bool, action: str, title: str, content: str) -> dict[str, Any] | None:
        blocked = _guard(self.context, target)
        if blocked:
            return blocked
        overwrite = target.exists()
        invalid = _destination_file(target)
        if invalid:
            return invalid
        if dry_run:
            return _dry_run(
                action,
                target,
                overwrite,
                operation=f"create {target}",
                path=str(target),
                title=title,
            )
        if overwrite and not self.context.normal_confirm(f"Create report '{target}' and overwrite the existing file?"):
            return dict(_DENIED)
        backup = _backup_existing(self.context, target) if overwrite else None
        _ensure_parent(target)
        self._write(target, title, content)
        result: dict[str, Any] = {"status": "created", "path": str(target)}
        if backup:
            result["backup"] = str(backup)
        return result

    def _write(self, target: Path, title: str, content: str) -> None:
        raise NotImplementedError


class CreateMarkdownReportTool(_ReportTool):
    name = "create_markdown_report"
    extension = ".md"
    description = "Create a UTF-8 Markdown report anywhere on the C: drive; overwrites require confirmation and an automatic backup; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute output path, normally ending in .md."},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Markdown body."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["path", "title", "content"],
    }

    def run(self, path: str, title: str, content: str, dry_run: bool = False) -> Any:
        try:
            return self._prepare(self._target(path), dry_run, "would_create_markdown", title, content)
        except Exception as exc:
            return {"error": f"Could not create Markdown report: {exc}"}

    def _write(self, target: Path, title: str, content: str) -> None:
        target.write_text(f"# {title.strip()}\n\n{content.rstrip()}\n", encoding="utf-8")


class CreatePdfReportTool(_ReportTool):
    name = "create_pdf_report"
    extension = ".pdf"
    description = "Create a readable PDF report using fpdf2; overwrites require confirmation and an automatic backup; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute output path, normally ending in .pdf."},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Plain text or simple markdown-like body."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["path", "title", "content"],
    }

    def run(self, path: str, title: str, content: str, dry_run: bool = False) -> Any:
        try:
            return self._prepare(self._target(path), dry_run, "would_create_pdf", title, content)
        except Exception as exc:
            return {"error": f"Could not create PDF report: {exc}"}

    def _write(self, target: Path, title: str, content: str) -> None:
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.set_margins(18, 18, 18)
        pdf.add_page()
        font_path = Path("C:/Windows/Fonts/arial.ttf")
        if font_path.exists():
            pdf.add_font("ArialUnicode", "", str(font_path))
            pdf.add_font("ArialUnicode", "B", str(Path("C:/Windows/Fonts/arialbd.ttf")))
            font_name = "ArialUnicode"
        else:
            font_name = "Helvetica"

        def safe_text(value: str) -> str:
            if font_name == "Helvetica":
                return value.encode("latin-1", errors="replace").decode("latin-1")
            return value

        pdf.set_font(font_name, "B", 20)
        pdf.multi_cell(0, 11, safe_text(title.strip()))
        pdf.ln(5)
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                pdf.ln(3)
                continue
            if stripped.startswith("#"):
                level = min(len(stripped) - len(stripped.lstrip("#")), 3)
                heading = stripped[level:].strip()
                pdf.set_font(font_name, "B", max(12, 17 - level * 2))
                pdf.multi_cell(0, 8, safe_text(heading))
                pdf.ln(1)
            else:
                pdf.set_font(font_name, "", 11)
                pdf.multi_cell(0, 6, safe_text(stripped))
                pdf.ln(1)
        pdf.output(str(target))


class CreateWordDocumentTool(_ReportTool):
    name = "create_word_document"
    extension = ".docx"
    description = "Create a readable Word document using python-docx; overwrites require confirmation and an automatic backup; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute output path, normally ending in .docx."},
            "title": {"type": "string"},
            "content": {"type": "string", "description": "Plain text or simple markdown-like body."},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["path", "title", "content"],
    }

    def run(self, path: str, title: str, content: str, dry_run: bool = False) -> Any:
        try:
            return self._prepare(self._target(path), dry_run, "would_create_word_document", title, content)
        except Exception as exc:
            return {"error": f"Could not create Word document: {exc}"}

    def _write(self, target: Path, title: str, content: str) -> None:
        from docx import Document

        document = Document()
        document.add_heading(title.strip(), level=0)
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                document.add_paragraph()
                continue
            if stripped.startswith("#"):
                level = min(len(stripped) - len(stripped.lstrip("#")), 3)
                document.add_heading(stripped[level:].strip(), level=level)
            else:
                document.add_paragraph(stripped)
        document.save(str(target))


class ExtractPdfTablesTool(Tool):
    name = "extract_pdf_tables"
    description = "Extract every table from a PDF at an absolute C: drive path using pdfplumber."
    input_schema = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Absolute path to a PDF file."}},
        "required": ["path"],
    }

    def __init__(self, context: _Context):
        self.context = context

    def run(self, path: str) -> Any:
        try:
            target = _absolute_path(path, self.context.allowed_root)
            blocked = _guard(self.context, target)
            if blocked:
                return blocked
            if not target.exists() or not target.is_file():
                return {"error": f"PDF file '{target}' does not exist."}
            if target.suffix.lower() != ".pdf":
                return {"error": "extract_pdf_tables requires a .pdf file."}
            import pdfplumber

            tables: list[dict[str, Any]] = []
            with pdfplumber.open(str(target)) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    for table_index, rows in enumerate(page.extract_tables() or [], start=1):
                        normalized = [["" if cell is None else str(cell) for cell in row] for row in rows]
                        tables.append({"page_number": page_number, "table_index": table_index, "rows": normalized})
            if not tables:
                return {"tables": [], "note": "No tables detected in this PDF."}
            return {"tables": tables, "count": len(tables), "path": str(target)}
        except Exception as exc:
            return {"error": f"Could not extract PDF tables: {exc}"}


class ConvertDocumentTool(Tool):
    name = "convert_document"
    description = "Convert a document with pandoc to pdf, docx, txt, or html. New outputs are additive; overwrites require confirmation and an automatic backup; supports dry_run."
    input_schema = {
        "type": "object",
        "properties": {
            "source_path": {"type": "string", "description": "Absolute source document path."},
            "target_format": {"type": "string", "enum": ["pdf", "docx", "txt", "html"]},
            "dry_run": {"type": "boolean", "default": False},
        },
        "required": ["source_path", "target_format"],
    }

    def __init__(self, context: _Context):
        self.context = context

    def run(self, source_path: str, target_format: str, dry_run: bool = False) -> Any:
        try:
            source = _absolute_path(source_path, self.context.allowed_root)
            target_format = str(target_format or "").strip().lower().lstrip(".")
            if target_format not in {"pdf", "docx", "txt", "html"}:
                return {"error": "target_format must be one of: pdf, docx, txt, html."}
            blocked = _guard(self.context, source)
            if blocked:
                return blocked
            if not source.exists() or not source.is_file():
                return {"error": f"Source document '{source}' does not exist."}
            target = source.with_suffix(f".{target_format}")
            blocked = _guard(self.context, target)
            if blocked:
                return blocked
            if target == source:
                return {"error": "The target format is the same as the source format."}
            pandoc = _find_pandoc()
            if pandoc is None:
                return {
                    "error": (
                        "Pandoc is required for convert_document but was not found on PATH. "
                        "Install it free from https://pandoc.org/installing.html, then restart the assistant."
                    )
                }
            invalid = _destination_file(target)
            if invalid:
                return invalid
            overwrite = target.exists()
            if dry_run:
                return _dry_run(
                    "would_convert",
                    target,
                    overwrite,
                    operation=f"{source} -> {target}",
                    source_path=str(source),
                    destination=str(target),
                    target_format=target_format,
                )
            if overwrite and not self.context.forced_confirm(f"Convert '{source}' to '{target}' and overwrite the existing file?"):
                return dict(_DENIED)
            backup = _backup_existing(self.context, target) if overwrite else None
            _ensure_parent(target)
            result = _run_pandoc(pandoc, source, target, self.context.allowed_root)
            if result.get("error"):
                return result
            response: dict[str, Any] = {
                "status": "converted",
                "source": str(source),
                "destination": str(target),
                "target_format": target_format,
            }
            if backup:
                response["backup"] = str(backup)
            return response
        except Exception as exc:
            return {"error": f"Could not convert document: {exc}"}


def _run_pandoc(pandoc: str, source: Path, target: Path, working_directory: Path) -> dict[str, Any]:
    """Run pandoc with the same bounded process-tree behavior as ShellTool."""
    is_windows = platform.system() == "Windows"
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if is_windows else 0
    try:
        process = subprocess.Popen(
            [pandoc, str(source), "-o", str(target)],
            cwd=str(working_directory),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
            start_new_session=not is_windows,
        )
        try:
            stdout, stderr = process.communicate(timeout=_PANDOC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            if is_windows:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.kill()
            stdout, stderr = process.communicate()
            return {
                "error": f"Pandoc timed out after {_PANDOC_TIMEOUT_SECONDS}s and was forcibly terminated.",
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "exit_code": process.returncode,
            }
        if process.returncode != 0:
            return {
                "error": f"Pandoc conversion failed with exit code {process.returncode}.",
                "stdout": stdout[-4000:],
                "stderr": stderr[-4000:],
                "exit_code": process.returncode,
            }
        return {"stdout": stdout[-4000:], "stderr": stderr[-4000:], "exit_code": process.returncode}
    except OSError as exc:
        return {"error": f"Could not run pandoc: {exc}"}


def _find_pandoc() -> str | None:
    """Return a usable Pandoc executable after an explicit version check."""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        return None
    try:
        subprocess.run(
            [pandoc, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=True,
        )
        return pandoc
    except (OSError, subprocess.SubprocessError):
        return None


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    context = _context(PROJECT_ROOT, confirm_fn)
    return [
        RenameFileTool(context),
        MoveFileTool(context),
        CopyFileTool(context),
        CreateFolderTool(context),
        CreateMarkdownReportTool(context),
        CreatePdfReportTool(context),
        CreateWordDocumentTool(context),
        ExtractPdfTablesTool(context),
        ConvertDocumentTool(context),
    ]
