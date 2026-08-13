"""FaissVectorIndex is a concrete, async implementation of shared-libs' VectorIndex Protocol.

Vectors are dim-4 and axis-aligned so inner-product ranking is hand-verifiable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import shutil
import tracemalloc
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from threading import Event, Thread, current_thread

import pytest
from edgeproc_core.vector_mgmt.conformance import assert_vector_index_conformance
from edgeproc_core.vector_mgmt.core.types import (
    IndexConfig,
    VectorEmbedding,
    VectorIndex,
)
from filelock import FileLock, Timeout

from edgeproc import errors
from edgeproc.localvec import faiss_index, snapshot_store
from edgeproc.localvec.faiss_index import (
    FaissVectorIndex,
    SnapshotConflictError,
    UnsupportedIndexOptionError,
)
from edgeproc.localvec.snapshot_store import SnapshotSequenceError, SnapshotStore


def _index() -> FaissVectorIndex:
    return FaissVectorIndex("products", IndexConfig(dimension=4))


def _emb(entity_id: str, vector: list[float], **meta: str) -> VectorEmbedding:
    return VectorEmbedding(entity_id=entity_id, embedding=vector, metadata=dict(meta))


def _committed_files(directory: Path) -> tuple[Path, Path]:
    manifest_path = max((directory / "snapshots").glob("*.snapshot.json"))
    manifest = json.loads(manifest_path.read_text())
    generation = manifest["generation"]
    return (
        directory / "snapshots" / f"{generation}.faiss",
        directory / "snapshots" / f"{generation}.state.json",
    )


def _snapshot_manifests(directory: Path) -> list[Path]:
    return sorted((directory / "snapshots").glob("*.snapshot.json"))


def _write_committed_state(directory: Path, state: dict[str, object]) -> None:
    state_path = _committed_files(directory)[1]
    state_bytes = json.dumps(state).encode()
    state_path.write_bytes(state_bytes)
    manifest_path = _snapshot_manifests(directory)[-1]
    manifest = json.loads(manifest_path.read_text())
    manifest["state_sha256"] = hashlib.sha256(state_bytes).hexdigest()
    manifest_path.write_text(json.dumps(manifest))


def _hold_snapshot_lock(lock_path: str, ready: Connection, release: Connection) -> None:
    with FileLock(lock_path):
        ready.send(True)
        release.recv()


def _commit_loaded_writer(
    directory: str, entity_id: str, ready: Connection, go: Connection
) -> None:
    index = FaissVectorIndex.load("products", Path(directory))
    asyncio.run(index.insert([_emb(entity_id, [0.0, 1.0, 0.0, 0.0])]))
    ready.send(True)
    go.recv()
    try:
        index.save(Path(directory))
    except SnapshotConflictError:
        ready.send("conflict")
    else:
        ready.send("saved")


async def _save_named_index(directory: Path, entity_id: str) -> None:
    idx = _index()
    await idx.insert([_emb(entity_id, [1.0, 0.0, 0.0, 0.0])])
    idx.save(directory)


async def _save_generations(directory: Path, start: int, stop: int) -> None:
    for number in range(start, stop):
        await _save_named_index(directory, f"item-{number}")


async def _write_legacy_pair(root: Path) -> Path:
    source = root / "source"
    legacy = root / "legacy"
    legacy.mkdir()
    await _save_named_index(source, "red")
    index_path, state_path = _committed_files(source)
    shutil.copyfile(index_path, legacy / "index.faiss")
    shutil.copyfile(state_path, legacy / "state.json")
    return legacy


def _load_read_only_tree(directory: Path) -> FaissVectorIndex:
    paths = [directory, *directory.rglob("*")]
    try:
        for path in paths:
            path.chmod(0o555 if path.is_dir() else 0o444)
        return FaissVectorIndex.load("products", directory)
    finally:
        for path in paths:
            path.chmod(0o755 if path.is_dir() else 0o644)


@dataclass(frozen=True)
class _LoadRace:
    thread: Thread
    release: Event
    loaded: list[FaissVectorIndex]
    failures: list[BaseException]


def _install_paused_reader(monkeypatch: pytest.MonkeyPatch) -> tuple[Event, Event]:
    entered, release = Event(), Event()
    original_payload = SnapshotStore._payload

    def pause(store: SnapshotStore, manifest: Path) -> object:
        if current_thread().name == "lockless-reader" and not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return original_payload(store, manifest)

    monkeypatch.setattr(SnapshotStore, "_payload", pause)
    return entered, release


def _install_paused_legacy_read(monkeypatch: pytest.MonkeyPatch) -> tuple[Event, Event]:
    entered, release = Event(), Event()
    original_read = faiss_index._read_legacy

    def pause(directory: Path) -> object:
        if current_thread().name == "lockless-reader":
            entered.set()
            assert release.wait(timeout=5)
        return original_read(directory)

    monkeypatch.setattr(faiss_index, "_read_legacy", pause)
    return entered, release


def _capture_load(
    directory: Path, loaded: list[FaissVectorIndex], failures: list[BaseException]
) -> None:
    try:
        loaded.append(FaissVectorIndex.load("products", directory))
    except BaseException as exc:  # test thread must surface every failure
        failures.append(exc)


def _start_lockless_load(directory: Path, release: Event) -> _LoadRace:
    loaded: list[FaissVectorIndex] = []
    failures: list[BaseException] = []
    thread = Thread(
        target=_capture_load, args=(directory, loaded, failures), name="lockless-reader"
    )
    thread.start()
    return _LoadRace(thread, release, loaded, failures)


async def _assert_race_loaded(race: _LoadRace, entity_id: str) -> None:
    race.release.set()
    race.thread.join(timeout=5)
    assert not race.thread.is_alive()
    assert race.failures == []
    result = await race.loaded[0].search([1.0, 0.0, 0.0, 0.0], k=1)
    assert [doc for doc, _ in result] == [entity_id]


def test_satisfies_the_shared_libs_protocol() -> None:
    assert isinstance(_index(), VectorIndex)


async def test_passes_the_supported_core_conformance_suite() -> None:
    async def factory(name: str, config: IndexConfig | None = None) -> FaissVectorIndex:
        return FaissVectorIndex(name, config)

    await assert_vector_index_conformance(factory)


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


async def test_scoped_delete_and_stats_match_the_supported_core_protocol() -> None:
    idx = _index()
    await idx.insert(
        [
            _emb("tenant-a", [1.0, 0.0, 0.0, 0.0], tenant_id="a"),
            _emb("tenant-b", [0.0, 1.0, 0.0, 0.0], tenant_id="b"),
        ]
    )

    await idx.delete(["tenant-a", "tenant-b"], filters={"tenant_id": "a"})

    assert [item for item, _ in await idx.search([0.0, 1.0, 0.0, 0.0], 5)] == ["tenant-b"]
    scoped = await idx.get_stats(filters={"tenant_id": "b"})
    assert scoped.vector_count == 1
    assert scoped.tombstone_count == 0


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

    state_path = _committed_files(tmp_path / "vec")[1]
    state = json.loads(state_path.read_text())
    state["faiss_ids"] = []
    _write_committed_state(tmp_path / "vec", state)
    with pytest.raises(ValueError, match="row count"):
        FaissVectorIndex.load("products", tmp_path / "vec")


async def test_failed_second_snapshot_rename_never_loads_same_shape_hybrid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given an old one-row snapshot and a same-shaped replacement with different meaning
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0], color="red")])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0], color="blue")])
    real_replace = os.replace
    calls = 0

    def fail_second_replace(src: object, dst: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated crash on second snapshot rename")
        real_replace(src, dst)

    # When the second file promotion fails after the first one became visible
    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(OSError, match="second snapshot rename"):
        new.save(directory)
    monkeypatch.setattr(os, "replace", real_replace)

    # Then load returns the last complete generation, never a cross-generation hybrid
    loaded = FaissVectorIndex.load("products", directory)
    red = await loaded.search([1.0, 0.0, 0.0, 0.0], k=1)
    blue = await loaded.search([0.0, 1.0, 0.0, 0.0], k=1)
    assert red == [("red", pytest.approx(0.0, abs=1e-6))]
    assert blue == [("red", pytest.approx(1.0, abs=1e-6))]


async def test_load_migrates_a_complete_legacy_pair_to_one_snapshot_commit(
    tmp_path: Path,
) -> None:
    # Given a valid index directory saved by 0.4.0
    source = tmp_path / "source"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    idx.save(source)
    index_path, state_path = _committed_files(source)
    shutil.copyfile(index_path, legacy / "index.faiss")
    shutil.copyfile(state_path, legacy / "state.json")

    # When the legacy directory is loaded
    loaded = FaissVectorIndex.load("products", legacy)

    # Then it is readable and durably migrated away from the two-file active format
    assert await loaded.search([1.0, 0.0, 0.0, 0.0], k=1) == [("red", pytest.approx(0.0, abs=1e-6))]
    assert len(list((legacy / "snapshots").glob("*.snapshot.json"))) == 1
    assert not (legacy / "index.faiss").exists()
    assert not (legacy / "state.json").exists()


async def test_should_load_legacy_pair_without_migration_when_directory_is_read_only(
    tmp_path: Path,
) -> None:
    # Given a valid 0.4.0 pair on an immutable mount
    legacy = await _write_legacy_pair(tmp_path)
    # When it is loaded without permission to acquire a lock or migrate
    loaded = _load_read_only_tree(legacy)
    # Then the legacy pair stays intact and readable without a write attempt
    assert await loaded.search([1.0, 0.0, 0.0, 0.0], k=1) == [("red", pytest.approx(0.0, abs=1e-6))]
    assert not (legacy / "snapshots").exists()
    assert (legacy / "index.faiss").is_file()
    assert (legacy / "state.json").is_file()


async def test_should_retry_read_only_legacy_load_when_writer_migrates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = await _write_legacy_pair(tmp_path)
    entered, release = _install_paused_legacy_read(monkeypatch)
    monkeypatch.setattr(faiss_index, "_acquire_load_lock", lambda _lock: False)
    race = _start_lockless_load(legacy, release)
    assert entered.wait(timeout=5)
    faiss_index._migrate_legacy(legacy, SnapshotStore(legacy))
    await _assert_race_loaded(race, "red")


async def test_should_refuse_a_symlinked_read_only_legacy_leaf(tmp_path: Path) -> None:
    # Given
    legacy = await _write_legacy_pair(tmp_path)
    external = tmp_path / "external.faiss"
    shutil.copyfile(legacy / "index.faiss", external)
    (legacy / "index.faiss").unlink()
    (legacy / "index.faiss").symlink_to(external)
    # When / Then
    with pytest.raises(ValueError, match="regular file"):
        _load_read_only_tree(legacy)


async def test_load_recovers_previous_generation_when_newest_manifest_is_corrupt(
    tmp_path: Path,
) -> None:
    # Given two committed generations and a corrupted newest commit record
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0])])
    new.save(directory)
    _snapshot_manifests(directory)[-1].write_bytes(b'{"format_version":')

    # When loading after the corrupt pointer is observed
    loaded = FaissVectorIndex.load("products", directory)

    # Then only the previous complete generation is recovered
    assert await loaded.search([1.0, 0.0, 0.0, 0.0], k=1) == [("red", pytest.approx(0.0, abs=1e-6))]


async def test_stale_writer_cannot_overwrite_a_cross_process_commit(tmp_path: Path) -> None:
    directory = tmp_path / "vec"
    base = _index()
    await base.insert([_emb("base", [1.0, 0.0, 0.0, 0.0])])
    base.save(directory)
    context = multiprocessing.get_context("spawn")
    ready_a, send_a = context.Pipe(duplex=False)
    ready_b, send_b = context.Pipe(duplex=False)
    go_a, release_a = context.Pipe(duplex=False)
    go_b, release_b = context.Pipe(duplex=False)
    writer_a = context.Process(
        target=_commit_loaded_writer,
        args=(str(directory), "writer-a", send_a, go_a),
    )
    writer_b = context.Process(
        target=_commit_loaded_writer,
        args=(str(directory), "writer-b", send_b, go_b),
    )
    writer_a.start()
    writer_b.start()
    assert ready_a.recv() is True
    assert ready_b.recv() is True

    release_a.send(True)
    assert ready_a.recv() == "saved"
    release_b.send(True)
    assert ready_b.recv() == "conflict"
    writer_a.join(timeout=5)
    writer_b.join(timeout=5)
    assert writer_a.exitcode == writer_b.exitcode == 0
    loaded = FaissVectorIndex.load("products", directory)
    assert {item for item, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=5)} == {
        "base",
        "writer-a",
    }


async def test_load_rejects_same_shape_index_swapped_across_generations(tmp_path: Path) -> None:
    # Given two same-shaped commits whose newest FAISS file is replaced by the older bytes
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0])])
    new.save(directory)
    manifests = [json.loads(path.read_text()) for path in _snapshot_manifests(directory)]
    old_index = directory / "snapshots" / f"{manifests[0]['generation']}.faiss"
    new_index = directory / "snapshots" / f"{manifests[1]['generation']}.faiss"
    shutil.copyfile(old_index, new_index)

    # When load sees a binary that still passes row-count and dimension checks
    loaded = FaissVectorIndex.load("products", directory)

    # Then its manifest digest rejects the hybrid and recovers the old complete generation
    assert await loaded.search([1.0, 0.0, 0.0, 0.0], k=1) == [("red", pytest.approx(0.0, abs=1e-6))]


async def test_load_ignores_newest_manifest_with_a_stale_sequence_claim(tmp_path: Path) -> None:
    # Given a newest filename whose signed-in content claims the previous sequence
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0])])
    new.save(directory)
    newest = _snapshot_manifests(directory)[-1]
    stale = json.loads(newest.read_text())
    stale["sequence"] = 1
    newest.write_text(json.dumps(stale))

    # When loading the directory
    loaded = FaissVectorIndex.load("products", directory)

    # Then the stale commit record cannot select its generation
    assert await loaded.search([1.0, 0.0, 0.0, 0.0], k=1) == [("red", pytest.approx(0.0, abs=1e-6))]


async def test_save_bounds_snapshot_retention_to_current_and_recovery_generation(
    tmp_path: Path,
) -> None:
    # Given more successful saves than recovery needs
    directory = tmp_path / "vec"
    for number in range(5):
        idx = _index()
        await idx.insert([_emb(f"item-{number}", [1.0, 0.0, 0.0, 0.0])])
        idx.save(directory)

    # When the post-commit collector runs
    manifests = _snapshot_manifests(directory)
    data_files = list((directory / "snapshots").glob("*.*"))

    # Then disk use is bounded to the active generation plus one recovery generation
    assert len(manifests) == 2
    assert len([path for path in data_files if path.suffix == ".faiss"]) == 2
    assert len([path for path in data_files if path.name.endswith(".state.json")]) == 2
    loaded = FaissVectorIndex.load("products", directory)
    assert [doc for doc, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=1)] == ["item-4"]


async def test_should_retry_lockless_load_when_writer_gc_replaces_enumerated_generations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "vec"
    await _save_generations(directory, 0, 2)
    entered, release = _install_paused_reader(monkeypatch)
    monkeypatch.setattr(faiss_index, "_acquire_load_lock", lambda _lock: False)
    race = _start_lockless_load(directory, release)
    assert entered.wait(timeout=5)
    await _save_generations(directory, 2, 4)
    await _assert_race_loaded(race, "item-3")


async def test_save_waits_for_the_real_cross_process_snapshot_lock(tmp_path: Path) -> None:
    # Given the OS-backed lock held by a separate spawned process
    directory = tmp_path / "vec"
    directory.mkdir()
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    context = multiprocessing.get_context("spawn")
    ready_receive, ready_send = context.Pipe(duplex=False)
    release_receive, release_send = context.Pipe(duplex=False)
    owner = context.Process(
        target=_hold_snapshot_lock,
        args=(str(directory / ".snapshot.lock"), ready_send, release_receive),
    )
    owner.start()
    assert ready_receive.poll(timeout=5)
    assert ready_receive.recv() is True
    finished = Event()
    failures: list[Exception] = []

    def save() -> None:
        try:
            idx.save(directory)
        except Exception as exc:  # test thread must report every unexpected failure
            failures.append(exc)
        finally:
            finished.set()

    # When save starts while another process-compatible lock owner is active
    thread = Thread(target=save)
    thread.start()

    # Then it cannot enter the snapshot/GC boundary until that owner releases it
    assert not finished.wait(timeout=0.1)
    release_send.send(True)
    assert finished.wait(timeout=5)
    thread.join(timeout=5)
    owner.join(timeout=5)
    assert owner.exitcode == 0
    assert failures == []


async def test_snapshot_lock_wait_is_bounded_by_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a separate process holding the lock past a configured short deadline
    directory = tmp_path / "vec"
    directory.mkdir()
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    context = multiprocessing.get_context("spawn")
    ready_receive, ready_send = context.Pipe(duplex=False)
    release_receive, release_send = context.Pipe(duplex=False)
    owner = context.Process(
        target=_hold_snapshot_lock,
        args=(str(directory / ".snapshot.lock"), ready_send, release_receive),
    )
    owner.start()
    assert ready_receive.poll(timeout=5)
    assert ready_receive.recv() is True
    monkeypatch.setenv("EDGEPROC_SNAPSHOT_LOCK_TIMEOUT", "0.05")

    # When save cannot acquire the complete persistence boundary in time
    with pytest.raises(Timeout):
        idx.save(directory)

    # Then it fails instead of waiting forever, and the owner exits normally
    release_send.send(True)
    owner.join(timeout=5)
    assert owner.exitcode == 0


async def test_load_waits_until_snapshot_gc_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a save paused inside GC after publishing its manifest
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0])])
    entered_gc = Event()
    release_gc = Event()
    original_gc = SnapshotStore._remove_unreferenced_data

    def paused_gc(store: SnapshotStore, generations: set[str]) -> None:
        entered_gc.set()
        assert release_gc.wait(timeout=5)
        original_gc(store, generations)

    monkeypatch.setattr(SnapshotStore, "_remove_unreferenced_data", paused_gc)
    save_thread = Thread(target=new.save, args=(directory,))
    save_thread.start()
    assert entered_gc.wait(timeout=5)
    loaded: list[FaissVectorIndex] = []
    load_thread = Thread(target=lambda: loaded.append(FaissVectorIndex.load("products", directory)))
    load_thread.start()

    # When load races the collector, it cannot observe files during the sweep
    load_thread.join(timeout=0.1)
    assert load_thread.is_alive()
    release_gc.set()
    save_thread.join(timeout=5)
    load_thread.join(timeout=5)

    # Then it sees the newly committed generation after the whole boundary completes
    assert [doc for doc, _ in await loaded[0].search([0.0, 1.0, 0.0, 0.0], k=1)] == ["blue"]


async def test_failed_manifest_commit_keeps_old_generation_and_gc_removes_orphans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a complete old generation and failure at the sole commit rename
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0])])
    real_replace = os.replace

    def fail_manifest_commit(src: object, dst: object) -> None:
        if str(dst).endswith(".snapshot.json"):
            raise OSError("simulated crash before snapshot commit")
        real_replace(src, dst)

    # When both staged data files exist but their manifest cannot commit
    monkeypatch.setattr(os, "replace", fail_manifest_commit)
    with pytest.raises(OSError, match="before snapshot commit"):
        new.save(directory)
    monkeypatch.setattr(os, "replace", real_replace)

    # Then load keeps the old value; the next save collects the abandoned generation
    loaded = FaissVectorIndex.load("products", directory)
    assert [doc for doc, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=1)] == ["red"]
    new.save(directory)
    assert len(list((directory / "snapshots").glob("*.faiss"))) == 2


async def test_post_commit_gc_failure_does_not_report_a_failed_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    directory = tmp_path / "vec"
    idx = _index()
    await idx.insert([_emb("old", [1.0, 0.0, 0.0, 0.0])])
    idx.save(directory)
    await idx.insert([_emb("new", [0.0, 1.0, 0.0, 0.0])])

    def fail_gc(_store: SnapshotStore) -> None:
        raise OSError("simulated post-commit GC failure")

    monkeypatch.setattr(SnapshotStore, "_collect_old_generations", fail_gc)
    idx.save(directory)
    idx.save(directory)
    assert "snapshot committed; deferred cleanup failed" in caplog.text
    loaded = FaissVectorIndex.load("products", directory)
    assert {item for item, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], 5)} == {
        "old",
        "new",
    }


async def test_save_refuses_a_symlinked_snapshot_directory(tmp_path: Path) -> None:
    directory = tmp_path / "vec"
    outside = tmp_path / "outside"
    directory.mkdir()
    outside.mkdir()
    (directory / "snapshots").symlink_to(outside, target_is_directory=True)
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])

    with pytest.raises(ValueError, match="snapshot directory"):
        idx.save(directory)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("leaf", ["manifest", "index", "state"])
async def test_load_refuses_symlinked_snapshot_leaves(tmp_path: Path, leaf: str) -> None:
    directory = tmp_path / "vec"
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    idx.save(directory)
    manifest_path = _snapshot_manifests(directory)[0]
    data_paths = {"index": _committed_files(directory)[0], "state": _committed_files(directory)[1]}
    target = manifest_path if leaf == "manifest" else data_paths[leaf]
    outside = tmp_path / target.name
    outside.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="no complete vector-index snapshot"):
        FaissVectorIndex.load("products", directory)


async def test_partial_state_write_never_becomes_a_committed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given one complete generation and a writer that crashes after a partial sidecar write
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    new = _index()
    await new.insert([_emb("blue", [0.0, 1.0, 0.0, 0.0])])
    real_write = snapshot_store._write_fsynced

    def partial_write(path: Path, data: bytes) -> None:
        if path.name.endswith(".state.json.tmp"):
            path.write_bytes(data[:8])
            raise OSError("simulated crash during sidecar write")
        real_write(path, data)

    # When save stops before either staged file is committed
    monkeypatch.setattr(snapshot_store, "_write_fsynced", partial_write)
    with pytest.raises(OSError, match="during sidecar write"):
        new.save(directory)

    # Then only the old complete generation can load
    loaded = FaissVectorIndex.load("products", directory)
    assert [doc for doc, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=1)] == ["red"]


async def test_first_snapshot_fsyncs_each_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a recorder around the real directory durability primitive
    fsynced: list[Path] = []
    real_fsync_directory = snapshot_store._fsync_directory

    def record_directory(path: Path) -> None:
        fsynced.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(snapshot_store, "_fsync_directory", record_directory)
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    directory = tmp_path / "new-parent" / "vec"

    # When the first generation creates its directory and commits its manifest
    idx.save(directory)

    # Then both new directory entries and the commit record are durably linked
    assert tmp_path in fsynced
    assert directory.parent in fsynced
    assert directory in fsynced
    assert directory / "snapshots" in fsynced


async def test_load_fails_closed_when_no_complete_generation_remains(tmp_path: Path) -> None:
    # Given the only commit record is corrupt
    directory = tmp_path / "vec"
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    idx.save(directory)
    _snapshot_manifests(directory)[0].write_bytes(b"not-json")

    # When load cannot identify any complete generation
    with pytest.raises(ValueError, match="no complete vector-index snapshot"):
        FaissVectorIndex.load("products", directory)


async def test_malformed_manifest_filename_cannot_block_the_last_complete_generation(
    tmp_path: Path,
) -> None:
    # Given a complete snapshot plus an attacker/corruption-created pointer filename
    directory = tmp_path / "vec"
    idx = _index()
    await idx.insert([_emb("red", [1.0, 0.0, 0.0, 0.0])])
    idx.save(directory)
    (directory / "snapshots" / "not-a-sequence.snapshot.json").write_text("{}")

    # When load enumerates commit records
    loaded = FaissVectorIndex.load("products", directory)

    # Then malformed names cannot crash sorting or hide the last complete generation
    assert [doc for doc, _ in await loaded.search([1.0, 0.0, 0.0, 0.0], k=1)] == ["red"]


async def test_corrupt_max_sequence_cannot_turn_save_into_a_successful_noop(tmp_path: Path) -> None:
    directory = tmp_path / "vec"
    old = _index()
    await old.insert([_emb("old", [1.0, 0.0, 0.0, 0.0])])
    old.save(directory)
    poison = directory / "snapshots" / "99999999999999999999.snapshot.json"
    poison.write_text("{}")
    new = _index()
    await new.insert([_emb("new", [0.0, 1.0, 0.0, 0.0])])

    new.save(directory)

    loaded = FaissVectorIndex.load("products", directory)
    assert [item for item, _ in await loaded.search([0.0, 1.0, 0.0, 0.0], k=1)] == ["new"]
    assert all(len(path.name.split(".", 1)[0]) == 20 for path in _snapshot_manifests(directory))


def test_valid_max_sequence_fails_closed_before_an_undiscoverable_commit(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "vec")
    index = tmp_path / "index.bin"
    index.write_bytes(b"index")
    store.commit(lambda path: shutil.copyfile(index, path), b"state")
    manifest = _snapshot_manifests(tmp_path / "vec")[0]
    body = json.loads(manifest.read_text())
    body["sequence"] = 99_999_999_999_999_999_999
    maximum = manifest.with_name("99999999999999999999.snapshot.json")
    maximum.write_text(json.dumps(body))
    manifest.unlink()

    with pytest.raises(SnapshotSequenceError, match="exhausted"):
        store.commit(lambda path: shutil.copyfile(index, path), b"state")
    assert _snapshot_manifests(tmp_path / "vec") == [maximum]


def test_snapshot_hashing_has_bounded_memory_for_large_files(tmp_path: Path) -> None:
    large = tmp_path / "large.faiss"
    with large.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024)
    tracemalloc.start()
    snapshot_store._digest(large)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


async def test_load_rejects_a_dimension_mismatch_against_the_state_sidecar(
    tmp_path: Path,
) -> None:
    # The saved FAISS binary is built for dimension 4. A sidecar hand-edited (or torn
    # by a crash) to claim a different dimension must not reopen: every vector op
    # after would be shaped wrong with nothing to say so.
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    idx.save(tmp_path / "vec")

    state_path = _committed_files(tmp_path / "vec")[1]
    state = json.loads(state_path.read_text())
    state["config"]["dimension"] = 8
    _write_committed_state(tmp_path / "vec", state)

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

    state_path = _committed_files(tmp_path / "vec")[1]
    state = json.loads(state_path.read_text())
    state["meta"]["ghost"] = {}
    _write_committed_state(tmp_path / "vec", state)

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

    state_path = _committed_files(tmp_path / "vec")[1]
    state = json.loads(state_path.read_text())
    state["config"]["distance_metric"] = "l2"
    _write_committed_state(tmp_path / "vec", state)

    with pytest.raises(UnsupportedIndexOptionError, match="distance_metric"):
        FaissVectorIndex.load("products", tmp_path / "vec")


async def test_load_supports_a_read_only_saved_index(tmp_path: Path) -> None:
    # Given
    directory = tmp_path / "vec"
    idx = _index()
    await idx.insert([_emb("a", [1.0, 0.0, 0.0, 0.0])])
    idx.save(directory)
    # When
    loaded = _load_read_only_tree(directory)
    result = await loaded.search([1.0, 0.0, 0.0, 0.0], k=1)
    # Then
    assert result == [("a", pytest.approx(0.0, abs=1e-6))]


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
