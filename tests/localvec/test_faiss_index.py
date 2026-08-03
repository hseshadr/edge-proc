"""FaissVectorIndex is a concrete, async implementation of shared-libs' VectorIndex Protocol.

Vectors are dim-4 and axis-aligned so inner-product ranking is hand-verifiable.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event

import pytest
from edgeproc_core.vector_mgmt.core.types import (
    IndexConfig,
    VectorEmbedding,
    VectorIndex,
)

from edgeproc import errors
from edgeproc.localvec.faiss_index import FaissVectorIndex, UnsupportedIndexOptionError


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


async def test_delete_ignores_ids_that_are_not_live() -> None:
    """Deleting an unknown or already-deleted id is a no-op, never a KeyError.

    Surfaced by branch coverage: the `if entity_id in self._live` guard had only ever
    been driven down its TRUE edge, so nothing proved a caller can safely re-issue a
    delete (a retried sync does exactly that).
    """
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a"])

    await idx.delete(["a", "never-inserted"])  # must not raise

    stats = await idx.get_stats()
    assert stats.vector_count == 1  # "b" only; the repeat delete changed nothing
    assert stats.tombstone_count == 1  # and did not double-count "a"


async def test_rebuild_of_a_fully_deleted_index_leaves_it_empty_and_searchable() -> None:
    """Compacting when EVERY entry is tombstoned must not blow up on an empty survivor set.

    Surfaced by branch coverage: `_reindex_survivors` only ever ran with a non-empty
    survivor list, so `np.vstack([])` — which raises on an empty sequence — was one
    fully-drained index away from firing in production.
    """
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0]), _emb("b", [0.0, 1.0, 0.0, 0.0])])
    await idx.delete(["a", "b"])

    await idx.rebuild()

    stats = await idx.get_stats()
    assert stats.vector_count == 0
    assert stats.tombstone_count == 0
    assert await idx.search([1.0, 0.0, 0.0, 0.0], k=5) == []  # empty, not a crash


async def test_rebuild_pins_dimension_to_the_stored_vectors() -> None:
    """INVERTED from ``test_rebuild_with_config_keeps_dimension_but_updates_other_knobs``.

    That test asserted ``idx.config.ef_search == 42`` — that the knob was STORED — and
    called it "other knobs are adopted". Nothing adopted it. ``ef_search`` was written to
    ``self.config`` and then dropped: the index is a brute-force ``IndexFlatIP`` with no
    graph to tune, and no code path ever read the value back. The assertion would have
    kept passing with the entire configuration path deleted, so it measured shape, not
    property — a passing test that asserted the defect as the requirement.

    What survives here is the half that was always real: a rebuild cannot re-dimension
    vectors already in the index. The knob's actual behaviour is asserted below, as a
    refusal — the honest answer to a request this backend cannot serve.
    """
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    await idx.rebuild(IndexConfig(dimension=999))
    assert idx.config.dimension == 4  # pinned to the stored vectors, not the request
    assert {doc for doc, _ in await idx.search([1.0, 0.0, 0.0, 0.0], k=1)} == {"a"}


async def test_rebuild_serializes_insert_and_search_on_real_faiss() -> None:
    """Serialization is MUTUAL EXCLUSION, not a queue order — assert only what is promised.

    ``docs/OPERATIONS.md`` promises that insert/rebuild/search are serialized per instance.
    It promises nothing about which of two concurrently-issued operations wins the lock,
    and it cannot: the guard is a ``threading.RLock``, which has no fairness guarantee, so
    the queued search may be granted before or after the queued insert. Asserting one of
    those orders made this test fail 8 of 12 local runs while passing on CI's scheduler.

    What IS guaranteed, and is what this now asserts: neither operation proceeds while the
    rebuild holds the lock; the queued search observes a coherent index rather than a torn
    one; and once every queued operation has drained, no write was lost.
    """
    idx = _index()
    await idx.insert([_emb("base", [1.0, 0.0, 0.0, 0.0])])
    rebuild_entered = Event()
    release_rebuild = Event()
    original_reindex = idx._reindex_survivors

    def paused_reindex(survivors: list[tuple[str, object]]) -> None:
        rebuild_entered.set()
        assert release_rebuild.wait(timeout=5)
        original_reindex(survivors)  # type: ignore[arg-type]

    idx._reindex_survivors = paused_reindex  # type: ignore[method-assign]
    rebuild_task = asyncio.create_task(idx.rebuild())
    assert await asyncio.to_thread(rebuild_entered.wait, 5)
    insert_task = asyncio.create_task(idx.insert([_emb("new", [0.0, 1.0, 0.0, 0.0])]))
    search_task = asyncio.create_task(idx.search([1.0, 0.0, 0.0, 0.0], k=5))
    await asyncio.sleep(0.05)
    assert not insert_task.done()  # the rebuild holds the lock, so nothing else proceeds
    assert not search_task.done()
    release_rebuild.set()
    await asyncio.gather(rebuild_task, insert_task)

    queued = {doc for doc, _ in await search_task}
    assert queued in ({"base"}, {"base", "new"})  # coherent on either side of the insert
    drained = {doc for doc, _ in await idx.search([1.0, 0.0, 0.0, 0.0], k=5)}
    assert drained == {"base", "new"}  # the rebuild dropped nothing and lost no write
    assert (await idx.get_stats()).tombstone_count == 0


async def test_save_serializes_with_rebuild_and_load_rejects_crash_mismatch(
    tmp_path: Path,
) -> None:
    idx = _index()
    await idx.insert([_emb("base", [1.0, 0.0, 0.0, 0.0])])
    rebuild_entered = Event()
    release_rebuild = Event()
    original_reindex = idx._reindex_survivors

    def paused_reindex(survivors: list[tuple[str, object]]) -> None:
        rebuild_entered.set()
        assert release_rebuild.wait(timeout=5)
        original_reindex(survivors)  # type: ignore[arg-type]

    idx._reindex_survivors = paused_reindex  # type: ignore[method-assign]
    rebuild_task = asyncio.create_task(idx.rebuild())
    assert await asyncio.to_thread(rebuild_entered.wait, 5)
    save_task = asyncio.create_task(asyncio.to_thread(idx.save, tmp_path / "vec"))
    await asyncio.sleep(0.05)
    assert not save_task.done()
    release_rebuild.set()
    await asyncio.gather(rebuild_task, save_task)
    loaded = FaissVectorIndex.load("products", tmp_path / "vec")
    assert (await loaded.get_stats()).vector_count == 1

    state_path = tmp_path / "vec" / "state.json"
    state = json.loads(state_path.read_text())
    state["faiss_ids"] = []
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="row count"):
        FaissVectorIndex.load("products", tmp_path / "vec")


async def test_load_rejects_a_dimension_mismatch_against_the_state_sidecar(
    tmp_path: Path,
) -> None:
    # The saved FAISS binary is built for dimension 4. A sidecar hand-edited (or torn
    # by a crash) to claim a different dimension must not reopen: every vector op
    # after would be shaped wrong with nothing to say so.
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    idx.save(tmp_path / "vec")

    state_path = tmp_path / "vec" / "state.json"
    state = json.loads(state_path.read_text())
    state["config"]["dimension"] = 8
    state_path.write_text(json.dumps(state))

    with pytest.raises(ValueError, match="dimension does not match"):
        FaissVectorIndex.load("products", tmp_path / "vec")


async def test_load_rejects_metadata_ids_that_do_not_match_live_state(
    tmp_path: Path,
) -> None:
    # The metadata map must exactly cover the live (non-tombstoned) ids. A sidecar
    # carrying a metadata entry for an id nothing else knows about is torn — reopening
    # it anyway would let a filtered search silently reference a ghost entity.
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    idx.save(tmp_path / "vec")

    state_path = tmp_path / "vec" / "state.json"
    state = json.loads(state_path.read_text())
    state["meta"]["ghost"] = {}
    state_path.write_text(json.dumps(state))

    with pytest.raises(ValueError, match="metadata IDs"):
        FaissVectorIndex.load("products", tmp_path / "vec")


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


# -- an option this backend cannot honour is REFUSED, never silently dropped ------------
#
# ``IndexConfig`` is shared-libs' type and is deliberately wide: its own docstring says a
# backend "is free to honour or ignore" the HNSW knobs. Ignoring them silently is what
# these tests forbid HERE. A caller who sets ``distance_metric="l2"`` and gets inner
# product back has no signal that they were overruled — they get confidently wrong
# numbers. Every assertion below drives the refusal, not the stored attribute.


async def test_construction_accepts_the_metric_this_backend_implements() -> None:
    """Guard against over-refusing: the one honoured metric must still build and search."""
    idx = FaissVectorIndex("products", IndexConfig(dimension=4, distance_metric="cosine"))
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    results = await idx.search([1.0, 0.0, 0.0, 0.0], k=1)
    assert [doc for doc, _ in results] == ["a"]
    assert results[0][1] == pytest.approx(0.0, abs=1e-6)  # cosine distance, as advertised


@pytest.mark.parametrize("metric", ["l2", "inner_product"])
async def test_construction_refuses_a_distance_metric_it_does_not_implement(
    metric: str,
) -> None:
    with pytest.raises(UnsupportedIndexOptionError, match="distance_metric"):
        FaissVectorIndex("products", IndexConfig(dimension=4, distance_metric=metric))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("knob", "value"), [("m", 64), ("ef_construction", 400), ("ef_search", 42)]
)
async def test_construction_refuses_a_tuned_graph_knob(knob: str, value: int) -> None:
    """A changed HNSW knob is a request this flat index cannot serve, so it refuses."""
    with pytest.raises(UnsupportedIndexOptionError, match=knob):
        FaissVectorIndex("products", IndexConfig(dimension=4, **{knob: value}))


async def test_construction_accepts_graph_knobs_left_at_their_defaults() -> None:
    """A default value asks for nothing, so passing it explicitly must not refuse."""
    defaults = IndexConfig()
    idx = FaissVectorIndex(
        "products",
        IndexConfig(
            dimension=4,
            m=defaults.m,
            ef_construction=defaults.ef_construction,
            ef_search=defaults.ef_search,
        ),
    )
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    assert [doc for doc, _ in await idx.search([1.0, 0.0, 0.0, 0.0], k=1)] == ["a"]


async def test_rebuild_refuses_a_tuned_graph_knob_and_changes_nothing() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    with pytest.raises(UnsupportedIndexOptionError, match="ef_search"):
        await idx.rebuild(IndexConfig(dimension=4, ef_search=42))
    assert idx.config.ef_search == IndexConfig().ef_search  # the refusal adopted nothing
    assert [doc for doc, _ in await idx.search([1.0, 0.0, 0.0, 0.0], k=1)] == ["a"]


async def test_rebuild_refuses_a_distance_metric_it_does_not_implement() -> None:
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    with pytest.raises(UnsupportedIndexOptionError, match="distance_metric"):
        await idx.rebuild(IndexConfig(dimension=4, distance_metric="l2"))
    assert idx.config.distance_metric == "cosine"  # still the metric actually computed


async def test_search_refuses_a_per_query_ef_search() -> None:
    """The Protocol makes ``ef_search`` a search argument; this backend cannot honour it.

    It was previously accepted and dropped on the floor — the parameter was not even
    forwarded to ``_search_sync``. A caller widening the beam for a hard query got the
    identical result set and no indication their tuning did nothing.
    """
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    with pytest.raises(UnsupportedIndexOptionError, match="ef_search"):
        await idx.search([1.0, 0.0, 0.0, 0.0], k=1, ef_search=64)


async def test_load_refuses_a_persisted_config_it_cannot_honour(tmp_path: Path) -> None:
    """Fail-closed on the persistence path too: a sidecar claiming ``l2`` must not reopen.

    Silently reopening it as inner product is exactly the confidently-wrong-result
    failure this change exists to remove — and a saved index outlives the process that
    wrote it, so the refusal has to live at load, not only at construction.
    """
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    idx.save(tmp_path / "vec")

    state_path = tmp_path / "vec" / "state.json"
    state = json.loads(state_path.read_text())
    state["config"]["distance_metric"] = "l2"
    state_path.write_text(json.dumps(state))

    with pytest.raises(UnsupportedIndexOptionError, match="distance_metric"):
        FaissVectorIndex.load("products", tmp_path / "vec")


def test_refusal_carries_the_canonical_config_invalid_code() -> None:
    """The refusal is coded, not just typed — it renders to RFC 9457 like every other."""
    error = UnsupportedIndexOptionError("ef_search tunes a graph this index does not have")
    assert errors.code_of(error) == errors.CONFIG_INVALID
    problem = errors.problem_details_for(error, {"field": "ef_search"})
    assert problem.type == errors.CONFIG_INVALID
    assert problem.detail == "ef_search tunes a graph this index does not have"


def test_refusal_is_still_a_value_error() -> None:
    """Metadata only: every existing ``except ValueError`` around index config still fires.

    The module's other fail-closed refusals (duplicate id, wrong dimension, torn sidecar)
    are ``ValueError``s, so a caller guarding index construction already catches this one.
    """
    assert issubclass(UnsupportedIndexOptionError, ValueError)
