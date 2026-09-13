"""Core local tools for conversation, semantic, episodic, and procedural memory."""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

from ..memory.store import SqliteStore
from ..memory.vector_store import VectorMemory, blended_search
from .base import Tool


def _retrieval_kwargs(config: dict[str, Any] | None) -> dict[str, float]:
    cfg = config or {}
    defaults = {"similarity_weight": 0.6, "recency_weight": 0.2, "importance_weight": 0.2, "half_life_days": 14.0}
    values: dict[str, float] = {}
    for key, default in defaults.items():
        try:
            values[key] = float(cfg.get(key, default))
        except (AttributeError, TypeError, ValueError):
            values[key] = default
    return values


def _search(memory: VectorMemory, query: str, k: int, config: dict[str, Any] | None, where=None) -> list[dict[str, Any]]:
    return blended_search(memory, query, k, where=where, **_retrieval_kwargs(config))


class MemorySearchTool(Tool):
    """Existing assistant-memory search, now using blended local ranking."""

    name = "search_memory"
    description = "Search long-term memory for notes, facts, or conversation snippets missing from the current context."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "k": {"type": "integer", "description": "Number of results, default 5."}},
        "required": ["query"],
    }

    def __init__(self, vector_store: VectorMemory, retrieval_config: dict[str, Any] | None = None):
        self.vector_store = vector_store
        self.retrieval_config = retrieval_config or {}

    def run(self, query: str, k: int = 5) -> Any:
        try:
            return {"results": _search(self.vector_store, query, k, self.retrieval_config)}
        except Exception as exc:
            return {"error": f"Could not search memory: {exc}"}


class RememberFactTool(Tool):
    name = "remember_fact"
    description = "Store a durable user preference, standing fact, or project context for future conversations. Use sparingly for information likely to matter again."
    input_schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}, "category": {"type": "string", "description": "Optional category such as preference, standing_fact, or project_context."}},
        "required": ["content"],
    }

    def __init__(self, semantic_memory: VectorMemory):
        self.semantic_memory = semantic_memory

    def run(self, content: str, category: str = "standing_fact") -> Any:
        text = str(content or "").strip()
        if not text:
            return {"error": "content cannot be empty."}
        category = str(category or "standing_fact").strip() or "standing_fact"
        try:
            fact_id = self.semantic_memory.add_memory(text, {"category": category})
            return {"status": "remembered", "id": fact_id, "content": text, "category": category}
        except Exception as exc:
            return {"error": f"Could not remember fact: {exc}"}


class RecallFactsTool(Tool):
    name = "recall_facts"
    description = "Recall durable saved facts and preferences using blended semantic, recency, and importance ranking."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 5}, "category": {"type": "string", "description": "Optional exact category filter."}},
        "required": ["query"],
    }

    def __init__(self, semantic_memory: VectorMemory, retrieval_config: dict[str, Any] | None = None):
        self.semantic_memory = semantic_memory
        self.retrieval_config = retrieval_config or {}

    def run(self, query: str, k: int = 5, category: str | None = None) -> Any:
        try:
            where = {"category": str(category)} if category else None
            return {"results": _search(self.semantic_memory, query, k, self.retrieval_config, where=where)}
        except Exception as exc:
            return {"error": f"Could not recall facts: {exc}"}


class ForgetFactTool(Tool):
    name = "forget_fact"
    description = "Forget the closest matching durable fact by query, or remove a fact whose content exactly matches content."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Find and remove the closest fact."}, "content": {"type": "string", "description": "Remove a fact only when its stored content exactly matches."}},
        "required": [],
    }

    def __init__(self, semantic_memory: VectorMemory, retrieval_config: dict[str, Any] | None = None):
        self.semantic_memory = semantic_memory
        self.retrieval_config = retrieval_config or {}

    def run(self, query: str | None = None, content: str | None = None) -> Any:
        if not str(query or "").strip() and not str(content or "").strip():
            return {"error": "Provide query or exact content to forget."}
        try:
            matches = self.semantic_memory.find_exact(str(content).strip()) if content and str(content).strip() else _search(self.semantic_memory, str(query).strip(), 1, self.retrieval_config)
            if not matches:
                return {"found": False, "message": "No matching fact was found."}
            fact = matches[0]
            self.semantic_memory.delete_memory(str(fact["id"]))
            return {"status": "forgotten", "found": True, "content": fact.get("text", ""), "id": fact["id"]}
        except Exception as exc:
            return {"error": f"Could not forget fact: {exc}"}


