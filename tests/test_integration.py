"""End-to-end proof: the extracted substrate runs through the facade, and the FAISS
index drops into shared-libs' IndexManager without an adapter in between."""

from __future__ import annotations

from edgeproc_core.vector_mgmt.core.index_manager import IndexManager
from edgeproc_core.vector_mgmt.core.types import (
    IndexConfig,
    VectorEmbedding,
    VectorIndex,
)
from edgeproc_core.vector_mgmt.partitioning.strategies import GlobalPartitionStrategy

from edgeproc import EdgeProc, PrivacyMode, Task, TaskKind
from edgeproc.core.registry import RuntimeRegistry
from edgeproc.localvec.faiss_index import FaissVectorIndex
from edgeproc.localvec.runtime import LocalVecRuntime
from edgeproc.localvec.searcher import KeywordSearcher

from .localvec._fakes import FakeEncoder

_TEXTS = ["red shoes", "blue boots", "green dress"]
_IDS = ["p1", "p2", "p3"]


async def _edgeproc_with_localvec() -> EdgeProc:
    encoder = FakeEncoder()
    index = FaissVectorIndex("products", IndexConfig(dimension=encoder.dim))
    await index.insert(
        [
            VectorEmbedding(entity_id=i, embedding=v.tolist())
            for i, v in zip(_IDS, encoder.encode_texts(_TEXTS), strict=True)
        ]
    )
    runtime = LocalVecRuntime(encoder, index, KeywordSearcher.from_texts(_TEXTS, _IDS))
    registry = RuntimeRegistry()
    registry.register(runtime)
    return EdgeProc(registry=registry)


async def test_facade_routes_a_search_task_end_to_end() -> None:
    ep = await _edgeproc_with_localvec()
    result = await ep.run(
        Task(
            kind=TaskKind.SEARCH,
            payload={"query": "red shoes", "k": 3},
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
    )
    assert result.success is True
    assert result.runtime_used == "localvec"
    assert result.payload["results"][0][0] == "p1"


async def test_facade_fails_closed_on_cloud_privacy() -> None:
    ep = await _edgeproc_with_localvec()
    result = await ep.run(
        Task(kind=TaskKind.SEARCH, payload={"query": "x"}, privacy_mode=PrivacyMode.CLOUD_PREMIUM)
    )
    assert result.success is False
    assert result.error == "no_runtime_accepted"


async def _global_manager() -> tuple[IndexManager, FakeEncoder]:
    encoder = FakeEncoder()

    async def factory(name: str, config: IndexConfig | None = None) -> VectorIndex:
        return FaissVectorIndex(name, config)

    strategy = GlobalPartitionStrategy(
        index_factory=factory, config=IndexConfig(dimension=encoder.dim)
    )
    return IndexManager(strategy), encoder


async def test_faiss_index_drops_into_shared_libs_index_manager() -> None:
    manager, encoder = await _global_manager()
    await manager.insert(
        [
            VectorEmbedding(entity_id=i, embedding=v.tolist())
            for i, v in zip(_IDS, encoder.encode_texts(_TEXTS), strict=True)
        ]
    )
    results = await manager.search(encoder.encode_query("red shoes").tolist(), k=3)
    assert results[0][0] == "p1"


async def test_index_manager_rebuilds_when_tombstones_exceed_threshold() -> None:
    manager, encoder = await _global_manager()
    await manager.insert(
        [
            VectorEmbedding(entity_id=i, embedding=v.tolist())
            for i, v in zip(_IDS, encoder.encode_texts(_TEXTS), strict=True)
        ]
    )
    await manager.delete(["p1"])  # 1 of 3 → 33% tombstones, over the 10% threshold
    assert await manager.rebuild_if_needed() is True
    stats = await manager.get_stats()
    assert stats[0].tombstone_count == 0
