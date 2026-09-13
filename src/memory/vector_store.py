"""Semantic long-term memory via Chroma, running fully locally (no API calls,
no network needed) using Chroma's built-in default embedding function.
"""
from __future__ import annotations

import time
import uuid
from math import isfinite
from pathlib import Path
from typing import Any


class VectorMemory:
    def __init__(
        self,
        persist_path: Path,
        collection_name: str = "assistant_memory",
        distance_space: str | None = None,
    ):
        import chromadb  # lazy import: only needed once you actually use memory

        self.client = chromadb.PersistentClient(path=str(persist_path))
        self.collection_name = collection_name
        self.distance_space = distance_space
        # Do not alter metadata on existing collections. In particular,
        # assistant_memory may predate this code and use Chroma's default
        # distance space. New procedure signatures use cosine distance so the
        # configured similarity threshold has a clear interpretation.
        try:
            self.collection = self.client.get_collection(collection_name)
        except Exception:
            if distance_space:
                self.collection = self.client.get_or_create_collection(
                    collection_name,
                    metadata={"hnsw:space": distance_space},
                )
            else:
                self.collection = self.client.get_or_create_collection(collection_name)

    def add_memory(self, text: str, metadata: dict[str, Any] | None = None) -> str:
        doc_id = str(uuid.uuid4())
        meta = dict(metadata or {})
        meta.setdefault("created_at", time.time())
        self.collection.add(documents=[text], ids=[doc_id], metadatas=[meta])
        return doc_id

    def search(
        self,
        query: str,
        k: int = 5,
        where: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        total = self._matching_count(where)
        if total == 0:
            return []
        k = min(max(1, int(k)), total)
        kwargs: dict[str, Any] = {"query_texts": [query], "n_results": k}
        if where:
            kwargs["where"] = where
        results = self.collection.query(**kwargs)
        out = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        for doc_id, doc, meta, dist in zip(ids, docs, metas, dists):
            distance = float(dist) if isinstance(dist, (int, float)) else 0.0
            out.append(
                {
                    "id": doc_id,
                    "text": doc,
                    "metadata": meta or {},
                    "distance": distance,
                    "raw_similarity": self.similarity_from_distance(distance),
                }
            )
        return out

    def find_exact(self, text: str) -> list[dict[str, Any]]:
        """Return entries whose stored document exactly equals ``text``."""
        if self.collection.count() == 0:
            return []
        results = self.collection.get(include=["documents", "metadatas"])
        matches: list[dict[str, Any]] = []
        for doc_id, document, metadata in zip(
            results.get("ids", []),
            results.get("documents", []),
            results.get("metadatas", []),
        ):
            if document == text:
                matches.append({"id": doc_id, "text": document, "metadata": metadata or {}})
        return matches

    def delete_memory(self, doc_id: str) -> bool:
        """Delete one entry and report whether it existed."""
        existing = self.collection.get(ids=[doc_id], include=["metadatas"])
        ids = existing.get("ids", [])
        if not ids:
            return False
        self.collection.delete(ids=[doc_id])
        return True

    def list_entries(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent entries with stable ids for local memory management."""
        limit = min(max(1, int(limit)), 100)
        if self.collection.count() == 0:
            return []
        records = self.collection.get(limit=limit, include=["documents", "metadatas"])
        entries = []
        for doc_id, document, metadata in zip(
            records.get("ids", []),
            records.get("documents", []),
            records.get("metadatas", []),
        ):
            entries.append(
                {
                    "id": doc_id,
                    "text": document or "",
                    "metadata": metadata or {},
                }
            )
        return sorted(
            entries,
            key=lambda item: float((item.get("metadata") or {}).get("created_at", 0) or 0),
            reverse=True,
        )

    def delete_where(self, where: dict[str, Any]) -> int:
        """Delete all entries matching a simple Chroma metadata filter."""
        records = self.collection.get(where=where, include=["metadatas"])
        ids = records.get("ids", [])
        if ids:
            self.collection.delete(ids=ids)
        return len(ids)

    def similarity_from_distance(self, distance: float) -> float:
        """Return a cosine similarity for cosine collections when available."""
        if self.distance_space == "cosine" and isfinite(distance):
            return max(0.0, min(1.0, 1.0 - distance))
        return 0.0

    def _matching_count(self, where: dict[str, Any] | None) -> int:
        if not where:
            return int(self.collection.count())
        try:
            records = self.collection.get(where=where, include=["metadatas"])
            return len(records.get("ids", []))
        except Exception:
            # Chroma supports the simple equality filters used by these
            # collections. This fallback keeps old Chroma versions usable.
            return int(self.collection.count())


def blended_search(
    vector_memory: VectorMemory,
    query: str,
    k: int,
    similarity_weight: float = 0.6,
    recency_weight: float = 0.2,
    importance_weight: float = 0.2,
    half_life_days: float = 14,
    where: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank local vector matches by similarity, recency, and importance.

    The candidate lookup remains Chroma's semantic retrieval. The second pass
    is deterministic, local math only, and keeps metadata-free legacy memory
    records useful through neutral defaults.
    """
    requested = max(1, int(k))
    candidates = vector_memory.search(query, k=requested * 4, where=where)
    if not candidates:
        return []

    distances = [max(0.0, float(item.get("distance", 0.0))) for item in candidates]
    max_distance = max(distances) if distances else 0.0
    now = time.time()
    half_life = max(float(half_life_days), 0.001)
    weights = [max(0.0, float(similarity_weight)), max(0.0, float(recency_weight)), max(0.0, float(importance_weight))]
    weight_total = sum(weights) or 1.0

    ranked: list[dict[str, Any]] = []
    for item in candidates:
        metadata = item.get("metadata") or {}
        distance = max(0.0, float(item.get("distance", 0.0)))
        similarity_score = 1.0 if max_distance == 0 else max(0.0, 1.0 - (distance / max_distance))
        try:
            created_at = float(metadata.get("created_at", now))
        except (TypeError, ValueError):
            created_at = now
        age_days = max(0.0, (now - created_at) / 86400)
        recency_score = 0.5 ** (age_days / half_life)
        importance_score = _importance_score(metadata.get("importance"))
        final_score = (
            weights[0] * similarity_score
            + weights[1] * recency_score
            + weights[2] * importance_score
        ) / weight_total
        ranked.append(
            {
                **item,
                "similarity_score": similarity_score,
                "recency_score": recency_score,
                "importance_score": importance_score,
                "score": final_score,
            }
        )
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[:requested]


def _importance_score(value: Any) -> float:
    if value is None:
        return 0.5
    try:
        importance = float(value)
    except (TypeError, ValueError):
        return 0.5
    # Explicit episode/turn importance uses the documented 1–5 scale. Values
    # strictly below one remain supported for any legacy/custom metadata that
    # already stores a normalized score.
    if 0.0 <= importance < 1.0:
        return importance
    return max(0.0, min(1.0, (importance - 1.0) / 4.0))
