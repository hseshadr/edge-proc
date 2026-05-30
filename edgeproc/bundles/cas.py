"""Content-addressed store: zstd-compressed chunks + atomic pointer swap + GC.

The store IS the project's integrity and crash-safety boundary, so its rules are
pinned and fail-closed by construction:

- **Hash-over-plaintext** — a chunk's name is ``sha256(plaintext)``; the file on
  disk holds ``zstd(plaintext)``. The read path is always decompress → re-hash →
  compare to the name; any mismatch (tamper, truncation, a non-zstd file) raises
  :class:`IntegrityError`. A stored file can never lie about its content.
- **Atomic promote** — the active pointer is published with ``os.replace`` of a
  pre-``fsync``ed temp file on the SAME filesystem. ``os.replace`` is atomic on
  POSIX and Windows, so a concurrent reader (or a crash mid-swap) sees the OLD
  pointer or the NEW one, never a torn/empty ``active``. (Pinned: not
  ``renameat2``, not symlinks.)
- **Mark-sweep GC** — the reachable set is the active manifest plus every chunk
  it references; everything else is swept. With nothing promoted, ``gc`` is a
  fail-safe no-op (returning 0), never a wipe.

zstd compression and the atomic swap are storage internals of this module by
design — not their own modules. The ``CacheStore`` Protocol is the seam an
``OPFSCacheStore`` (Phase C, browser) fills with zero consumer change.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

import zstandard

from edgeproc.bundles.manifest import IndexManifest, VersionPointer, canonical_bytes


class IntegrityError(Exception):
    """A stored object failed its content-address / decompress check (fail-closed)."""


@runtime_checkable
class CacheStore(Protocol):
    """Local content-addressed store (``FilesystemCacheStore``; OPFS in Phase C)."""

    def has_chunk(self, chunk_hash: str) -> bool: ...
    def put_chunk(self, plaintext: bytes) -> str: ...
    def put_chunk_compressed(self, chunk_hash: str, compressed: bytes) -> None: ...
    def get_chunk(self, chunk_hash: str) -> bytes: ...
    def put_manifest(self, manifest_bytes: bytes) -> str: ...
    def get_manifest(self, manifest_hash: str) -> bytes: ...
    def read_active(self) -> VersionPointer | None: ...
    def promote(self, pointer: VersionPointer) -> None: ...
    def gc(self) -> int: ...


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically via a fsynced same-dir temp + replace."""
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


class FilesystemCacheStore:
    """Filesystem ``CacheStore``: ``chunks/<aa>/<hash>``, ``manifests/<hash>``, ``active``."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # FROZEN CAS layout contract: a producer's origin dir and a consumer's cache both
        # address objects via these exact subdirs/names. Renaming any breaks existing stores.
        self._chunks = root / "chunks"
        self._manifests = root / "manifests"
        self._active = root / "active"
        self._chunks.mkdir(parents=True, exist_ok=True)
        self._manifests.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """The store's root dir — also the origin dir a producer lays ``latest`` into."""
        return self._root

    def _chunk_path(self, chunk_hash: str) -> Path:
        return self._chunks / chunk_hash[:2] / chunk_hash

    def has_chunk(self, chunk_hash: str) -> bool:
        return self._chunk_path(chunk_hash).is_file()

    def put_chunk(self, plaintext: bytes) -> str:
        chunk_hash = _sha256(plaintext)
        path = self._chunk_path(chunk_hash)
        if path.is_file():
            return chunk_hash  # idempotent: content-addressed, never rewritten
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, zstandard.compress(plaintext))
        return chunk_hash

    def put_chunk_compressed(self, chunk_hash: str, compressed: bytes) -> None:
        """Store the producer's verbatim zstd bytes, then verify fail-closed.

        The consumer ingests fetched chunks WITHOUT re-compressing (the origin
        serves the producer's exact zstd file). Integrity is re-checked on-device:
        decompress and confirm ``sha256(plaintext) == chunk_hash``, else remove the
        bad file and raise :class:`IntegrityError`.
        """
        path = self._chunk_path(chunk_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, compressed)
        self._verify_or_remove(path, chunk_hash)

    def _verify_or_remove(self, path: Path, chunk_hash: str) -> None:
        try:
            if _sha256(_decompress(path.read_bytes())) != chunk_hash:
                raise IntegrityError(f"fetched chunk {chunk_hash} failed content-address check")
        except IntegrityError:
            path.unlink(missing_ok=True)
            raise

    def get_chunk(self, chunk_hash: str) -> bytes:
        plaintext = _decompress(self._chunk_path(chunk_hash).read_bytes())
        if _sha256(plaintext) != chunk_hash:
            raise IntegrityError(f"chunk {chunk_hash} failed content-address check")
        return plaintext

    def put_manifest(self, manifest_bytes: bytes) -> str:
        manifest_hash = _sha256(manifest_bytes)
        _atomic_write(self._manifests / manifest_hash, manifest_bytes)
        return manifest_hash

    def get_manifest(self, manifest_hash: str) -> bytes:
        raw = (self._manifests / manifest_hash).read_bytes()
        if _sha256(raw) != manifest_hash:
            raise IntegrityError(f"manifest {manifest_hash} failed content-address check")
        return raw

    def read_active(self) -> VersionPointer | None:
        if not self._active.is_file():
            return None
        return VersionPointer.model_validate_json(self._active.read_bytes())

    def promote(self, pointer: VersionPointer) -> None:
        _atomic_write(self._active, pointer.model_dump_json().encode("utf-8"))

    def gc(self) -> int:
        active = self.read_active()
        if active is None:
            return 0  # fail-safe: never wipe a store with no promoted version
        manifest = self._load_manifest(active.manifest_hash)
        keep_chunks = {ref.hash for entry in manifest.files for ref in entry.chunks}
        removed = self._sweep_chunks(keep_chunks)
        return removed + self._sweep_manifests(active.manifest_hash)

    def _load_manifest(self, manifest_hash: str) -> IndexManifest:
        manifest = IndexManifest.model_validate_json(self.get_manifest(manifest_hash))
        if _sha256(canonical_bytes(manifest)) != manifest_hash:
            raise IntegrityError(f"active manifest {manifest_hash} is not canonical")
        return manifest

    def _sweep_chunks(self, keep: set[str]) -> int:
        removed = 0
        for path in self._chunks.glob("*/*"):
            if path.is_file() and path.name not in keep:
                path.unlink()
                removed += 1
        return removed

    def _sweep_manifests(self, keep: str) -> int:
        removed = 0
        for path in self._manifests.iterdir():
            if path.is_file() and path.name != keep:
                path.unlink()
                removed += 1
        return removed


def _decompress(stored: bytes) -> bytes:
    try:
        return zstandard.decompress(stored)
    except zstandard.ZstdError as exc:
        raise IntegrityError("stored chunk failed to decompress") from exc