class LogEpisodeTool(Tool):
    name = "log_episode"
    description = "Record a significant outcome, decision, solved problem, or milestone. Do not use for routine exchanges."
    input_schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "outcome": {"type": "string", "enum": ["success", "failure", "ongoing"]}, "importance": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3}},
        "required": ["summary"],
    }

    def __init__(self, episodic_memory: VectorMemory):
        self.episodic_memory = episodic_memory

    def run(self, summary: str, outcome: str = "ongoing", importance: int = 3) -> Any:
        text = str(summary or "").strip()
        if not text:
            return {"error": "summary cannot be empty."}
        outcome = str(outcome or "ongoing").strip().lower()
        if outcome not in {"success", "failure", "ongoing"}:
            return {"error": "outcome must be success, failure, or ongoing."}
        try:
            importance = int(importance)
        except (TypeError, ValueError):
            return {"error": "importance must be an integer from 1 to 5."}
        if importance not in range(1, 6):
            return {"error": "importance must be an integer from 1 to 5."}
        try:
            episode_id = self.episodic_memory.add_memory(text, {"outcome": outcome, "importance": importance})
            return {"status": "logged", "id": episode_id, "summary": text, "outcome": outcome, "importance": importance}
        except Exception as exc:
            return {"error": f"Could not log episode: {exc}"}


class RecallEpisodesTool(Tool):
    name = "recall_episodes"
    description = "Recall significant past decisions, outcomes, and milestones using blended local ranking."
    input_schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "k": {"type": "integer", "default": 5}},
        "required": ["query"],
    }

    def __init__(self, episodic_memory: VectorMemory, retrieval_config: dict[str, Any] | None = None):
        self.episodic_memory = episodic_memory
        self.retrieval_config = retrieval_config or {}

    def run(self, query: str, k: int = 5) -> Any:
        try:
            return {"results": _search(self.episodic_memory, query, k, self.retrieval_config)}
        except Exception as exc:
            return {"error": f"Could not recall episodes: {exc}"}


class RecallProcedureTool(Tool):
    name = "recall_procedure"
    description = "Suggest a previously successful multi-tool procedure for a similar task. It is guidance only and never executes steps automatically."
    input_schema = {
        "type": "object",
        "properties": {"task_description": {"type": "string"}},
        "required": ["task_description"],
    }

    def __init__(self, procedure_signatures: VectorMemory, store: SqliteStore, retrieval_config: dict[str, Any] | None = None, similarity_threshold: float = 0.85):
        self.procedure_signatures = procedure_signatures
        self.store = store
        self.retrieval_config = retrieval_config or {}
        self.similarity_threshold = float(similarity_threshold)
        procedural_config = self.retrieval_config.get("procedural_memory", {})
        if not isinstance(procedural_config, dict):
            procedural_config = {}
        try:
            self.fallback_similarity_threshold = float(
                procedural_config.get("fallback_similarity_threshold", 0.70)
            )
        except (TypeError, ValueError):
            self.fallback_similarity_threshold = 0.70

    def run(self, task_description: str) -> Any:
        text = str(task_description or "").strip()
        if not text:
            return {"error": "task_description cannot be empty."}
        try:
            candidates = _search(self.procedure_signatures, text, 5, self.retrieval_config)
            candidates = [
                item for item in candidates
                if (item.get("metadata") or {}).get("procedure_id") is not None
            ]
            matches = [
                item for item in candidates
                if float(item.get("raw_similarity", 0.0)) >= self.similarity_threshold
            ]
            confidence = "high"
            if not matches:
                matches = [
                    item for item in candidates
                    if float(item.get("raw_similarity", 0.0)) >= self.fallback_similarity_threshold
                ]
                confidence = "low"
            if not matches:
                return {"found": False}
            best = max(matches, key=lambda item: float(item.get("raw_similarity", 0.0)))
            procedure = self.store.get_procedure(int(best["metadata"]["procedure_id"]))
            if procedure is None:
                return {"found": False}
            return {"found": True, "confidence": confidence, "task_signature": procedure["task_signature"], "steps": procedure["steps"], "success_count": procedure["success_count"], "similarity": best["raw_similarity"]}
        except Exception as exc:
            return {"error": f"Could not recall procedure: {exc}"}


