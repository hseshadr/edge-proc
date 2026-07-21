"""FaissVectorIndex is a concrete, async implementation of shared-libs' VectorIndex Protocol.

Vectors are dim-4 and axis-aligned so inner-product ranking is hand-verifiable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from edgeproc_core.vector_mgmt.core.types import (
    IndexConfig,
    VectorEmbedding,
    VectorIndex,
)

from edgeproc.localvec.faiss_index import FaissVectorIndex


def _index() -> FaissVectorIndex:
    return FaissVectorIndex("products", IndexConfig(dimension=4))


def _emb(entity_id: str, vector: list[float], **meta: str) -> VectorEmbedding:
    return VectorEmbedding(entity_id=entity_id, embedding=vector, metadata=dict(meta))


def test_satisfies_the_shared_libs_protocol() -> None:
    assert isinstance(_index(), VectorIndex)


async def test_search_returns_nearest_first_as_cosine_distance() -> None:
    # Contract (shared-libs): results are (entity_id, distance), lower = nearer.
    idx = _index()
    await idx.insert(
        [
            _emb("a", [1.0, 0.0, 0.0, 0.0]),
            _emb("b", [0.0, 1.0, 0.0, 0.0]),
            _emb("c", [0.9, 0.1, 0.0, 0.0]),
        ]
    )
    results = await idx.search([1.0, 0.0, 0.0, 0.0], k=3)
    assert [doc for doc, _ in results] == ["a", "c", "b"]
    distances = [dist for _, dist in results]
    assert distances[0] == pytest.approx(0.0, abs=1e-6)  # identical vector → zero distance
    assert distances == sorted(distances)  # ascending: nearer first


async def test_search_on_empty_index_returns_empty() -> None:
    assert await _index().search([1.0, 0.0, 0.0, 0.0], k=5) == []


async def test_search_respects_k() -> None:
    idx = _index()
    await idx.insert([_emb(str(n), [1.0, 0.0, 0.0, 0.0]) for n in range(5)])
    assert len(await idx.search([1.0, 0.0, 0.0, 0.0], k=2)) == 2


async def test_delete_tombstones_so_search_skips_it() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])
    results = await idx.search([1.0, 0.0, 0.0, 0.0], k=5)
    assert "a" not in {doc for doc, _ in results}


async def test_reinsert_after_delete_purges_stale_row() -> None:
    # Regression: FlatIP has no per-row delete, so deleting an id then re-inserting the
    # SAME id with a NEW vector leaves the old physical row in the FAISS index. If the code
    # keys liveness on id alone, the resurrected id un-filters that stale row — search then
    # returns the entity TWICE (duplicate) or scored by the deleted vector. The index must
    # filter the superseded row itself.
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])
    # Re-insert "a" pointing the OPPOSITE way (now orthogonal to its original vector).
    await idx.insert([_emb("a", [0.0, 1.0, 0.0, 0.0])])

    results = await idx.search([1.0, 0.0, 0.0, 0.0], k=5)
    ids = [doc for doc, _ in results]
    assert ids.count("a") == 1  # the stale row must never surface a duplicate
    # Distance must reflect the CURRENT (orthogonal) vector ≈ 1.0, not the deleted
    # identical-vector row that would score ≈ 0.0.
    assert dict(results)["a"] == pytest.approx(1.0, abs=1e-6)


async def test_reinsert_after_delete_counts_stale_row_as_tombstone() -> None:
    # The superseded physical row is dead weight a rebuild must compact, so get_stats must
    # count it — otherwise the tombstone ratio under-reports bloat and rebuilds fire late.
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    await idx.delete(["a"])
    await idx.insert([_emb("a", [0.0, 1.0, 0.0, 0.0])])  # 2 physical rows, 1 live
    stats = await idx.get_stats()
    assert stats.vector_count == 1
    assert stats.tombstone_count == 1  # the orphaned old row
    await idx.rebuild()
    assert (await idx.get_stats()).tombstone_count == 0  # compacted away


async def test_get_stats_reports_live_and_tombstone_counts() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])
    stats = await idx.get_stats()
    assert stats.vector_count == 1
    assert stats.tombstone_count == 1
    assert stats.tombstone_percentage == pytest.approx(50.0)


async def test_rebuild_compacts_tombstones() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])
    await idx.rebuild()
    stats = await idx.get_stats()
    assert stats.tombstone_count == 0
    assert stats.vector_count == 1
    assert {doc for doc, _ in await idx.search([0.0, 1.0, 0.0, 0.0], k=5)} == {"b"}


async def test_rebuild_with_config_keeps_dimension_but_updates_other_knobs() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    await idx.rebuild(IndexConfig(dimension=999, ef_search=42))
    assert idx.config.dimension == 4  # dimension is pinned to the stored vectors
    assert idx.config.ef_search == 42  # other knobs are adopted
    assert {doc for doc, _ in await idx.search([1.0, 0.0, 0.0, 0.0], k=1)} == {"a"}


async def test_insert_duplicate_id_fails_closed() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    with pytest.raises(ValueError, match="already present"):
        await idx.insert([_emb("a", [0.0, 1.0, 0.0, 0.0])])


async def test_insert_wrong_dimension_fails_closed() -> None:
    idx = _index()
    with pytest.raises(ValueError, match="dimension"):
        await idx.insert([_emb("a", [1.0, 0.0])])


async def test_metadata_filter_restricts_results() -> None:
    idx = _index()
    await idx.insert(
        [
            _emb("a", [1.0, 0.0, 0.0, 0.0], brand="acme"),
            _emb("b", [0.9, 0.1, 0.0, 0.0], brand="other"),
        ]
    )
    results = await idx.search([1.0, 0.0, 0.0, 0.0], k=5, filters={"brand": "acme"})
    assert {doc for doc, _ in results} == {"a"}


async def test_save_and_load_round_trips_search(tmp_path: Path) -> None:
    idx = _index()
    await idx.insert(
        [
            _emb("a", [1.0, 0.0, 0.0, 0.0]),
            _emb("b", [0.0, 1.0, 0.0, 0.0]),
            _emb("c", [0.9, 0.1, 0.0, 0.0]),
        ]
    )
    idx.save(tmp_path / "vec")
    loaded = FaissVectorIndex.load("products", tmp_path / "vec")
    assert loaded.config.dimension == 4
    assert [doc for doc, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=3)] == ["a", "c", "b"]


async def test_load_preserves_tombstones(tmp_path: Path) -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])
    idx.save(tmp_path / "vec")
    loaded = FaissVectorIndex.load("products", tmp_path / "vec")
    stats = await loaded.get_stats()
    assert stats.vector_count == 1
    assert stats.tombstone_count == 1
    assert "a" not in {doc for doc, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=5)}


async def test_load_preserves_metadata_for_filtering(tmp_path: Path) -> None:
    idx = _index()
    await idx.insert(
        [
            _emb("a", [1.0, 0.0, 0.0, 0.0], brand="acme"),
            _emb("b", [0.9, 0.1, 0.0, 0.0], brand="other"),
        ]
    )
    idx.save(tmp_path / "vec")
    loaded = FaissVectorIndex.load("products", tmp_path / "vec")
    results = await loaded.search([1.0, 0.0, 0.0, 0.0], k=5, filters={"brand": "acme"})
    assert {doc for doc, _ in results} == {"a"}


async def test_loaded_index_supports_further_rebuild(tmp_path: Path) -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])
    idx.save(tmp_path / "vec")
    loaded = FaissVectorIndex.load("products", tmp_path / "vec")
    await loaded.rebuild()
    assert (await loaded.get_stats()).tombstone_count == 0
