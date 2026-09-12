"""Local behavior tests for the knowledge-base ingest tool."""
from __future__ import annotations

from plugins.knowledge_base.plugin import KnowledgeBaseTool


class _StateStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get_state(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def set_state(self, key: str, value: object) -> None:
        self.values[key] = value


class _VectorStore:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict[str, object] | None]] = []

    def add_memory(self, text: str, metadata: dict[str, object] | None = None) -> None:
        self.items.append((text, metadata))


def test_ingest_knowledge_base_skips_unchanged_file(tmp_path):
    (tmp_path / "notes.md").write_text("# Notes\nA reliable local test document.", encoding="utf-8")
    tool = object.__new__(KnowledgeBaseTool)
    tool.knowledge_base_dir = tmp_path
    tool.store = _StateStore()
    tool.vector_memory = _VectorStore()
    tool.chunk_size = 1400
    tool.chunk_overlap = 180

    first = tool.run()
    second = tool.run()

    assert first["ingested_files"] == ["notes.md"]
    assert first["total_chunks_added"] == 1
    assert second["ingested_files"] == []
    assert second["skipped_unchanged"] == ["notes.md"]


def test_configured_chunk_size_is_used_for_ingestion(tmp_path):
    (tmp_path / "config.yaml").write_text(
        """
filesystem_tool:
  workspace_root: workspace
knowledge_base:
  folder: documents
  chroma_collection: assistant_knowledge_base
  chunk_size: 20
  chunk_overlap: 0
""".strip(),
        encoding="utf-8",
    )
    tool = object.__new__(KnowledgeBaseTool)
    tool.root = tmp_path
    tool._config = tool._load_config()
    tool.workspace_root = tool._workspace_root()
    tool.knowledge_base_dir = tool._workspace_child_path(
        str(tool._configured_value("knowledge_base", "folder", "knowledge_base"))
    )
    tool.knowledge_base_dir.mkdir(parents=True)
    tool.knowledge_base_dir.joinpath("chunked.txt").write_text("x" * 55, encoding="utf-8")
    tool.store = _StateStore()
    tool.vector_memory = _VectorStore()
    tool.chunk_size = int(tool._configured_value("knowledge_base", "chunk_size", 1400))
    tool.chunk_overlap = int(tool._configured_value("knowledge_base", "chunk_overlap", 180))

    result = tool.run()

    assert result["total_chunks_added"] == 3
