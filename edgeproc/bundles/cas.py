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
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable

import zstandard
from filelock import FileLock, Timeout
from packaging.version import InvalidVersion, Version

from edgeproc.bundles.containment import UnsafePathError, resolve_within
from edgeproc.bundles.manifest import (
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    is_fresh_sequence,
    validate_sha256_hex,
)
from edgeproc.core.settings import EdgeProcSettings
from edgeproc.errors import BUNDLE_INTEGRITY_FAILED


class IntegrityError(Exception):
    """A stored object failed its content-address / decompress check (fail-closed).

    Carries the canonical ``bundle.integrity_failed`` code (``edgeproc_core.errors``)
    so a consumer can render it to RFC 9457 via :func:`edgeproc.errors.problem_details_for`.
    The code is metadata only — the exception's type and message are unchanged, so every
    existing ``except IntegrityError`` handler behaves exactly as before.
    """

    code: ClassVar[str] = BUNDLE_INTEGRITY_FAILED


class RollbackError(IntegrityError):
    """A promote would downgrade the active pointer to an OLDER version (fail-closed).

    Subclasses :class:`IntegrityError` — an anti-rollback violation is a trust-boundary
    failure, so every existing ``IntegrityError`` handler already refuses it.
    """


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
    def mutation(self) -> AbstractContextManager[None]: ...
    def promote(self, pointer: VersionPointer) -> None: ...
    def gc(self) -> int: ...


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validated_digest(digest: str) -> str:
    try:
        return validate_sha256_hex(digest)
    except ValueError as exc:
        raise IntegrityError(str(exc)) from exc


def _open_exclusive_temp(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags, 0o600)
    except OSError as exc:
        raise IntegrityError("atomic write refused an existing or unsafe temp path") from exc


def _atomic_write(target: Path, data: bytes) -> None:
    """Write ``data`` to ``target`` atomically via a fsynced same-dir temp + replace."""
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    fd = _open_exclusive_temp(tmp)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


class FilesystemCacheStore:
    """Filesystem ``CacheStore``: ``chunks/<aa>/<hash>``, ``manifests/<hash>``, ``active``."""

    def __init__(
        self,
        root: Path,
        *,
        max_decompressed_bytes: int | None = None,
        mutation_lock_timeout: float | None = None,
    ) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        # FROZEN CAS layout contract: a producer's origin dir and a consumer's cache both
        # address objects via these exact subdirs/names. Renaming any breaks existing stores.
        self._prepare_directory("chunks")
        self._prepare_directory("manifests")
        # Decompression-bomb ceiling: a chunk that inflates past this is refused fail-closed.
        settings = EdgeProcSettings()
        self._max_decompressed_bytes = (
            max_decompressed_bytes
            if max_decompressed_bytes is not None
            else settings.max_decompressed_bytes
        )
        timeout = (
            mutation_lock_timeout
            if mutation_lock_timeout is not None
            else settings.mutation_lock_timeout
        )
        self._mutation_lock = FileLock(self._store_path(".mutation.lock"), timeout=timeout)

    @property
    def root(self) -> Path:
        """The store's root dir — also the origin dir a producer lays ``latest`` into."""
        return self._root

    def _store_path(self, relpath: str) -> Path:
        try:
            return resolve_within(self._root, relpath)
        except UnsafePathError as exc:
            raise IntegrityError(str(exc)) from exc

    def _prepare_directory(self, name: str) -> None:
        (self._root / name).mkdir(parents=True, exist_ok=True)
        self._store_path(name)

    def _chunk_path(self, chunk_hash: str) -> Path:
        digest = _validated_digest(chunk_hash)
        return self._store_path(f"chunks/{digest[:2]}/{digest}")

    def _manifest_path(self, manifest_hash: str) -> Path:
        digest = _validated_digest(manifest_hash)
        return self._store_path(f"manifests/{digest}")

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
            plaintext = _decompress(path.read_bytes(), self._max_decompressed_bytes)
            if _sha256(plaintext) != chunk_hash:
                raise IntegrityError(f"fetched chunk {chunk_hash} failed content-address check")
        except IntegrityError:
            path.unlink(missing_ok=True)
            raise

    def get_chunk(self, chunk_hash: str) -> bytes:
        plaintext = _decompress(
            self._chunk_path(chunk_hash).read_bytes(), self._max_decompressed_bytes
        )
        if _sha256(plaintext) != chunk_hash:
            raise IntegrityError(f"chunk {chunk_hash} failed content-address check")
        return plaintext

    def put_manifest(self, manifest_bytes: bytes) -> str:
        manifest_hash = _sha256(manifest_bytes)
        _atomic_write(self._manifest_path(manifest_hash), manifest_bytes)
        return manifest_hash

    def write_atomic(self, relative_path: str, data: bytes) -> None:
        """Atomically write a contained producer artifact under the store root."""
        target = self._store_path(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)

    def get_manifest(self, manifest_hash: str) -> bytes:
        raw = self._manifest_path(manifest_hash).read_bytes()
        if _sha256(raw) != manifest_hash:
            raise IntegrityError(f"manifest {manifest_hash} failed content-address check")
        return raw

    def read_active(self) -> VersionPointer | None:
        active = self._store_path("active")
        if not active.is_file():
            return None
        return VersionPointer.model_validate_json(active.read_bytes())

    @contextmanager
    def mutation(self) -> Iterator[None]:
        """Serialize cache mutations across threads and processes with a bounded wait."""
        try:
            with self._mutation_lock:
                yield
        except Timeout as exc:
            raise IntegrityError("filesystem mutation lock timed out; retry sync") from exc

    def promote(self, pointer: VersionPointer) -> None:
        with self.mutation():
            self._reject_rollback(pointer)
            _atomic_write(self._store_path("active"), pointer.model_dump_json().encode("utf-8"))

    def _reject_rollback(self, pointer: VersionPointer) -> None:
        """Refuse a promote whose version is provably OLDER than the active one.

        Anti-rollback: a validly-signed but stale pointer (a replayed old ``/latest``)
        must not downgrade a client that already promoted a newer version. Only a
        *provable* downgrade is refused; an equal/newer version, a first promote, or a
        version string neither side can parse is allowed — so no already-valid, signed
        bundle is ever rejected.
        """
        active = self.read_active()
        if active is not None and _is_downgrade(pointer, active):
            raise RollbackError("refusing rollback or reuse of an active monotonic sequence")

    def gc(self) -> int:
        with self.mutation():
            return self._gc_locked()

    def _gc_locked(self) -> int:
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
        for path in self._store_path("chunks").glob("*/*"):
            if path.is_file() and path.name not in keep:
                path.unlink()
                removed += 1
        return removed

    def _sweep_manifests(self, keep: str) -> int:
        removed = 0
        for path in self._store_path("manifests").iterdir():
            if path.is_file() and path.name != keep:
                path.unlink()
                removed += 1
        return removed


