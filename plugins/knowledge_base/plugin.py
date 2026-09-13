"""Local document ingestion and search for the assistant knowledge base."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from src.memory.store import SqliteStore
from src.memory.vector_store import VectorMemory
from src.tools.base import Tool

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SUPPORTED_SUFFIXES = {".pdf", ".txt", ".md", ".docx", ".xlsx", ".pptx"}
_DENIED = {"error": "Action not performed: confirmation denied or not provided."}
ConfirmFn = Callable[[str], bool]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record(value: Any) -> dict[str, Any]:
    """Normalize both the original filename -> hash format and rich records."""
    if isinstance(value, str):
        value = {"content_hash": value}
    if not isinstance(value, dict):
        value = {}
    tags = value.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, list):
        tags = []
    return {
        "content_hash": str(value.get("content_hash", "")),
        "ingested_at": value.get("ingested_at"),
        "chunk_count": int(value.get("chunk_count", 0) or 0),
        "page_count": value.get("page_count"),
        "sheet_count": value.get("sheet_count"),
        "slide_count": value.get("slide_count"),
        "tags": [str(tag) for tag in tags],
        "category": value.get("category"),
    }


def _records(value: Any) -> dict[str, dict[str, Any]]:
    return {str(name): _record(item) for name, item in value.items()} if isinstance(value, dict) else {}


def _text_hash(text: str) -> str:
    # Hash canonical extracted content rather than source bytes (or formatting
    # whitespace), so a re-exported document can still be recognized as a duplicate.
    canonical = " ".join(str(text).split())
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_name(metadata: Any) -> str:
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("source_file") or metadata.get("source") or "")


def _delete_source(vector_memory: Any, filename: str) -> None:
    collection = getattr(vector_memory, "collection", None)
    if collection is None:
        return
    for field in ("source_file", "source"):
        try:
            collection.delete(where={field: filename})
        except Exception:
            continue


def _source_chunks(vector_memory: Any, filename: str) -> list[dict[str, Any]]:
    collection = getattr(vector_memory, "collection", None)
    if collection is None:
        return []
    raw: list[dict[str, Any]] = []
    for field in ("source_file", "source"):
        try:
            result = collection.get(where={field: filename}, include=["documents", "metadatas"])
        except Exception:
            continue
        documents = result.get("documents", []) or []
        metadatas = result.get("metadatas", []) or []
        ids = result.get("ids", []) or []
        for index, document in enumerate(documents):
            metadata = metadatas[index] if index < len(metadatas) else {}
            if _source_name(metadata) != filename:
                continue
            raw.append({"text": document or "", "metadata": metadata or {}, "id": ids[index] if index < len(ids) else None})
        if raw:
            break
    raw.sort(key=lambda item: int(item["metadata"].get("chunk", 0) or 0))
    return raw


def _update_source_category(vector_memory: Any, filename: str, category: Any) -> None:
    collection = getattr(vector_memory, "collection", None)
    if collection is None:
        return
    chunks = _source_chunks(vector_memory, filename)
    ids = [item["id"] for item in chunks if item.get("id")]
    if not ids:
        return
    metadatas = []
    for item in chunks:
        metadata = dict(item["metadata"])
        if category:
            metadata["category"] = str(category)
        else:
            metadata.pop("category", None)
        metadatas.append(metadata)
    try:
        collection.update(ids=ids, metadatas=metadatas)
    except Exception:
        pass


class KnowledgeBaseTool(Tool):
    name = "ingest_knowledge_base"
    description = "Index PDF, TXT, Markdown, DOCX, XLSX, and PPTX documents from the configured workspace knowledge-base folder."
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
            str(self._configured_value("knowledge_base", "chroma_collection", "assistant_knowledge_base")),
        )
        self.chunk_size = _positive_int(self._configured_value("knowledge_base", "chunk_size", 1400), 1400)
        self.chunk_overlap = _nonnegative_int(self._configured_value("knowledge_base", "chunk_overlap", 180), 180)

    def run(self) -> Any:
        try:
            previous = _records(self.store.get_state("kb_file_hashes", {}))
            current: dict[str, dict[str, Any]] = {}
            ingested_files: list[str] = []
            skipped_unchanged: list[str] = []
            duplicates: list[dict[str, str]] = []
            failed_files: list[dict[str, str]] = []
            total_chunks_added = 0

            for path in sorted(self.knowledge_base_dir.iterdir()):
                if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                    continue
                filename = path.name
                prior = previous.get(filename)
                try:
                    document = _extract_document(path)
                    content_hash = _text_hash(document["text"])
                    if prior and prior.get("content_hash") == content_hash:
                        current[filename] = prior
                        skipped_unchanged.append(filename)
                        continue

                    duplicate_of = next(
                        (other for other, item in previous.items()
                         if other != filename and item.get("content_hash") == content_hash),
                        None,
                    )
                    if duplicate_of:
                        _delete_source(self.vector_memory, filename)
                        duplicates.append({"file": filename, "duplicate_of": duplicate_of})
                        continue

                    if prior:
                        _delete_source(self.vector_memory, filename)
                    ingested_at = _now_iso()
                    chunk_index = 0
                    for segment in document["segments"]:
                        for chunk in _chunks(segment["text"], self.chunk_size, self.chunk_overlap):
                            metadata = {
                                "source": filename,
                                "source_file": filename,
                                "chunk": chunk_index,
                                "ingested_at": ingested_at,
                            }
                            metadata.update(segment.get("metadata", {}))
                            category = (prior or {}).get("category")
                            if category:
                                metadata["category"] = str(category)
                            self.vector_memory.add_memory(chunk, metadata=metadata)
                            chunk_index += 1
                    total_chunks_added += chunk_index
                    current[filename] = {
                        "content_hash": content_hash,
                        "ingested_at": ingested_at,
                        "chunk_count": chunk_index,
                        "page_count": document.get("page_count"),
                        "sheet_count": document.get("sheet_count"),
                        "slide_count": document.get("slide_count"),
                        "tags": list((prior or {}).get("tags", [])),
                        "category": (prior or {}).get("category"),
                    }
                    ingested_files.append(filename)
                except (UnicodeDecodeError, UnicodeEncodeError) as exc:
                    # A single malformed/oddly encoded file must not discard
                    # progress from the rest of the batch (Windows terminals
                    # and legacy document encodings can still surface this).
                    failed_files.append({"file": filename, "error": str(exc)})
                    if prior:
                        current[filename] = prior
                except Exception as exc:
                    failed_files.append({"file": filename, "error": str(exc)})
                    if prior:
                        current[filename] = prior

            self.store.set_state("kb_file_hashes", current)
            return {
                "ingested_files": ingested_files,
                "skipped_unchanged": skipped_unchanged,
                "duplicates": duplicates,
                "total_chunks_added": total_chunks_added,
                "failed_files": failed_files,
            }
        except Exception as exc:
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
        if not path.is_relative_to(self.workspace_root.resolve()):
            raise ValueError("knowledge_base.folder must remain inside the workspace root.")
        return path

    def _load_config(self) -> dict[str, Any]:
        config_path = self.root / "config.yaml"
        if not config_path.exists():
            return {}
        with config_path.open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        return config if isinstance(config, dict) else {}


def _parse_date(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _candidate_names(store: Any, filename: str | None, category: str | None,
                     date_after: str | None, date_before: str | None) -> set[str] | None:
    if store is None:
        return None
    state = _records(store.get_state("kb_file_hashes", {}))
    has_filter = any(value is not None for value in (filename, category, date_after, date_before))
    if not state:
        return set() if has_filter else None
    try:
        after = _parse_date(date_after) if date_after else None
        before = _parse_date(date_before) if date_before else None
    except ValueError:
        raise ValueError("date_after and date_before must be ISO date/time strings.")
    names = set()
    for name, record in state.items():
        if filename and filename.casefold() not in name.casefold():
            continue
        if category is not None and record.get("category") != category:
            continue
        ingested = record.get("ingested_at")
        try:
            when = _parse_date(ingested) if ingested else None
        except ValueError:
            when = None
        if after and (when is None or when <= after):
            continue
        if before and (when is None or when >= before):
            continue
        names.add(name)
    return names


def _collection_search(vector_memory: Any, query: str, k: int, names: set[str] | None) -> list[dict[str, Any]]:
    collection = getattr(vector_memory, "collection", None)
    if collection is None or names is None:
        return vector_memory.search(query, k=max(1, int(k)))
    found: list[dict[str, Any]] = []
    for name in names:
        result = None
        for field in ("source_file", "source"):
            try:
                result = collection.query(query_texts=[query], n_results=max(1, int(k)), where={field: name})
                break
            except Exception:
                continue
        if not result:
            continue
        documents = (result.get("documents", [[]]) or [[]])[0] or []
        if not documents:
            # A legacy collection may have only the ``source`` metadata key;
            # an empty query on source_file is not proof that the document is absent.
            continue
        metadatas = (result.get("metadatas", [[]]) or [[]])[0] or []
        distances = (result.get("distances", [[]]) or [[]])[0] or []
        found.extend({"text": doc, "metadata": metadatas[index] if index < len(metadatas) else {}, "distance": distances[index] if index < len(distances) else None} for index, doc in enumerate(documents))
    found.sort(key=lambda item: item.get("distance") if item.get("distance") is not None else float("inf"))
    return found[:max(1, int(k))]


def _filter_results(results: list[dict[str, Any]], names: set[str] | None,
                    filename: str | None, category: str | None,
                    date_after: str | None, date_before: str | None) -> list[dict[str, Any]]:
    after = _parse_date(date_after) if date_after else None
    before = _parse_date(date_before) if date_before else None
    filtered = []
    for item in results:
        metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
        source = _source_name(metadata)
        if names is not None and source not in names:
            continue
        if filename and filename.casefold() not in source.casefold():
            continue
        if category is not None and metadata.get("category") != category:
            continue
        if after or before:
            try:
                when = _parse_date(metadata.get("ingested_at"))
            except (TypeError, ValueError):
                continue
            if after and when <= after:
                continue
            if before and when >= before:
                continue
        filtered.append(item)
    return filtered


class KnowledgeBaseSearchTool(Tool):
    name = "search_knowledge_base"
    description = "Search indexed knowledge-base documents, optionally filtered by filename, category, or ingestion date."
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Question or information to find."},
            "k": {"type": "integer", "description": "Maximum number of matching chunks, default 5."},
            "filename": {"type": "string", "description": "Optional filename substring."},
            "category": {"type": "string", "description": "Optional exact category."},
            "date_after": {"type": "string", "description": "Optional ISO ingestion timestamp lower bound."},
            "date_before": {"type": "string", "description": "Optional ISO ingestion timestamp upper bound."},
        },
        "required": ["query"],
    }

    def __init__(self, vector_memory: Any, store: Any = None):
        self.vector_memory = vector_memory
        self.store = store

    def run(self, query: str, k: int = 5, filename: str | None = None,
            category: str | None = None, date_after: str | None = None,
            date_before: str | None = None) -> Any:
        try:
            if not query or not query.strip():
                return {"error": "A search query is required."}
            names = _candidate_names(self.store, filename, category, date_after, date_before)
            if names == set():
                return {"results": []}
            results = _collection_search(self.vector_memory, query.strip(), k, names)
            if any(value is not None for value in (filename, category, date_after, date_before)):
                results = _filter_results(results, names, filename, category, date_after, date_before)
            return {"results": results}
        except Exception as exc:
            return {"error": f"Knowledge-base search failed: {exc}"}


class ListKnowledgeDocumentsTool(Tool):
    name = "list_knowledge_documents"
    description = "List indexed knowledge-base documents and their ingestion metadata."
    input_schema = {"type": "object", "properties": {}, "required": []}

    def __init__(self, store: Any):
        self.store = store

    def run(self) -> Any:
        try:
            return {"documents": [{"filename": filename, **record} for filename, record in sorted(_records(self.store.get_state("kb_file_hashes", {})).items())]}
        except Exception as exc:
            return {"error": f"Could not list knowledge documents: {exc}"}


class RemoveKnowledgeDocumentTool(Tool):
    name = "remove_knowledge_document"
    description = "Unindex a knowledge document, optionally deleting its workspace file with confirmation."
    input_schema = {"type": "object", "properties": {"filename": {"type": "string"}, "delete_file": {"type": "boolean", "default": False}}, "required": ["filename"]}

    def __init__(self, store: Any, vector_memory: Any, knowledge_base_dir: Path, confirm_fn: Optional[ConfirmFn] = None):
        self.store, self.vector_memory, self.knowledge_base_dir = store, vector_memory, knowledge_base_dir
        self.confirm_fn = confirm_fn or (lambda _: False)

    def run(self, filename: str, delete_file: bool = False) -> Any:
        try:
            filename = Path(str(filename)).name
            state = _records(self.store.get_state("kb_file_hashes", {}))
            if filename not in state:
                return {"error": f"No indexed document named '{filename}' found."}
            if delete_file and not self.confirm_fn(f"Delete the knowledge-base file '{filename}' from disk?"):
                return dict(_DENIED)
            _delete_source(self.vector_memory, filename)
            state.pop(filename, None)
            self.store.set_state("kb_file_hashes", state)
            file_path = (self.knowledge_base_dir / filename).resolve()
            if delete_file:
                if not file_path.is_relative_to(self.knowledge_base_dir.resolve()):
                    return {"error": "Refused: file path escaped the knowledge-base folder."}
                if file_path.exists():
                    file_path.unlink()
            return {"removed": filename, "file_deleted": bool(delete_file)}
        except Exception as exc:
            return {"error": f"Could not remove knowledge document: {exc}"}


class DocumentSummaryTool(Tool):
    name = "document_summary"
    description = "Return the extracted text of one indexed document for the model to summarize."
    input_schema = {"type": "object", "properties": {"filename": {"type": "string"}}, "required": ["filename"]}

    def __init__(self, vector_memory: Any):
        self.vector_memory = vector_memory

    def run(self, filename: str) -> Any:
        try:
            normalized = Path(str(filename)).name
            chunks = _source_chunks(self.vector_memory, normalized)
            if not chunks:
                return {"error": f"No indexed document named '{filename}' found."}
            text = "\n\n".join(str(item["text"]) for item in chunks)
            if len(text) > 15000:
                text = text[:15000] + "... [truncated]"
            return {"filename": normalized, "text": text}
        except Exception as exc:
            return {"error": f"Could not summarize document: {exc}"}


class AskDocumentTool(Tool):
    name = "ask_document"
    description = "Search only one indexed document and return matching chunks with their page metadata."
    input_schema = {"type": "object", "properties": {"filename": {"type": "string"}, "question": {"type": "string"}, "k": {"type": "integer"}}, "required": ["filename", "question"]}

    def __init__(self, vector_memory: Any):
        self.vector_memory = vector_memory

    def run(self, filename: str, question: str, k: int = 5) -> Any:
        try:
            filename = Path(str(filename)).name
            collection = getattr(self.vector_memory, "collection", None)
            results = []
            if collection is not None:
                for field in ("source_file", "source"):
                    try:
                        raw = collection.query(query_texts=[question], n_results=max(1, int(k)), where={field: filename})
                        docs = (raw.get("documents", [[]]) or [[]])[0] or []
                        metas = (raw.get("metadatas", [[]]) or [[]])[0] or []
                        dists = (raw.get("distances", [[]]) or [[]])[0] or []
                        results = [{"text": doc, "metadata": metas[i] if i < len(metas) else {}, "distance": dists[i] if i < len(dists) else None} for i, doc in enumerate(docs)]
                        if results:
                            break
                    except Exception:
                        continue
            else:
                results = [item for item in self.vector_memory.search(question, k=max(1, int(k))) if _source_name(item.get("metadata")) == filename]
            for item in results:
                item["page_number"] = item.get("metadata", {}).get("page_number")
            return {"filename": filename, "results": results}
        except Exception as exc:
            return {"error": f"Document question failed: {exc}"}


class SearchKnowledgeBaseWithCitationsTool(KnowledgeBaseSearchTool):
    name = "search_knowledge_base_with_citations"
    description = "Search the knowledge base and return each result with a filename/page citation when known."

    def run(self, query: str, k: int = 5, filename: str | None = None, category: str | None = None,
            date_after: str | None = None, date_before: str | None = None) -> Any:
        result = super().run(query, k, filename, category, date_after, date_before)
        if result.get("error"):
            return result
        for item in result.get("results", []):
            metadata = item.get("metadata", {})
            source = _source_name(metadata)
            page = metadata.get("page_number")
            item["citation"] = f"{source}, page {page}" if page is not None else source
        return result


class TagDocumentTool(Tool):
    name = "tag_document"
    description = "Update the tags and category stored for an indexed document."
    input_schema = {"type": "object", "properties": {"filename": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "category": {"type": ["string", "null"]}}, "required": ["filename"]}

    def __init__(self, store: Any, vector_memory: Any):
        self.store, self.vector_memory = store, vector_memory

    def run(self, filename: str, tags: list[str] | None = None, category: str | None = None) -> Any:
        try:
            filename = Path(str(filename)).name
            state = _records(self.store.get_state("kb_file_hashes", {}))
            if filename not in state:
                return {"error": f"No indexed document named '{filename}' found."}
            # Omitted optional fields mean "leave this metadata alone"; an
            # explicit empty tag list can still intentionally clear tags.
            if tags is not None:
                state[filename]["tags"] = [str(tag) for tag in tags]
            if category is not None:
                state[filename]["category"] = category
            self.store.set_state("kb_file_hashes", state)
            _update_source_category(self.vector_memory, filename, state[filename]["category"])
            return {"filename": filename, "tags": state[filename]["tags"], "category": state[filename]["category"]}
        except Exception as exc:
            return {"error": f"Could not tag knowledge document: {exc}"}


def _extract_document(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        pages = [page.extract_text() or "" for page in PdfReader(str(path)).pages]
        return {"text": "\n\n".join(pages), "segments": [{"text": text, "metadata": {"page_number": number}} for number, text in enumerate(pages, 1)], "page_count": len(pages)}
    if suffix in {".txt", ".md"}:
        text = _extract_text(path)
        return {"text": text, "segments": [{"text": text, "metadata": {}}]}
    if suffix == ".docx":
        from docx import Document
        text = "\n".join(paragraph.text for paragraph in Document(str(path)).paragraphs)
        return {"text": text, "segments": [{"text": text, "metadata": {}}]}
    if suffix == ".xlsx":
        import openpyxl
        workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        segments = []
        try:
            for sheet in workbook.worksheets:
                rows = ["\t".join("" if cell is None else str(cell) for cell in row) for row in sheet.iter_rows(values_only=True)]
                segments.append({"text": "\n".join(rows), "metadata": {"sheet_name": sheet.title}})
        finally:
            workbook.close()
        return {"text": "\n\n".join(item["text"] for item in segments), "segments": segments, "sheet_count": len(segments)}
    if suffix == ".pptx":
        from pptx import Presentation
        presentation = Presentation(str(path))
        segments = []
        for number, slide in enumerate(presentation.slides, 1):
            texts = [shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False)]
            segments.append({"text": "\n".join(texts), "metadata": {"slide_number": number}})
        return {"text": "\n\n".join(item["text"] for item in segments), "segments": segments, "slide_count": len(segments)}
    raise ValueError(f"Unsupported knowledge-base file type: {suffix}")


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


def register(confirm_fn: Optional[ConfirmFn] = None) -> list[Tool]:
    ingest_tool = KnowledgeBaseTool()
    normal_confirm = confirm_fn or (lambda _: False)
    return [
        ingest_tool,
        KnowledgeBaseSearchTool(ingest_tool.vector_memory, ingest_tool.store),
        ListKnowledgeDocumentsTool(ingest_tool.store),
        RemoveKnowledgeDocumentTool(ingest_tool.store, ingest_tool.vector_memory, ingest_tool.knowledge_base_dir, normal_confirm),
        DocumentSummaryTool(ingest_tool.vector_memory),
        AskDocumentTool(ingest_tool.vector_memory),
        SearchKnowledgeBaseWithCitationsTool(ingest_tool.vector_memory, ingest_tool.store),
        TagDocumentTool(ingest_tool.store, ingest_tool.vector_memory),
    ]
