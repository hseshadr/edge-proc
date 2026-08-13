"""FAISS-backed vector index that implements shared-libs' ``VectorIndex`` Protocol.

Bridges a synchronous, build-once FAISS index into ``edgeproc_core.vector_mgmt``'s
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
import errno
from pathlib import Path
from threading import RLock
from typing import ClassVar, Final

import faiss
import numpy as np
from edgeproc_core.vector_mgmt.core.types import (
    DistanceMetric,
    IndexConfig,
    IndexStats,
    Metadata,
    Scalar,
    VectorEmbedding,
)
from filelock import BaseFileLock, FileLock
from numpy.typing import NDArray
from pydantic import BaseModel

from edgeproc.core.settings import EdgeProcSettings
from edgeproc.errors import CONFIG_INVALID
from edgeproc.localvec.snapshot_store import (
    NoSnapshotError,
    SnapshotPayload,
    SnapshotRevision,
    SnapshotStore,
    open_regular_leaf,
    read_regular_bytes,
)
from edgeproc.localvec.snapshot_store import SnapshotConflictError as _SnapshotConflictError

SnapshotConflictError = _SnapshotConflictError

# Legacy 0.4.0 on-disk contract. New saves use generation-addressed snapshots; these names
# remain reserved so load() can migrate an existing directory without data loss.
_INDEX_FILE: Final[str] = "index.faiss"
_STATE_FILE: Final[str] = "state.json"
_READ_ONLY_LOAD_ATTEMPTS: Final[int] = 3

#: The one metric this backend computes. ``IndexFlatIP`` scores inner product, which over
#: the unit-normalized vectors ``LocalEncoder`` emits IS cosine similarity; ``_collect``
#: returns ``1 - similarity``, i.e. cosine DISTANCE, per the shared-libs contract. Naming
#: any other metric — including ``inner_product``, whose caller would read that returned
#: number as a similarity and be wrong by exactly that transform — is refused.
_SUPPORTED_METRIC: Final[DistanceMetric] = "cosine"

#: Why the HNSW knobs cannot be honoured, said once so every refusal says it identically.
_NO_GRAPH: Final[str] = (
    "this backend builds a brute-force faiss.IndexFlatIP, which has no graph to tune"
)


class UnsupportedIndexOptionError(ValueError):
    """A caller asked for an index option this FAISS backend cannot implement (fail-closed).

    Raised instead of accepting the option and quietly doing something else. Silently
    ignoring ``distance_metric="l2"`` is worse than rejecting it: the caller believes they
    configured Euclidean distance, gets inner product, and has no signal at all.

    Subclasses :class:`ValueError` — the module's other refusals (duplicate id, wrong
    dimension, torn sidecar) already are one, so a caller guarding index construction
    catches this unchanged. Carries the canonical ``config.invalid`` code so a consumer
    can render it via :func:`edgeproc.errors.problem_details_for`.
    """

    code: ClassVar[str] = CONFIG_INVALID


class _PersistedState(BaseModel):
    """On-disk sidecar for a saved index (the FAISS vectors live in ``index.faiss``)."""

    config: IndexConfig
    faiss_ids: list[str]
    tombstoned: list[str]
    meta: dict[str, dict[str, Scalar]]


class FaissVectorIndex:
    """Async ``VectorIndex`` over a FAISS ``IndexFlatIP`` with tombstone deletes.

    Honours exactly two of ``IndexConfig``'s knobs — ``dimension`` and a
    ``distance_metric`` of ``"cosine"`` — and REFUSES the rest rather than accepting
    them and doing something else. See :class:`UnsupportedIndexOptionError`.
    """

    def __init__(self, index_name: str, config: IndexConfig | None = None) -> None:
        self.index_name = index_name
        self.config = _honoured(config or IndexConfig())
        self._faiss: faiss.Index = faiss.IndexFlatIP(self.config.dimension)
        self._faiss_ids: list[str] = []
        self._live: dict[str, NDArray[np.float32]] = {}
        self._row_of: dict[str, int] = {}  # entity_id -> its CURRENT authoritative FAISS row
        self._meta: dict[str, Metadata] = {}
        self._tombstoned: set[str] = set()
        self._lock = RLock()
        self._snapshot_revision: SnapshotRevision | None = None

    def save(self, directory: Path) -> None:
        """Persist the FAISS index plus its id map, tombstones, and metadata."""
        SnapshotStore.prepare_root(directory)
        with self._lock, _persistence_lock(directory):
            state = self._persisted_state()
            store = SnapshotStore(directory)
            self._snapshot_revision = store.commit(
                lambda path: faiss.write_index(self._faiss, str(path)),
                state.model_dump_json().encode("utf-8"),
                self._snapshot_revision,
            )

    def _persisted_state(self) -> _PersistedState:
        return _PersistedState(
            config=self.config,
            faiss_ids=self._faiss_ids,
            tombstoned=sorted(self._tombstoned),
            meta={entity_id: dict(meta) for entity_id, meta in self._meta.items()},
        )

    @classmethod
    def load(cls, index_name: str, directory: Path) -> FaissVectorIndex:
        """Reload an index previously written by :meth:`save`."""
        lock = _persistence_lock(directory)
        acquired = _acquire_load_lock(lock)
        try:
            return _restore_persisted(index_name, directory, migrate_legacy=acquired)
        finally:
            if acquired:
                lock.release()

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
        ef_search: int | None = None,  # present for Protocol parity; refused, never dropped
    ) -> list[tuple[str, float]]:
        if ef_search is not None:
            raise UnsupportedIndexOptionError(f"cannot honour ef_search: {_NO_GRAPH}")
        return await asyncio.to_thread(self._search_sync, query_vector, k, filters)

    async def delete(self, entity_ids: list[str], filters: Metadata | None = None) -> None:
        with self._lock:
            for entity_id in entity_ids:
                if entity_id in self._live and _passes(self._meta[entity_id], filters):
                    del self._live[entity_id]
                    del self._meta[entity_id]
                    del self._row_of[entity_id]
                    self._tombstoned.add(entity_id)

    async def get_stats(self, filters: Metadata | None = None) -> IndexStats:
        with self._lock:
            live = sum(_passes(meta, filters) for meta in self._meta.values())
            # Count EVERY dead physical row — deleted ids AND rows superseded by a re-insert —
            # so the tombstone ratio that triggers rebuilds reflects the index's real bloat.
            dead = 0 if filters else int(self._faiss.ntotal) - live
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            if config is not None:
                # Validate BEFORE adopting: a refused rebuild must leave the index exactly
                # as it was, not half-reconfigured with the offending knob already stored.
                self.config = _honoured(config).model_copy(
                    update={"dimension": self.config.dimension}
                )
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


def _honoured(config: IndexConfig) -> IndexConfig:
    """Return ``config`` if this backend can implement every knob it sets, else refuse.

    ``IndexConfig`` is shared-libs' wide, pass-through type: its own docstring says a
    backend "is free to honour or ignore" the HNSW knobs. Ignoring is what this refuses
    to do — an option that changes nothing must not look like an option that worked.
    """
    if config.distance_metric != _SUPPORTED_METRIC:
        raise UnsupportedIndexOptionError(
            f"distance_metric={config.distance_metric!r} is not implemented here: this "
            f"backend computes {_SUPPORTED_METRIC} distance only"
        )
    tuned = _tuned_graph_knobs(config)
    if tuned:
        raise UnsupportedIndexOptionError(f"cannot honour {', '.join(tuned)}: {_NO_GRAPH}")
    return config


def _tuned_graph_knobs(config: IndexConfig) -> list[str]:
    """The HNSW knobs this caller actually changed — a default value asks for nothing."""
    default = IndexConfig()
    changed: tuple[tuple[str, bool], ...] = (
        ("m", config.m != default.m),
        ("ef_construction", config.ef_construction != default.ef_construction),
        ("ef_search", config.ef_search != default.ef_search),
    )
    return [knob for knob, tuned in changed if tuned]


def _as_vector(values: list[float]) -> NDArray[np.float32]:
    return np.asarray(values, dtype=np.float32)


def _passes(meta: Metadata, filters: Metadata | None) -> bool:
    if not filters:
        return True
    return all(meta.get(key) == value for key, value in filters.items())


def _persistence_lock(directory: Path) -> BaseFileLock:
    timeout = EdgeProcSettings().snapshot_lock_timeout
    return FileLock(directory / ".snapshot.lock", timeout=timeout)


def _acquire_load_lock(lock: BaseFileLock) -> bool:
    """Acquire when writable; immutable mounts safely read committed generations unlocked."""
    try:
        lock.acquire()
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EPERM, errno.EROFS}:
            raise
        return False
    return True


def _restore_persisted(
    index_name: str, directory: Path, *, migrate_legacy: bool
) -> FaissVectorIndex:
    faiss_index, state, revision = _load_persisted(directory, migrate_legacy=migrate_legacy)
    _validate_persisted_index(faiss_index, state)
    instance = FaissVectorIndex(index_name, state.config)
    instance._restore(faiss_index, state)
    instance._snapshot_revision = revision
    return instance


def _load_persisted(
    directory: Path, *, migrate_legacy: bool
) -> tuple[faiss.Index, _PersistedState, SnapshotRevision | None]:
    if migrate_legacy:
        return _load_writable(directory)
    return _load_read_only(directory)


def _load_writable(
    directory: Path,
) -> tuple[faiss.Index, _PersistedState, SnapshotRevision]:
    store = SnapshotStore(directory)
    try:
        payload = store.latest()
    except NoSnapshotError:
        return _migrate_legacy(directory, store)
    index, state = _read_snapshot(payload)
    return index, state, payload.revision


def _load_read_only(
    directory: Path,
) -> tuple[faiss.Index, _PersistedState, SnapshotRevision | None]:
    failure = FileNotFoundError("snapshot changed during read-only load")
    for _attempt in range(_READ_ONLY_LOAD_ATTEMPTS):
        try:
            return _load_read_only_once(directory)
        except FileNotFoundError as exc:
            failure = exc
    raise failure


def _read_legacy_result(
    directory: Path,
) -> tuple[faiss.Index, _PersistedState, None]:
    index, state = _read_legacy(directory)
    return index, state, None


def _load_read_only_once(
    directory: Path,
) -> tuple[faiss.Index, _PersistedState, SnapshotRevision | None]:
    if not (directory / "snapshots").exists():
        return _read_legacy_result(directory)
    store = SnapshotStore(directory)
    try:
        payload = store.latest()
    except NoSnapshotError:
        return _read_legacy_result(directory)
    index, state = _read_snapshot(payload)
    return index, state, payload.revision


def _read_snapshot(payload: SnapshotPayload) -> tuple[faiss.Index, _PersistedState]:
    try:
        state = _PersistedState.model_validate_json(payload.state_bytes)
        # faiss exports this reader at runtime but omits it from its published stub.
        reader = faiss.PyCallbackIOReader(payload.index_file.read)  # type: ignore[attr-defined]
        return faiss.read_index(reader), state
    finally:
        payload.close()


def _read_legacy(directory: Path) -> tuple[faiss.Index, _PersistedState]:
    index_file = open_regular_leaf(directory / _INDEX_FILE, "legacy FAISS index")
    try:
        state_bytes = read_regular_bytes(directory / _STATE_FILE, "legacy state sidecar")
        reader = faiss.PyCallbackIOReader(index_file.read)  # type: ignore[attr-defined]
        return faiss.read_index(reader), _PersistedState.model_validate_json(state_bytes)
    finally:
        index_file.close()


def _migrate_legacy(
    directory: Path, store: SnapshotStore
) -> tuple[faiss.Index, _PersistedState, SnapshotRevision]:
    index, state = _read_legacy(directory)
    _validate_persisted_index(index, state)
    revision = store.commit(
        lambda path: faiss.write_index(index, str(path)), state.model_dump_json().encode()
    )
    (directory / _INDEX_FILE).unlink()
    (directory / _STATE_FILE).unlink()
    return index, state, revision


def _validate_persisted_index(index: faiss.Index, state: _PersistedState) -> None:
    if index.ntotal != len(state.faiss_ids):
        raise ValueError("persisted FAISS row count does not match state sidecar")
    if index.d != state.config.dimension:
        raise ValueError("persisted FAISS dimension does not match state sidecar")
    live_ids = set(state.faiss_ids) - set(state.tombstoned)
    if set(state.meta) != live_ids:
        raise ValueError("persisted metadata IDs do not match live state")