def _is_downgrade(incoming: VersionPointer, active: VersionPointer) -> bool:
    """True iff ``incoming`` is provably older than ``active`` — by sequence OR version.

    A lower monotonic ``sequence`` is a downgrade. An equal sequence is allowed only for
    an exact idempotent re-promote; different content at that sequence is equivocation.
    Legacy pointers still fall back to PEP 440, preserving their original behavior.
    """
    return _sequence_violation(incoming, active) or _version_downgrade(
        incoming.version, active.version
    )


def _sequence_violation(incoming: VersionPointer, active: VersionPointer) -> bool:
    """Reject a pointer that is not strictly fresher — except an identical re-promote.

    The counter comparison is :func:`is_fresh_sequence`, so there is exactly ONE
    implementation of "is this sequence fresher" and the public predicate can never
    drift from what promotion actually enforces. Promotion adds the one rule the pure
    predicate does not carry: re-promoting the byte-identical active pointer is
    idempotent, not equivocation. A legacy pointer (either counter unset) is
    undecidable, so ``is_fresh_sequence`` returns True and PEP 440 still decides.
    """
    return not is_fresh_sequence(incoming, active) and incoming != active


def _version_downgrade(incoming: str, active: str) -> bool:
    """True iff ``incoming`` is a provably-older PEP 440 version than ``active``."""
    try:
        return Version(incoming) < Version(active)
    except InvalidVersion:
        return False


def _decompress(stored: bytes, max_output_size: int) -> bytes:
    """Decompress a stored chunk, refusing a decompression bomb (fail-closed).

    Streams at most ``max_output_size`` bytes rather than trusting the frame's
    (attacker-controlled) content-size header, so a small file that inflates past the
    cap is rejected before it is ever materialized into memory.
    """
    decompressor = zstandard.ZstdDecompressor()
    try:
        with decompressor.stream_reader(stored) as reader:
            plaintext = reader.read(max_output_size + 1)
    except zstandard.ZstdError as exc:
        raise IntegrityError("stored chunk failed to decompress") from exc
    if len(plaintext) > max_output_size:
        raise IntegrityError("stored chunk exceeds max decompressed size")
    return plaintext
