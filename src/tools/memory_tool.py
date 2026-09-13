"""Core local tools for conversation, semantic, episodic, and procedural memory."""
from __future__ import annotations

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

    def run(self, task_description: str) -> Any:
        text = str(task_description or "").strip()
        if not text:
            return {"error": "task_description cannot be empty."}
        try:
            candidates = _search(self.procedure_signatures, text, 5, self.retrieval_config)
            matches = [item for item in candidates if float(item.get("raw_similarity", 0.0)) >= self.similarity_threshold and (item.get("metadata") or {}).get("procedure_id") is not None]
            if not matches:
                return {"found": False}
            best = max(matches, key=lambda item: float(item.get("raw_similarity", 0.0)))
            procedure = self.store.get_procedure(int(best["metadata"]["procedure_id"]))
            if procedure is None:
                return {"found": False}
            return {"found": True, "task_signature": procedure["task_signature"], "steps": procedure["steps"], "success_count": procedure["success_count"], "similarity": best["raw_similarity"]}
        except Exception as exc:
            return {"error": f"Could not recall procedure: {exc}"}
