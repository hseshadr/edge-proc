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
from pathlib import Path

import pytest
import zstandard

from edgeproc.bundles.cas import CacheStore, FilesystemCacheStore, IntegrityError
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


def _manifest_for(store: FilesystemCacheStore, *payloads: bytes) -> IndexManifest:
    """Store ``payloads`` as chunks and return a manifest of one file over them."""
    refs = [ChunkRef(hash=store.put_chunk(p), size=len(p)) for p in payloads]
    blob = b"".join(payloads)
    entry = FileEntry(
        path="index.faiss",
        file_type="faiss",
        size=len(blob),
        file_sha256=_chunk_hash(blob),
        chunks=refs,
    )
    return IndexManifest(bundle_id="b", version="1.0.0", files=[entry])


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
    m1 = _manifest_for(store, b"v1" * 16)
    p1 = _promote_manifest(store, m1)
    assert store.read_active() == p1
    m2 = _manifest_for(store, b"v2" * 16)
    p2 = _promote_manifest(store, m2)
    assert store.read_active() == p2


def test_promote_refuses_rollback_to_older_version(tmp_path: Path) -> None:
    # Anti-rollback: once a NEWER version is active, a validly-signed but OLDER pointer
    # must be refused — an attacker replaying a stale `/latest` cannot downgrade a client.
    from edgeproc.bundles.cas import RollbackError  # noqa: PLC0415

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


def test_promote_allows_equal_and_forward_versions(tmp_path: Path) -> None:
    # The guard must reject ONLY a provable downgrade: an equal-version re-publish and a
    # forward bump are both legitimate and must still promote (covenant: never reject valid).
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
    store.promote(p_b)  # equal version, different content → allowed
    assert store.read_active() == p_b
    store.promote(p_c)  # forward bump → allowed
    assert store.read_active() == p_c


def test_promote_allows_unparseable_version_covenant(tmp_path: Path) -> None:
    # Covenant: the anti-rollback guard must NEVER reject a validly-signed bundle. When a
    # version string is not PEP 440, there is nothing to compare, so a downgrade cannot be
    # PROVEN — the promote must still succeed (fail-OPEN), never fail-closed on the guard.
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
    store.promote(weird_ptr)  # unparseable version → cannot prove downgrade → allowed
    assert store.read_active() == weird_ptr


def test_promote_crash_safety_keeps_old_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    m1 = _manifest_for(store, b"v1" * 16)
    p1 = _promote_manifest(store, m1)
    # Stage v2 fully (chunks + manifest) BEFORE the simulated crash — only the
    # active-pointer swap should fail, exactly as a real crash mid-promote would.
    m2 = _manifest_for(store, b"v2" * 16)
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
