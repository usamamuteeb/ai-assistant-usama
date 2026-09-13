"""Offline coverage for the additive local memory layers."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

from src.memory.store import SqliteStore
from src.memory.vector_store import blended_search
from src.orchestrator import Orchestrator, _turn_importance
from src.tools.memory_tool import ForgetFactTool, RememberFactTool


class FakeVectorMemory:
    def __init__(self, records=None):
        self.records = list(records or [])
        self.deleted: list[str] = []

    def add_memory(self, text, metadata=None):
        identifier = f"id-{len(self.records) + 1}"
        self.records.append(
            {
                "id": identifier,
                "text": text,
                "metadata": {"created_at": time.time(), **(metadata or {})},
                "distance": 0.1,
                "raw_similarity": 0.9,
            }
        )
        return identifier

    def search(self, _query, k=5, where=None):
        matches = self.records
        if where:
            matches = [
                record
                for record in matches
                if all(record["metadata"].get(key) == value for key, value in where.items())
            ]
        return matches[:k]

    def find_exact(self, text):
        return [record for record in self.records if record["text"] == text]

    def delete_memory(self, doc_id):
        self.deleted.append(doc_id)
        self.records = [record for record in self.records if record["id"] != doc_id]


def test_blended_search_can_prioritize_importance():
    vector = FakeVectorMemory(
        [
            {
                "id": "low",
                "text": "deployment outcome",
                "metadata": {"created_at": time.time(), "importance": 1},
                "distance": 0.05,
            },
            {
                "id": "high",
                "text": "deployment outcome",
                "metadata": {"created_at": time.time(), "importance": 5},
                "distance": 0.06,
            },
        ]
    )

    results = blended_search(
        vector,
        "deployment",
        2,
        similarity_weight=0,
        recency_weight=0,
        importance_weight=1,
    )

    assert [result["id"] for result in results] == ["high", "low"]


def test_remember_and_forget_exact_fact_are_local_and_reversible():
    vector = FakeVectorMemory()
    remember = RememberFactTool(vector)
    forget = ForgetFactTool(vector)

    stored = remember.run("User prefers concise answers", category="preference")
    removed = forget.run(content="User prefers concise answers")

    assert stored["status"] == "remembered"
    assert removed["status"] == "forgotten"
    assert vector.deleted == [stored["id"]]


def test_sqlite_procedures_store_steps_and_increment_success_count():
    with tempfile.TemporaryDirectory() as tmpdir:
        with SqliteStore(Path(tmpdir) / "memory.db") as store:
            procedure_id = store.add_procedure("check calendar and email", ["list_events", "search_email"])
            assert store.get_procedure(procedure_id)["steps"] == ["list_events", "search_email"]

            assert store.mark_procedure_used(procedure_id)
            assert store.get_procedure(procedure_id)["success_count"] == 2


def test_automatic_turn_importance_uses_tools_length_and_error_signals():
    basic = _turn_importance("hello", "hi", [])
    significant = _turn_importance("x" * 300, "task may be incomplete: error", ["one", "two"])

    assert basic == 1
    assert significant == 5


def test_orchestrator_records_a_procedure_after_a_successful_multitool_turn():
    with tempfile.TemporaryDirectory() as tmpdir:
        assistant = object.__new__(Orchestrator)
        assistant.store = SqliteStore(Path(tmpdir) / "memory.db")
        assistant.procedure_signatures = FakeVectorMemory()
        assistant._procedure_config = {"similarity_threshold": 0.85}

        assistant._record_procedure("Check calendar and unread email", ["list_events", "search_email"])

        saved = assistant.store.get_procedure(1)
        assert saved is not None
        assert saved["steps"] == ["list_events", "search_email"]
        assert assistant.procedure_signatures.records[0]["metadata"]["procedure_id"] == 1
        assistant.store.close()
