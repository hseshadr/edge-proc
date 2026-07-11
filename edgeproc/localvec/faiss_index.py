"""FAISS-backed vector index that implements shared-libs' ``VectorIndex`` Protocol.

Bridges a synchronous, build-once FAISS index into ``shared_libs_python.vector_mgmt``'s
async, lifecycle-managed contract:

- CPU-bound FAISS calls run in ``asyncio.to_thread`` so the event loop never blocks.
- ``delete`` tombstones by id (FlatIP has no native delete); ``search`` over-fetches
  and filters tombstoned rows; ``rebuild`` physically compacts them away.
- ``get_stats`` reports the tombstone ratio that drives ``IndexManager`` rebuilds.

Once constructed it drops straight into shared-libs' ``IndexManager`` and partition
strategies.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final

import faiss
import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel
from shared_libs_python.vector_mgmt.core.types import (
    IndexConfig,
    IndexStats,
    Metadata,
    Scalar,
    VectorEmbedding,
)

# FROZEN on-disk contract: a saved index dir is addressed by these exact filenames, so
# load() can find a save()d index across versions. Renaming either breaks existing dirs.
_INDEX_FILE: Final[str] = "index.faiss"
_STATE_FILE: Final[str] = "state.json"


class _PersistedState(BaseModel):
    """On-disk sidecar for a saved index (the FAISS vectors live in ``index.faiss``)."""

    config: IndexConfig
    faiss_ids: list[str]
    tombstoned: list[str]
    meta: dict[str, dict[str, Scalar]]


class FaissVectorIndex:
    """Async ``VectorIndex`` over a FAISS ``IndexFlatIP`` with tombstone deletes."""

    def __init__(self, index_name: str, config: IndexConfig | None = None) -> None:
        self.index_name = index_name
        self.config = config or IndexConfig()
        self._faiss: faiss.Index = faiss.IndexFlatIP(self.config.dimension)
        self._faiss_ids: list[str] = []
        self._live: dict[str, NDArray[np.float32]] = {}
        self._row_of: dict[str, int] = {}  # entity_id -> its CURRENT authoritative FAISS row
        self._meta: dict[str, Metadata] = {}
        self._tombstoned: set[str] = set()

    def save(self, directory: Path) -> None:
        """Persist the FAISS index plus its id map, tombstones, and metadata."""
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._faiss, str(directory / _INDEX_FILE))
        state = _PersistedState(
            config=self.config,
            faiss_ids=self._faiss_ids,
            tombstoned=sorted(self._tombstoned),
            meta={entity_id: dict(meta) for entity_id, meta in self._meta.items()},
        )
        (directory / _STATE_FILE).write_text(state.model_dump_json())

    @classmethod
    def load(cls, index_name: str, directory: Path) -> FaissVectorIndex:
        """Reload an index previously written by :meth:`save`."""
        state = _PersistedState.model_validate_json((directory / _STATE_FILE).read_text())
        instance = cls(index_name, state.config)
        instance._restore(faiss.read_index(str(directory / _INDEX_FILE)), state)
        return instance

    def _restore(self, faiss_index: faiss.Index, state: _PersistedState) -> None:
        self._faiss = faiss_index
        self._faiss_ids = list(state.faiss_ids)
        self._tombstoned = set(state.tombstoned)
        self._meta = {entity_id: meta for entity_id, meta in state.meta.items()}
        self._live, self._row_of = self._reconstruct_live_rows()

    def _reconstruct_live_rows(self) -> tuple[dict[str, NDArray[np.float32]], dict[str, int]]:
        """Rebuild the live-vector and authoritative-row maps from the persisted rows.

        A duplicate id left by a delete+re-insert keeps its LAST physical row (later
        re-inserts win); the earlier row is orphaned and stays filtered out on search.
        """
        live: dict[str, NDArray[np.float32]] = {}
        row_of: dict[str, int] = {}
        for row, entity_id in enumerate(self._faiss_ids):
            if entity_id not in self._tombstoned:
                live[entity_id] = np.asarray(self._faiss.reconstruct(row), dtype=np.float32)
                row_of[entity_id] = row
        return live, row_of

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
                del self._row_of[entity_id]
                self._tombstoned.add(entity_id)

    async def get_stats(self) -> IndexStats:
        live = len(self._live)
        # Count EVERY dead physical row — deleted ids AND rows superseded by a re-insert —
        # so the tombstone ratio that triggers rebuilds reflects the index's real bloat.
        dead = int(self._faiss.ntotal) - live
        total = live + dead
        return IndexStats(
            index_name=self.index_name,
            vector_count=live,
            index_size_mb=live * self.config.dimension * 4 / (1024 * 1024),
            tombstone_count=dead,
            tombstone_percentage=(dead / total * 100.0) if total else 0.0,
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
        # This new row is now the entity's authoritative row; any prior row for the same
        # id (from a delete+re-insert) is left orphaned/dead — filtered on read below.
        self._row_of[embedding.entity_id] = len(self._faiss_ids) - 1
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
        # Over-fetch past every dead physical row (deleted + superseded) so we can still
        # surface k live hits after filtering; underestimating this drops real results.
        dead_rows = int(self._faiss.ntotal) - len(self._live)
        fetch = min(self._faiss.ntotal, k + dead_rows)
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
        # Accept a row ONLY if it is the entity's CURRENT authoritative row. A deleted id
        # is absent from ``_row_of``; a stale row left by a delete+re-insert maps to a
        # later row — either way this superseded/dead row is refused, never duplicated.
        if self._row_of.get(entity_id) != idx:
            return None
        if not _passes(self._meta[entity_id], filters):
            return None
        return entity_id

    def _rebuild_sync(self, config: IndexConfig | None) -> None:
        if config is not None:
            self.config = config.model_copy(update={"dimension": self.config.dimension})
        survivors = list(self._live.items())
        self._faiss = faiss.IndexFlatIP(self.config.dimension)
        self._reindex_survivors(survivors)
        self._tombstoned.clear()

    def _reindex_survivors(self, survivors: list[tuple[str, NDArray[np.float32]]]) -> None:
        """Repopulate the FAISS index + id/row maps from the compacted survivor set."""
        self._faiss_ids = [entity_id for entity_id, _ in survivors]
        self._row_of = {entity_id: row for row, entity_id in enumerate(self._faiss_ids)}
        if survivors:
            self._faiss.add(np.vstack([vector for _, vector in survivors]))


def _as_vector(values: list[float]) -> NDArray[np.float32]:
    return np.asarray(values, dtype=np.float32)


def _passes(meta: Metadata, filters: Metadata | None) -> bool:
    if not filters:
        return True
    return all(meta.get(key) == value for key, value in filters.items())