def _iso_timestamp(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _content_preview(value: Any, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


class ListMemoriesTool(Tool):
    name = "list_memories"
    description = "List recent entries from semantic, episodic, or procedural memory for inspection and management."
    input_schema = {
        "type": "object",
        "properties": {
            "layer": {"type": "string", "enum": ["semantic", "episodic", "procedural"]},
            "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
        },
        "required": ["layer"],
    }

    def __init__(self, semantic_memory: VectorMemory, episodic_memory: VectorMemory, procedure_signatures: VectorMemory, store: SqliteStore):
        self.semantic_memory = semantic_memory
        self.episodic_memory = episodic_memory
        self.procedure_signatures = procedure_signatures
        self.store = store

    def run(self, layer: str, limit: int = 20) -> Any:
        layer = str(layer or "").strip().lower()
        if layer not in {"semantic", "episodic", "procedural"}:
            return {"error": "layer must be semantic, episodic, or procedural."}
        try:
            limit = min(max(1, int(limit)), 100)
            if layer == "procedural":
                with self.store._lock:
                    rows = self.store.conn.execute(
                        "SELECT id, task_signature, success_count, created_at "
                        "FROM procedures ORDER BY created_at DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                entries = [
                    {
                        "id": int(row["id"]),
                        "content_preview": _content_preview(row["task_signature"]),
                        "timestamp": _iso_timestamp(row["created_at"]),
                        "success_count": int(row["success_count"] or 0),
                    }
                    for row in rows
                ]
            else:
                memory = self.semantic_memory if layer == "semantic" else self.episodic_memory
                entries = []
                for item in memory.list_entries(limit):
                    metadata = item.get("metadata") or {}
                    entry = {
                        "id": item["id"],
                        "content_preview": _content_preview(item.get("text")),
                        "timestamp": _iso_timestamp(metadata.get("created_at")),
                    }
                    if layer == "semantic":
                        entry["category"] = metadata.get("category")
                    else:
                        entry["outcome"] = metadata.get("outcome")
                        entry["importance"] = metadata.get("importance", 0.5)
                    entries.append(entry)
            return {"layer": layer, "entries": entries, "count": len(entries)}
        except Exception as exc:
            return {"error": f"Could not list {layer} memories: {exc}"}


class RemoveMemoryTool(Tool):
    name = "remove_memory"
    description = "Remove one specific semantic, episodic, or procedural memory entry by the stable id returned by list_memories."
    input_schema = {
        "type": "object",
        "properties": {
            "layer": {"type": "string", "enum": ["semantic", "episodic", "procedural"]},
            "id": {"type": "string", "description": "Stable id returned by list_memories."},
        },
        "required": ["layer", "id"],
    }

    def __init__(self, semantic_memory: VectorMemory, episodic_memory: VectorMemory, procedure_signatures: VectorMemory, store: SqliteStore):
        self.semantic_memory = semantic_memory
        self.episodic_memory = episodic_memory
        self.procedure_signatures = procedure_signatures
        self.store = store

    def run(self, layer: str, id: str) -> Any:
        layer = str(layer or "").strip().lower()
        identifier = str(id or "").strip()
        if layer not in {"semantic", "episodic", "procedural"}:
            return {"error": "layer must be semantic, episodic, or procedural."}
        if not identifier:
            return {"error": "id cannot be empty."}
        try:
            if layer in {"semantic", "episodic"}:
                memory = self.semantic_memory if layer == "semantic" else self.episodic_memory
                removed = memory.delete_memory(identifier)
                return {"layer": layer, "id": identifier, "removed": removed}

            try:
                procedure_id = int(identifier)
            except (TypeError, ValueError):
                return {"error": "procedural memory id must be an integer."}
            with self.store._lock:
                row = self.store.conn.execute(
                    "SELECT id FROM procedures WHERE id = ?", (procedure_id,)
                ).fetchone()
                if row is None:
                    return {"layer": layer, "id": procedure_id, "removed": False}
                self.store.conn.execute("DELETE FROM procedures WHERE id = ?", (procedure_id,))
                self.store.conn.commit()
            vector_count = self.procedure_signatures.delete_where({"procedure_id": procedure_id})
            return {"layer": layer, "id": procedure_id, "removed": True, "signature_entries_removed": vector_count}
        except Exception as exc:
            return {"error": f"Could not remove {layer} memory: {exc}"}
