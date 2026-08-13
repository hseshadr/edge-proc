"""Content-addressed store — round-trip, fail-closed integrity, atomic swap, GC.

The store's contract is the project's trust + crash-safety boundary, so these
tests pin it before the sync engine depends on it:

- the content-address (``sha256(plaintext)``) IS the integrity check: a tampered
  stored file must be rejected fail-closed on read (``IntegrityError``), never
  surfaced as a stray zstd/sha error;
- ``promote`` is atomic — a crash mid-swap leaves the OLD pointer intact and
  readable, never a torn/empty ``active``;
- ``gc`` removes only true orphans and is a no-op when nothing is promoted (it
  must never wipe a store with no active pointer).
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest
import zstandard

from edgeproc.bundles import cas
from edgeproc.bundles.cas import (
    CacheStore,
    FilesystemCacheStore,
    IntegrityError,
    RollbackError,
)
from edgeproc.bundles.manifest import (
    ChunkRef,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    manifest_digest,
)

# Highly compressible payload (proves zstd shrinks the stored file).
_COMPRESSIBLE = b"edgeproc " * 4096


def _chunk_hash(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _store(tmp_path: Path) -> FilesystemCacheStore:
    return FilesystemCacheStore(tmp_path)


def _manifest_for(
    store: FilesystemCacheStore, *payloads: bytes, version: str = "1.0.0"
) -> IndexManifest:
    """Store ``payloads`` as chunks and return a manifest of one file over them.

    ``version`` is explicit because ``promote`` requires a strictly-greater version (or a
    strictly-greater ``sequence``) to replace an active pointer: a test that promotes twice
    must move the version forward, exactly as a real publisher does.
    """
    refs = [ChunkRef(hash=store.put_chunk(p), size=len(p)) for p in payloads]
    blob = b"".join(payloads)
    entry = FileEntry(
        path="index.faiss",
        file_type="faiss",
        size=len(blob),
        file_sha256=_chunk_hash(blob),
        chunks=refs,
    )
    return IndexManifest(bundle_id="b", version=version, files=[entry])


def _promote_manifest(store: FilesystemCacheStore, manifest: IndexManifest) -> VersionPointer:
    digest = store.put_manifest(canonical_bytes(manifest))
    pointer = VersionPointer(manifest_hash=digest, version=manifest.version, signature="sig")
    store.promote(pointer)
    return pointer


def test_chunk_round_trip_and_zstd_layout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chunk_hash = store.put_chunk(_COMPRESSIBLE)
    assert store.get_chunk(chunk_hash) == _COMPRESSIBLE
    on_disk = tmp_path / "chunks" / chunk_hash[:2] / chunk_hash
    assert on_disk.is_file()
    # zstd actually compressed: stored bytes are smaller than the plaintext.
    assert on_disk.stat().st_size < len(_COMPRESSIBLE)


def test_atomic_write_fsyncs_parent_directory_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a recorder that distinguishes file fsyncs from directory fsyncs
    directory_fsyncs = 0
    real_fsync = os.fsync

    def record_fsync(fd: int) -> None:
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record_fsync)

    # When a CAS object becomes visible through an atomic replace
    _store(tmp_path).put_chunk(b"durable content address")

    # Then its parent directory entry is also made durable
    assert directory_fsyncs >= 1


def test_first_chunk_durably_links_every_new_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "new-parent" / "cache"
    fsynced: list[Path] = []
    real_fsync = cas._fsync_directory

    def record(path: Path) -> None:
        fsynced.append(path)
        real_fsync(path)

    monkeypatch.setattr(cas, "_fsync_directory", record)
    store = FilesystemCacheStore(root)
    digest = store.put_chunk(b"first")

    assert tmp_path in fsynced
    assert root.parent in fsynced
    assert root in fsynced
    assert root / "chunks" in fsynced
    assert root / "chunks" / digest[:2] in fsynced


def test_atomic_write_can_retry_after_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a first attempt that reaches the durable temp file but fails its rename
    store = _store(tmp_path)
    real_replace = os.replace

    def fail_replace(_src: object, _dst: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        store.put_chunk(b"retryable payload")

    # When the filesystem is healthy and the same operation retries
    monkeypatch.setattr(os, "replace", real_replace)
    digest = store.put_chunk(b"retryable payload")

    # Then no abandoned same-PID temp file wedges the content address
    assert store.get_chunk(digest) == b"retryable payload"


def test_put_chunk_idempotent_and_has_chunk(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = b"some chunk bytes"
    digest = _chunk_hash(data)
    assert store.has_chunk(digest) is False
    first = store.put_chunk(data)
    assert first == digest
    assert store.has_chunk(digest) is True
    # Re-put identical data: same hash, no error, no rewrite.
    assert store.put_chunk(data) == first


def test_cas_refuses_same_root_symlinked_object_leaves(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = b'{"safe":true}'
    digest = hashlib.sha256(manifest).hexdigest()
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    (tmp_path / "manifests" / digest).symlink_to(victim)

    with pytest.raises(IntegrityError, match="symlink"):
        store.put_manifest(manifest)
    assert victim.read_bytes() == b"unchanged"


def test_cas_refuses_symlinked_chunk_and_active_leaves(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = b"same-root leaf"
    digest = store.put_chunk(data)
    chunk = tmp_path / "chunks" / digest[:2] / digest
    victim = tmp_path / "victim"
    victim.write_bytes(chunk.read_bytes())
    chunk.unlink()
    chunk.symlink_to(victim)
    with pytest.raises(IntegrityError, match="symlink"):
        store.get_chunk(digest)

    active_victim = tmp_path / "active-victim"
    active_victim.write_text("{}")
    (tmp_path / "active").symlink_to(active_victim)
    with pytest.raises(IntegrityError, match="symlink"):
        store.read_active()


def test_get_chunk_fail_closed_on_corruption(tmp_path: Path) -> None:
    store = _store(tmp_path)
    chunk_hash = store.put_chunk(_COMPRESSIBLE)
    on_disk = tmp_path / "chunks" / chunk_hash[:2] / chunk_hash
    on_disk.write_bytes(b"not zstd at all")  # corrupt the stored compressed file
    with pytest.raises(IntegrityError):
        store.get_chunk(chunk_hash)


def test_get_chunk_fail_closed_on_hash_mismatch(tmp_path: Path) -> None:
    # Valid zstd whose plaintext does NOT hash to the file's name (swapped content).
    store = _store(tmp_path)
    chunk_hash = store.put_chunk(b"the real payload")
    on_disk = tmp_path / "chunks" / chunk_hash[:2] / chunk_hash
    on_disk.write_bytes(zstandard.compress(b"a different payload"))
    with pytest.raises(IntegrityError):
        store.get_chunk(chunk_hash)


def test_get_chunk_rejects_decompression_bomb(tmp_path: Path) -> None:
    # A tiny zstd file whose plaintext explodes far past the store's cap is a
    # decompression bomb: it must be refused fail-closed, never inflated into memory.
    store = FilesystemCacheStore(tmp_path, max_decompressed_bytes=1024)
    bomb_plaintext = b"\x00" * (1024 * 1024)  # 1 MiB of zeros → tiny zstd, 1000x the cap
    chunk_hash = _chunk_hash(bomb_plaintext)  # address it by its real content hash
    on_disk = tmp_path / "chunks" / chunk_hash[:2] / chunk_hash
    on_disk.parent.mkdir(parents=True, exist_ok=True)
    on_disk.write_bytes(zstandard.compress(bomb_plaintext))
    with pytest.raises(IntegrityError, match="max decompressed size"):
        store.get_chunk(chunk_hash)


def test_put_chunk_compressed_rejects_decompression_bomb(tmp_path: Path) -> None:
    # The network-facing ingest path must also refuse a bomb — and leave nothing on disk.
    store = FilesystemCacheStore(tmp_path, max_decompressed_bytes=1024)
    bomb_plaintext = b"\x00" * (1024 * 1024)
    chunk_hash = _chunk_hash(bomb_plaintext)
    with pytest.raises(IntegrityError):
        store.put_chunk_compressed(chunk_hash, zstandard.compress(bomb_plaintext))
    assert store.has_chunk(chunk_hash) is False  # fail-closed cleanup


def test_put_chunk_compressed_rejects_content_address_mismatch(tmp_path: Path) -> None:
    # Given
    store = _store(tmp_path)
    claimed_hash = _chunk_hash(b"claimed payload")

    # When / Then
    with pytest.raises(IntegrityError, match="content-address check"):
        store.put_chunk_compressed(claimed_hash, zstandard.compress(b"different payload"))
    assert store.has_chunk(claimed_hash) is False


def test_put_chunk_compressed_rejects_digest_path_traversal(tmp_path: Path) -> None:
    # Given
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    store = FilesystemCacheStore(tmp_path / "cache")

    # When / Then
    with pytest.raises(IntegrityError, match="invalid SHA-256 digest"):
        store.put_chunk_compressed("../../victim", zstandard.compress(b"attacker"))
    assert victim.read_bytes() == b"original"


def test_get_manifest_rejects_digest_path_traversal(tmp_path: Path) -> None:
    # Given
    store = FilesystemCacheStore(tmp_path)

    # When / Then
    with pytest.raises(IntegrityError, match="invalid SHA-256 digest"):
        store.get_manifest("../active")


def test_put_manifest_refuses_preplanted_atomic_temp_symlink(tmp_path: Path) -> None:
    # Given
    root = tmp_path / "cache"
    store = FilesystemCacheStore(root)
    manifest = b"signed manifest"
    digest = hashlib.sha256(manifest).hexdigest()
    victim = tmp_path / "victim"
    victim.write_bytes(b"original")
    (root / "manifests" / f"{digest}.tmp.{os.getpid()}").symlink_to(victim)

    # When / Then
    with pytest.raises(IntegrityError, match="atomic write"):
        store.put_manifest(manifest)
    assert victim.read_bytes() == b"original"


@pytest.mark.parametrize("directory", ["chunks", "manifests"])
def test_store_rejects_symlinked_cas_directory(tmp_path: Path, directory: str) -> None:
    # Given
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "cache"
    root.mkdir()
    (root / directory).symlink_to(outside, target_is_directory=True)

    # When / Then
    with pytest.raises(IntegrityError, match=r"symlink|escapes"):
        FilesystemCacheStore(root)


def test_manifest_round_trip_and_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manifest = _manifest_for(store, b"a" * 32, b"b" * 32)
    raw = canonical_bytes(manifest)
    digest = store.put_manifest(raw)
    assert digest == manifest_digest(manifest)
    assert store.get_manifest(digest) == raw
    # Tamper the stored manifest → fail closed.
    (tmp_path / "manifests" / digest).write_bytes(raw + b"x")
    with pytest.raises(IntegrityError):
        store.get_manifest(digest)


def test_promote_and_read_active_swaps_to_newest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.read_active() is None
    m1 = _manifest_for(store, b"v1" * 16, version="1.0.0")
    p1 = _promote_manifest(store, m1)
    assert store.read_active() == p1
    m2 = _manifest_for(store, b"v2" * 16, version="1.0.1")
    p2 = _promote_manifest(store, m2)
    assert store.read_active() == p2


def test_promote_refuses_rollback_to_older_version(tmp_path: Path) -> None:
    # Anti-rollback: once a NEWER version is active, a validly-signed but OLDER pointer
    # must be refused — an attacker replaying a stale `/latest` cannot downgrade a client.
    store = _store(tmp_path)
    newer = _manifest_for(store, b"new" * 16)
    older = _manifest_for(store, b"old" * 16)
    new_pointer = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(newer)), version="2.0.0", signature="sig"
    )
    old_pointer = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(older)), version="1.0.0", signature="sig"
    )
    store.promote(new_pointer)
    with pytest.raises(RollbackError):
        store.promote(old_pointer)
    assert store.read_active() == new_pointer  # the downgrade never took effect


def test_promote_refuses_an_equal_version_and_allows_a_forward_bump(tmp_path: Path) -> None:
    # Only a STRICTLY GREATER version proves freshness. An equal version is a different
    # bundle wearing the same label, and nothing in it says which of the two is newer — so
    # it is refused, while a forward bump still promotes.
    #
    # CONTRACT REVERSED 2026-08-03. This test asserted the exact opposite until then
    # ("equal version, different content → allowed"), reading `Version(a) < Version(b)`
    # returning False as affirmative proof of freshness. That is how any earlier,
    # genuinely-signed pointer at the same version replaced the installed one. A replayed
    # artifact IS validly signed, so "cannot tell" must REJECT, not accept. An equal-version
    # re-publish keeps shipping by binding a strictly-greater monotonic `sequence` — see
    # tests/bundles/test_fail_closed_freshness.py for the attack and that escape hatch.
    store = _store(tmp_path)
    same_a = _manifest_for(store, b"a" * 16)
    same_b = _manifest_for(store, b"b" * 16)
    p_a = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(same_a)), version="1.0.0", signature="s"
    )
    p_b = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(same_b)), version="1.0.0", signature="s"
    )
    forward = _manifest_for(store, b"c" * 16)
    p_c = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(forward)), version="1.0.1", signature="s"
    )
    store.promote(p_a)
    with pytest.raises(RollbackError):
        store.promote(p_b)  # equal version, different content → REFUSED
    assert store.read_active() == p_a  # the unproven swap never took effect
    store.promote(p_c)  # forward bump → allowed
    assert store.read_active() == p_c


def test_promote_refuses_a_version_pep440_cannot_compare(tmp_path: Path) -> None:
    # Fail-CLOSED: when a version string is not PEP 440 there is nothing to compare, so the
    # promote cannot PROVE it is fresher — and an unprovable promote is a refusal.
    #
    # This test asserted the exact opposite until 2026-07-30 ("cannot prove a downgrade →
    # allow"), which is how a 2019 pointer replaced a 2026 one for any publisher using
    # date-style versions. See tests/bundles/test_fail_closed_freshness.py for the attack
    # driven end to end, and for the `sequence` escape hatch this leaves open.
    store = _store(tmp_path)
    active = _manifest_for(store, b"act" * 16)
    incoming = _manifest_for(store, b"inc" * 16)
    active_ptr = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(active)),
        version="2.0.0",
        signature="s",
    )
    weird_ptr = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(incoming)),
        version="not-a-semver",
        signature="s",
    )
    store.promote(active_ptr)
    with pytest.raises(RollbackError):
        store.promote(weird_ptr)
    assert store.read_active() == active_ptr


def test_promote_refuses_an_empty_version_string(tmp_path: Path) -> None:
    # The degenerate end of "unparseable": `version` is required by the schema, so it can
    # never be absent, but it CAN be empty. `Version("")` proves nothing, so it is refused.
    store = _store(tmp_path)
    active_ptr = VersionPointer(manifest_hash="ab" * 32, version="2.0.0", signature="s")
    blank_ptr = VersionPointer(manifest_hash="cd" * 32, version="", signature="s")
    store.promote(active_ptr)
    with pytest.raises(RollbackError):
        store.promote(blank_ptr)
    assert store.read_active() == active_ptr


def test_promote_refuses_when_the_active_pointer_cannot_be_read(tmp_path: Path) -> None:
    # The anti-rollback guard's INPUT is the active pointer. An `active` that exists but is
    # malformed must refuse the promote as a catalogued IntegrityError — not leak a raw
    # pydantic ValidationError past every `except IntegrityError` fail-closed handler.
    store = _store(tmp_path)
    store.promote(VersionPointer(manifest_hash="ab" * 32, version="2.0.0", signature="s"))
    (tmp_path / "active").write_bytes(b"{not json")

    with pytest.raises(IntegrityError):
        store.promote(VersionPointer(manifest_hash="cd" * 32, version="0.0.1", signature="s"))
    assert (tmp_path / "active").read_bytes() == b"{not json"  # the swap never happened


def test_promote_refuses_when_active_exists_but_is_not_a_file(tmp_path: Path) -> None:
    # `is_file()` answered False for an `active` that EXISTS as a directory, so read_active
    # returned None and the anti-rollback guard was skipped entirely — the only thing that
    # then stopped the promote was os.replace failing. A refusal must come from the guard.
    store = _store(tmp_path)
    store.promote(VersionPointer(manifest_hash="ab" * 32, version="2.0.0", signature="s"))
    (tmp_path / "active").unlink()
    (tmp_path / "active").mkdir()

    with pytest.raises(IntegrityError):
        store.promote(VersionPointer(manifest_hash="cd" * 32, version="0.0.1", signature="s"))


def test_first_promote_needs_no_freshness_proof(tmp_path: Path) -> None:
    # Nothing is active yet, so there is nothing to be fresher THAN: the first promote of
    # an unparseable version is accepted. Fail-closed applies to replacing trusted state.
    store = _store(tmp_path)
    manifest = _manifest_for(store, b"first" * 16)
    pointer = VersionPointer(
        manifest_hash=store.put_manifest(canonical_bytes(manifest)),
        version="not-a-semver",
        signature="s",
    )
    store.promote(pointer)
    assert store.read_active() == pointer


def test_promote_crash_safety_keeps_old_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    m1 = _manifest_for(store, b"v1" * 16, version="1.0.0")
    p1 = _promote_manifest(store, m1)
    # Stage v2 fully (chunks + manifest) BEFORE the simulated crash — only the
    # active-pointer swap should fail, exactly as a real crash mid-promote would.
    m2 = _manifest_for(store, b"v2" * 16, version="1.0.1")
    digest2 = store.put_manifest(canonical_bytes(m2))
    p2 = VersionPointer(manifest_hash=digest2, version=m2.version, signature="sig")

    real_replace = os.replace

    def _boom(src: object, dst: object) -> None:
        raise OSError("simulated crash during swap")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated crash"):
        store.promote(p2)
    monkeypatch.setattr(os, "replace", real_replace)
    # The active pointer is still the intact p1 — no torn/empty active file.
    assert store.read_active() == p1


def test_gc_removes_orphans_keeps_active_and_shared(tmp_path: Path) -> None:
    store = _store(tmp_path)
    shared = b"shared-chunk-payload" * 4
    only_a = b"only-in-a" * 4
    only_b = b"only-in-b" * 4
    manifest_a = _manifest_for(store, shared, only_a)
    manifest_b = _manifest_for(store, shared, only_b)
    digest_b = store.put_manifest(canonical_bytes(manifest_b))
    _promote_manifest(store, manifest_a)

    removed = store.gc()

    # manifest_b + only_b are orphans; manifest_a, shared, only_a survive.
    assert removed > 0
    assert not (tmp_path / "manifests" / digest_b).exists()
    a_digest = manifest_digest(manifest_a)
    assert (tmp_path / "manifests" / a_digest).exists()
    for payload in (shared, only_a):
        assert store.get_chunk(_chunk_hash(payload)) == payload
    assert not store.has_chunk(_chunk_hash(only_b))


def test_gc_refuses_symlinked_chunk_shard_without_deleting_external_file(tmp_path: Path) -> None:
    store = _store(tmp_path / "cache")
    _promote_manifest(store, _manifest_for(store, b"active payload"))
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / ("f" * 64)
    victim.write_bytes(b"must survive")
    (store.root / "chunks" / "ff").symlink_to(outside, target_is_directory=True)

    with pytest.raises(IntegrityError, match="symlink"):
        store.gc()

    assert victim.read_bytes() == b"must survive"


def test_gc_refuses_symlinked_chunk_leaf_without_touching_target(tmp_path: Path) -> None:
    store = _store(tmp_path / "cache")
    _promote_manifest(store, _manifest_for(store, b"active payload"))
    victim = tmp_path / "outside-chunk"
    victim.write_bytes(b"must survive")
    shard = store.root / "chunks" / "ff"
    shard.mkdir()
    (shard / ("f" * 64)).symlink_to(victim)

    with pytest.raises(IntegrityError, match="symlink"):
        store.gc()

    assert victim.read_bytes() == b"must survive"


def test_gc_refuses_a_non_directory_chunk_shard(tmp_path: Path) -> None:
    store = _store(tmp_path / "cache")
    _promote_manifest(store, _manifest_for(store, b"active payload"))
    (store.root / "chunks" / "ff").write_bytes(b"not a shard")

    with pytest.raises(IntegrityError, match="real directory"):
        store.gc()


def test_gc_refuses_symlinked_manifest_leaf_without_touching_target(tmp_path: Path) -> None:
    store = _store(tmp_path / "cache")
    _promote_manifest(store, _manifest_for(store, b"active payload"))
    victim = tmp_path / "outside-manifest"
    victim.write_bytes(b"must survive")
    (store.root / "manifests" / ("f" * 64)).symlink_to(victim)

    with pytest.raises(IntegrityError, match="symlink"):
        store.gc()

    assert victim.read_bytes() == b"must survive"


def test_gc_fail_closed_on_non_canonical_active_manifest(tmp_path: Path) -> None:
    # The active pointer names a stored blob that parses but is NOT canonical
    # (its bytes != canonical_bytes), so its name is not its true digest → reject.
    store = _store(tmp_path)
    manifest = _manifest_for(store, b"z" * 16)
    non_canonical = b" " + canonical_bytes(manifest)  # parses, but extra byte
    digest = store.put_manifest(non_canonical)
    store.promote(VersionPointer(manifest_hash=digest, version="1.0.0", signature="s"))
    with pytest.raises(IntegrityError):
        store.gc()


def test_gc_no_active_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_chunk(b"orphan one")
    store.put_chunk(b"orphan two")
    store.put_manifest(canonical_bytes(_manifest_for(store, b"x" * 8)))
    assert store.gc() == 0
    assert store.has_chunk(_chunk_hash(b"orphan one")) is True


def test_filesystem_store_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(FilesystemCacheStore(tmp_path), CacheStore)
