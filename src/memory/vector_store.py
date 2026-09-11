"""Semantic long-term memory via Chroma, running fully locally (no API calls,
no network needed) using Chroma's built-in default embedding function.
"""
from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any


class VectorMemory:
    def __init__(self, persist_path: Path, collection_name: str = "assistant_memory"):
        import chromadb  # lazy import: only needed once you actually use memory

        self.client = chromadb.PersistentClient(path=str(persist_path))
        self.collection = self.client.get_or_create_collection(collection_name)

    def add_memory(self, text: str, metadata: dict[str, Any] | None = None) -> str:
        doc_id = str(uuid.uuid4())
        meta = dict(metadata or {})
        meta.setdefault("created_at", time.time())
        self.collection.add(documents=[text], ids=[doc_id], metadatas=[meta])
        return doc_id

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        if self.collection.count() == 0:
            return []
        k = min(k, self.collection.count())
        results = self.collection.query(query_texts=[query], n_results=k)
        out = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            out.append({"text": doc, "metadata": meta, "distance": dist})
        return out
