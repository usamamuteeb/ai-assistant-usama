"""Local document ingestion for the assistant knowledge base."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from src.memory.store import SqliteStore
from src.memory.vector_store import VectorMemory
from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md"}


class KnowledgeBaseTool(Tool):
    name = "ingest_knowledge_base"
    description = (
        "Index PDF, TXT, and Markdown documents from the workspace knowledge_base folder. "
        "This is a local mechanical operation and does not require model interaction."
    )
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, root: Path = PROJECT_ROOT):
        self.root = root
        self._config = self._load_config()
        self.workspace_root = self._workspace_root()
        folder = self._configured_value("knowledge_base", "folder", "knowledge_base")
        self.knowledge_base_dir = self._workspace_child_path(str(folder))
        self.knowledge_base_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteStore(self._configured_path("memory", "sqlite_path", "data/sqlite.db"))
        self.vector_memory = VectorMemory(
            self._configured_path("memory", "chroma_path", "data/chroma"),
            str(self._configured_value(
                "knowledge_base", "chroma_collection", "assistant_knowledge_base"
            )),
        )
        self.chunk_size = _positive_int(
            self._configured_value("knowledge_base", "chunk_size", 1400), 1400
        )
        self.chunk_overlap = _nonnegative_int(
            self._configured_value("knowledge_base", "chunk_overlap", 180), 180
        )

    def run(self) -> Any:
        try:
            previous_hashes = self.store.get_state("kb_file_hashes", {})
            if not isinstance(previous_hashes, dict):
                previous_hashes = {}

            ingested_files: list[str] = []
            skipped_unchanged: list[str] = []
            failed_files: list[dict[str, str]] = []
            total_chunks_added = 0
            current_hashes: dict[str, str] = {}

            for path in sorted(self.knowledge_base_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                filename = path.name
                try:
                    digest = _sha256(path)
                    if previous_hashes.get(filename) == digest:
                        current_hashes[filename] = digest
                        skipped_unchanged.append(filename)
                        continue

                    text = _extract_text(path)
                    chunks = _chunks(text, self.chunk_size, self.chunk_overlap)
                    if chunks:
                        for index, chunk in enumerate(chunks):
                            self.vector_memory.add_memory(
                                chunk,
                                metadata={"source": filename, "chunk": index},
                            )
                        total_chunks_added += len(chunks)
                    current_hashes[filename] = digest
                    ingested_files.append(filename)
                except (UnicodeDecodeError, UnicodeEncodeError) as exc:
                    # Keep the batch moving and leave this file out of the
                    # successful hash set so a later run retries it.
                    failed_files.append({"file": filename, "error": str(exc)})

            self.store.set_state("kb_file_hashes", current_hashes)
            return {
                "ingested_files": ingested_files,
                "skipped_unchanged": skipped_unchanged,
                "total_chunks_added": total_chunks_added,
                "failed_files": failed_files,
            }
        except Exception as exc:  # plugin errors become model/UI-visible data
            return {"error": f"Knowledge-base ingestion failed: {exc}"}

    def _workspace_root(self) -> Path:
        configured = self._configured_value("filesystem_tool", "workspace_root", "workspace")
        path = Path(configured)
        return path if path.is_absolute() else self.root / path

    def _configured_path(self, section: str, key: str, default: str) -> Path:
        configured = self._configured_value(section, key, default)
        path = Path(configured)
        return path if path.is_absolute() else self.root / path

    def _configured_value(self, section: str, key: str, default: Any) -> Any:
        values = self._config.get(section, {})
        return values.get(key, default) if isinstance(values, dict) else default

    def _workspace_child_path(self, relative_path: str) -> Path:
        path = (self.workspace_root / relative_path).resolve()
        workspace = self.workspace_root.resolve()
        if not path.is_relative_to(workspace):
            raise ValueError("knowledge_base.folder must remain inside the workspace root.")
        return path

    def _load_config(self) -> dict[str, Any]:
        config_path = self.root / "config.yaml"
        if not config_path.exists():
            return {}
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return config if isinstance(config, dict) else {}


class KnowledgeBaseSearchTool(Tool):
    name = "search_knowledge_base"
    description = (
        "Search documents already ingested into the local knowledge base using semantic search. "
        "Use this for questions about knowledge-base PDFs, TXT, or Markdown documents; "
        "do not read the raw document with filesystem tools unless the user explicitly asks "
        "for the file contents."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Question or information to find."},
            "k": {"type": "integer", "description": "Maximum number of matching chunks, default 5."},
        },
        "required": ["query"],
    }

    def __init__(self, vector_memory: VectorMemory):
        self.vector_memory = vector_memory

    def run(self, query: str, k: int = 5) -> Any:
        try:
            if not query or not query.strip():
                return {"error": "A search query is required."}
            return {"results": self.vector_memory.search(query.strip(), k=max(1, int(k)))}
        except Exception as exc:
            return {"error": f"Knowledge-base search failed: {exc}"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_text(path: Path) -> str:
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8", errors="replace")
    from pypdf import PdfReader

    return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _chunks(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    normalized = " ".join(text.split())
    if not normalized:
        return []
    step = max(1, chunk_size - min(chunk_overlap, chunk_size - 1))
    return [normalized[start : start + chunk_size] for start in range(0, len(normalized), step)]


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _nonnegative_int(value: Any, default: int) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    ingest_tool = KnowledgeBaseTool()
    return [ingest_tool, KnowledgeBaseSearchTool(ingest_tool.vector_memory)]
