"""FAISS-backed vector index that implements shared-libs' ``VectorIndex`` Protocol.

This is the bridge between edge-reco's sync, build-once FAISS index and
``shared_libs_python.vector_mgmt``'s async, lifecycle-managed contract:

- CPU-bound FAISS calls run in ``asyncio.to_thread`` so the event loop never blocks.
- ``delete`` tombstones by id (FlatIP has no native delete); ``search`` over-fetches
  and filters tombstoned rows; ``rebuild`` physically compacts them away.
- ``get_stats`` reports the tombstone ratio that drives ``IndexManager`` rebuilds.

Once constructed it drops straight into shared-libs' ``IndexManager`` and partition
strategies — the whole point of building EdgeProc on top of lego #1.
"""

from __future__ import annotations

import asyncio

import faiss
import numpy as np
from numpy.typing import NDArray
from shared_libs_python.vector_mgmt.core.types import (
    IndexConfig,
    IndexStats,
    Metadata,
    VectorEmbedding,
)


class FaissVectorIndex:
    """Async ``VectorIndex`` over a FAISS ``IndexFlatIP`` with tombstone deletes."""

    def __init__(self, index_name: str, config: IndexConfig | None = None) -> None:
        self.index_name = index_name
        self.config = config or IndexConfig()
        self._faiss = faiss.IndexFlatIP(self.config.dimension)
        self._faiss_ids: list[str] = []
        self._live: dict[str, NDArray[np.float32]] = {}
        self._meta: dict[str, Metadata] = {}
        self._tombstoned: set[str] = set()

    async def insert(self, embeddings: list[VectorEmbedding]) -> None:
        await asyncio.to_thread(self._insert_sync, embeddings)

    async def search(
        self,
        query_vector: list[float],
        k: int,
        filters: Metadata | None = None,
        ef_search: int | None = None,  # accepted for Protocol parity; flat index has no such knob
    ) -> list[tuple[str, float]]:
        return await asyncio.to_thread(self._search_sync, query_vector, k, filters)

    async def delete(self, entity_ids: list[str]) -> None:
        for entity_id in entity_ids:
            if entity_id in self._live:
                del self._live[entity_id]
                del self._meta[entity_id]
                self._tombstoned.add(entity_id)

    async def get_stats(self) -> IndexStats:
        live = len(self._live)
        total = live + len(self._tombstoned)
        return IndexStats(
            index_name=self.index_name,
            vector_count=live,
            index_size_mb=live * self.config.dimension * 4 / (1024 * 1024),
            tombstone_count=len(self._tombstoned),
            tombstone_percentage=(len(self._tombstoned) / total * 100.0) if total else 0.0,
        )

    async def rebuild(self, config: IndexConfig | None = None) -> None:
        await asyncio.to_thread(self._rebuild_sync, config)

    # -- sync internals (run inside asyncio.to_thread) -----------------------------

    def _insert_sync(self, embeddings: list[VectorEmbedding]) -> None:
        for embedding in embeddings:
            self._add_one(embedding)

    def _add_one(self, embedding: VectorEmbedding) -> None:
        self._validate_new(embedding)
        vector = _as_vector(embedding.embedding)
        self._faiss.add(vector.reshape(1, -1))
        self._faiss_ids.append(embedding.entity_id)
        self._live[embedding.entity_id] = vector
        self._meta[embedding.entity_id] = embedding.metadata
        self._tombstoned.discard(embedding.entity_id)

    def _validate_new(self, embedding: VectorEmbedding) -> None:
        if embedding.entity_id in self._live:
            raise ValueError(f"entity {embedding.entity_id!r} already present; delete to replace")
        if len(embedding.embedding) != self.config.dimension:
            raise ValueError(
                f"embedding dimension {len(embedding.embedding)} != index {self.config.dimension}"
            )

    def _search_sync(
        self, query_vector: list[float], k: int, filters: Metadata | None
    ) -> list[tuple[str, float]]:
        if self._faiss.ntotal == 0 or k <= 0:
            return []
        query = _as_vector(query_vector).reshape(1, -1)
        fetch = min(self._faiss.ntotal, k + len(self._tombstoned))
        scores, indices = self._faiss.search(query, fetch)
        return self._collect(scores[0], indices[0], k, filters)

    def _collect(
        self,
        scores: NDArray[np.float32],
        indices: NDArray[np.int64],
        k: int,
        filters: Metadata | None,
    ) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        for score, idx in zip(scores, indices, strict=True):
            entity_id = self._candidate(int(idx), filters)
            if entity_id is not None:
                # IndexFlatIP yields inner product (≈ cosine for normalized vectors);
                # the shared-libs contract is distance (lower = nearer), so 1 - similarity.
                results.append((entity_id, 1.0 - float(score)))
            if len(results) >= k:
                break
        return results

    def _candidate(self, idx: int, filters: Metadata | None) -> str | None:
        if idx < 0:
            return None  # pragma: no cover - fetch is capped at ntotal, so FAISS never pads with -1
        entity_id = self._faiss_ids[idx]
        if entity_id not in self._live or not _passes(self._meta[entity_id], filters):
            return None
        return entity_id

    def _rebuild_sync(self, config: IndexConfig | None) -> None:
        if config is not None:
            self.config = config.model_copy(update={"dimension": self.config.dimension})
        survivors = list(self._live.items())
        self._faiss = faiss.IndexFlatIP(self.config.dimension)
        self._faiss_ids = [entity_id for entity_id, _ in survivors]
        if survivors:
            self._faiss.add(np.vstack([vector for _, vector in survivors]))
        self._tombstoned.clear()


def _as_vector(values: list[float]) -> NDArray[np.float32]:
    return np.asarray(values, dtype=np.float32)


def _passes(meta: Metadata, filters: Metadata | None) -> bool:
    if not filters:
        return True
    return all(meta.get(key) == value for key, value in filters.items())
