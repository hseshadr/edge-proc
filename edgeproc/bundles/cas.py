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
- **Proved-fresher promote** — replacing the active pointer requires PROOF that the
  incoming one is fresher: a strictly-greater monotonic ``sequence``, or a strictly-greater
  PEP 440 ``version``. If neither comparison can speak — an equal or unparseable version
  and no counter — the promote is refused. Absence of disproof is not proof. (Re-promoting
  the byte-identical active pointer is a no-op write, so it needs no proof.)
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
from enum import Enum, auto
from pathlib import Path
from typing import ClassVar, Final, Protocol, runtime_checkable

import zstandard
from filelock import FileLock, Timeout
from packaging.version import InvalidVersion, Version
from pydantic import ValidationError

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
    """A promote could not prove it is at least as fresh as the active pointer (fail-closed).

    Raised for a provable downgrade (older version, non-increasing sequence) AND for a
    pointer that proves nothing at all — an unparseable version with no monotonic counter.
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
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_directory(target.parent)
    finally:
        tmp.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Persist a rename/unlink in ``path`` instead of only flushing file contents."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_mkdir(path: Path) -> None:
    """Create every missing component and durably link it from its parent."""
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _fsync_directory(directory.parent)


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
        _durable_mkdir(self._root)
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
        _durable_mkdir(self._root / name)
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
        _durable_mkdir(path.parent)
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
        _durable_mkdir(path.parent)
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
        """The active pointer, or ``None`` only when nothing has ever been promoted.

        ``None`` is the anti-rollback guard's "nothing to be fresher than" signal, so it is
        reserved for a store with NO ``active`` entry. An ``active`` that exists but cannot
        be read is a catalogued refusal — answering "nothing is active" there would SKIP
        the guard and let any pointer promote without proof.
        """
        active = self._store_path("active")
        if not active.exists():
            return None
        return _parse_active(active)

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
        """Refuse a promote that cannot PROVE it is at least as fresh as the active one.

        Anti-rollback/anti-replay: a validly-signed but stale pointer (a replayed old
        ``/latest``) must not downgrade a client that already promoted a newer version.
        Fail-closed — proof is required, not merely the absence of disproof. A first
        promote has nothing to be fresher than and is always allowed.
        """
        active = self.read_active()
        if active is None:
            return
        reason = _downgrade_reason(pointer, active)
        if reason is not None:
            raise RollbackError(reason)

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


def _parse_active(active: Path) -> VersionPointer:
    """Read ``active`` as a pointer, or refuse fail-closed with a catalogued error.

    Anything unreadable lands here — malformed JSON, a schema violation, a path that is a
    directory rather than a file. All of it becomes :class:`IntegrityError`, so a consumer's
    existing ``except IntegrityError`` handler catches it instead of a raw pydantic error
    escaping the trust boundary.
    """
    try:
        return VersionPointer.model_validate_json(active.read_bytes())
    except (OSError, ValidationError) as exc:
        raise IntegrityError("active pointer exists but could not be read") from exc


class _Freshness(Enum):
    """What ONE comparison proved: fresh enough, provably stale, or nothing at all."""

    FRESH = auto()
    STALE = auto()
    UNDECIDABLE = auto()


_STALE_SEQUENCE: Final = "refusing rollback: sequence is not fresher than the active pointer's"
_STALE_VERSION: Final = "refusing rollback: version is older (PEP 440) than the active pointer's"
_UNPROVABLE: Final = (
    "refusing rollback: version is not strictly greater (equal, or not PEP 440) "
    "and no monotonic sequence proves freshness"
)


def _downgrade_reason(incoming: VersionPointer, active: VersionPointer) -> str | None:
    """Why ``incoming`` may not replace ``active`` — or ``None`` when it provably may.

    Re-promoting the byte-identical active pointer is idempotent (a no-op write), not
    equivocation, so it is the one case that needs no proof.
    """
    if incoming == active:
        return None
    return _freshness_refusal(
        _sequence_verdict(incoming, active),
        _version_verdict(incoming.version, active.version),
    )


def _freshness_refusal(sequence: _Freshness, version: _Freshness) -> str | None:
    """Combine the two verdicts FAIL-CLOSED: either one stale refuses, and so does no proof.

    The rule the whole guard reduces to: a promote must be PROVED fresher than the active
    pointer. Two comparisons can supply that proof — a strictly-greater monotonic counter,
    or a strictly-greater PEP 440 version. Neither speaking is a refusal, not a pass.
    """
    if sequence is _Freshness.STALE:
        return _STALE_SEQUENCE
    if version is _Freshness.STALE:
        return _STALE_VERSION
    if _Freshness.FRESH in (sequence, version):
        return None
    return _UNPROVABLE


def _sequence_verdict(incoming: VersionPointer, active: VersionPointer) -> _Freshness:
    """Monotonic-counter verdict, delegated to :func:`is_fresh_sequence`.

    There is exactly ONE implementation of "is this sequence fresher", so the public
    predicate can never drift from what promotion actually enforces. An ``active`` with no
    counter has no counter state to roll back to — undecidable, so the version decides.
    """
    if active.sequence is None:
        return _Freshness.UNDECIDABLE
    return _Freshness.FRESH if is_fresh_sequence(incoming, active) else _Freshness.STALE


def _version_verdict(incoming: str, active: str) -> _Freshness:
    """PEP 440 verdict; only a STRICTLY GREATER version proves freshness (fail-closed).

    Two absences of proof are folded in here, both of which used to read as ``FRESH``:

    - ``InvalidVersion`` — a version either side cannot parse. Swallowing it into "not a
      downgrade" handed every date-versioned publisher a rollback bypass.
    - An EQUAL version — ``Version(a) < Version(b)`` is ``False`` for equal versions, and
      that single ``False`` was taken as affirmative proof. Two different bundles can wear
      one label, so an equal version says nothing about which is newer: it is a replay
      window, not a pass. An equal-version re-publish proves itself with ``sequence``.

    An absence of proof on BOTH comparisons refuses the promote.
    """
    try:
        incoming_version, active_version = Version(incoming), Version(active)
    except InvalidVersion:
        return _Freshness.UNDECIDABLE
    if incoming_version < active_version:
        return _Freshness.STALE
    if incoming_version == active_version:
        return _Freshness.UNDECIDABLE
    return _Freshness.FRESH


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
