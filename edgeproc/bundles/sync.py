"""Sync a v2 signed, chunked bundle from an origin into a local cache.

The substrate is fail-closed at every layer: the version pointer must verify
under the pinned ed25519 key, the manifest must content-address to the pointer,
each chunk is verbatim-ingested into the CAS (which hashes on write), and the
final reassembly check proves every file's chunks concat to its declared sha256.
"""

from __future__ import annotations

import hashlib
from typing import NamedTuple

import structlog
from pydantic import BaseModel

from edgeproc.bundles.adapters import FetchAdapter
from edgeproc.bundles.cas import CacheStore, IntegrityError
from edgeproc.bundles.manifest import (
    FileEntry,
    IndexManifest,
    VersionPointer,
    pointer_signing_bytes,
)
from edgeproc.bundles.signing import Verifier
from edgeproc.core.settings import EdgeProcSettings

log = structlog.get_logger(__name__)


class SyncCapError(IntegrityError):
    """A sync would pull past its aggregate byte/file ceiling (fail-closed).

    Subclasses :class:`IntegrityError` — busting the resource ceiling is a trust-boundary
    refusal, so every existing ``IntegrityError`` handler already stops the sync.
    """


class SyncCaps(NamedTuple):
    """Resolved aggregate ceilings for one sync run."""

    total_bytes: int
    max_files: int


def _resolve_caps(max_total_bytes: int | None, max_files: int | None) -> SyncCaps:
    """Fill unset caps from ``EdgeProcSettings`` (generous defaults; env-overridable)."""
    settings = EdgeProcSettings()
    return SyncCaps(
        total_bytes=(
            max_total_bytes if max_total_bytes is not None else settings.max_sync_total_bytes
        ),
        max_files=max_files if max_files is not None else settings.max_sync_files,
    )


def _enforce_file_cap(manifest: IndexManifest, max_files: int) -> None:
    """Fail closed BEFORE any fetch if the manifest enumerates more files than the cap."""
    if len(manifest.files) > max_files:
        raise SyncCapError(f"manifest declares {len(manifest.files)} files > cap {max_files}")


class SyncResult(BaseModel):
    """Outcome of a ``sync_index`` run — proves only-changed-chunks were fetched."""

    version: str
    manifest_hash: str
    chunks_fetched: int
    chunks_reused: int
    bytes_fetched: int


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch_pointer(base_url: str, adapter: FetchAdapter, verifier: Verifier) -> VersionPointer:
    """Fetch ``/latest`` and verify its detached signature (fail-closed).

    The verified preimage is :func:`pointer_signing_bytes`, so a legacy pointer (no
    identity fields) verifies against its original signature unchanged, while a pointer
    that binds a bundle_id/channel/sequence is authenticated together with that identity.
    """
    pointer = VersionPointer.model_validate_json(adapter.fetch_bytes(base_url + "/latest"))
    verifier.verify(pointer_signing_bytes(pointer), pointer.signature)
    return pointer


def _check_identity(
    pointer: VersionPointer, expected_bundle_id: str | None, expected_channel: str | None
) -> None:
    """Fail-closed identity pin (opt-in): refuse a pointer bound to another bundle/channel.

    A caller that pins nothing gets today's behavior. When an expectation IS set, a validly
    signed pointer minted for a DIFFERENT bundle/channel — a cross-bundle replay under a
    shared signing key + transport compromise — is refused before any promote.
    """
    if expected_bundle_id is not None and pointer.bundle_id != expected_bundle_id:
        raise IntegrityError(f"pointer bundle_id {pointer.bundle_id!r} != expected")
    if expected_channel is not None and pointer.channel != expected_channel:
        raise IntegrityError(f"pointer channel {pointer.channel!r} != expected")


def _check_manifest_identity(pointer: VersionPointer, manifest: IndexManifest) -> None:
    """When the pointer BINDS a bundle_id, the manifest it names must declare the same one.

    Closes the forge gap: a pointer claiming bundle_id ``A`` that points at a manifest
    declaring ``B`` (a crafted pointer under a shared key) is refused, so the pinned
    identity is sound rather than a self-asserted label.
    """
    if pointer.bundle_id is not None and manifest.bundle_id != pointer.bundle_id:
        raise IntegrityError(f"manifest bundle_id {manifest.bundle_id!r} != pointer")


def _fetch_manifest(
    base_url: str, pointer: VersionPointer, adapter: FetchAdapter, store: CacheStore
) -> IndexManifest:
    """Fetch the manifest, verify it hashes to the pointer, parse + cache it."""
    raw = adapter.fetch_bytes(base_url + "/manifest/" + pointer.manifest_hash)
    if _sha256(raw) != pointer.manifest_hash:
        raise IntegrityError(f"manifest {pointer.manifest_hash} failed content-address check")
    store.put_manifest(raw)
    return IndexManifest.model_validate_json(raw)


class MissingChunks(NamedTuple):
    """Diff of the manifest's chunk set against the local cache."""

    to_fetch: frozenset[str]
    reused: int


def _missing_chunks(manifest: IndexManifest, store: CacheStore) -> MissingChunks:
    """Return chunks to fetch + reused count over the manifest's deduped chunk set."""
    all_chunk_hashes = {ref.hash for entry in manifest.files for ref in entry.chunks}
    missing = frozenset(h for h in all_chunk_hashes if not store.has_chunk(h))
    return MissingChunks(to_fetch=missing, reused=len(all_chunk_hashes) - len(missing))


def _fetch_missing(
    base_url: str,
    missing: frozenset[str],
    adapter: FetchAdapter,
    store: CacheStore,
    max_total_bytes: int,
) -> int:
    """Fetch + verbatim-ingest each missing chunk (fail-closed); return bytes fetched.

    Enforces the AGGREGATE ceiling as a running total: the chunk that would push the sync
    past ``max_total_bytes`` is refused BEFORE it is written, so a manifest enumerating
    unbounded chunks can never exhaust disk. Bounds fetch count too — each chunk adds
    bytes, so a tight ceiling caps how many are pulled.
    """
    fetched = 0
    for chunk_hash in missing:
        compressed = adapter.fetch_bytes(base_url + "/chunk/" + chunk_hash)
        if fetched + len(compressed) > max_total_bytes:
            raise SyncCapError(f"sync exceeded {max_total_bytes}-byte aggregate cap")
        store.put_chunk_compressed(chunk_hash, compressed)
        fetched += len(compressed)
    return fetched


def _verify_reassembly(manifest: IndexManifest, store: CacheStore) -> None:
    """Reassembly-on-read check: each file's chunks concat to its ``file_sha256``."""
    for entry in manifest.files:
        blob = b"".join(store.get_chunk(ref.hash) for ref in entry.chunks)
        if _sha256(blob) != entry.file_sha256:
            raise IntegrityError(f"file {entry.path} failed reassembly check")


def sync_index(
    *,
    base_url: str,
    store: CacheStore,
    adapter: FetchAdapter,
    verifier: Verifier,
    expected_bundle_id: str | None = None,
    expected_channel: str | None = None,
    max_total_bytes: int | None = None,
    max_files: int | None = None,
) -> SyncResult:
    """Pull a signed pointer, diff + fetch missing chunks, verify, atomically swap.

    ``expected_bundle_id``/``expected_channel`` (opt-in) pin the consumer to a bundle
    identity: a pointer bound to any other one is refused fail-closed. ``max_total_bytes``/
    ``max_files`` bound the aggregate a single sync will pull (disk-exhaustion defense);
    unset, they fall back to the generous ``EdgeProcSettings`` defaults.
    """
    caps = _resolve_caps(max_total_bytes, max_files)
    pointer = _fetch_pointer(base_url, adapter, verifier)
    _check_identity(pointer, expected_bundle_id, expected_channel)
    manifest = _fetch_manifest(base_url, pointer, adapter, store)
    _check_manifest_identity(pointer, manifest)
    _enforce_file_cap(manifest, caps.max_files)
    diff = _missing_chunks(manifest, store)
    bytes_fetched = _fetch_missing(base_url, diff.to_fetch, adapter, store, caps.total_bytes)
    _verify_reassembly(manifest, store)
    store.promote(pointer)
    return SyncResult(
        version=pointer.version,
        manifest_hash=pointer.manifest_hash,
        chunks_fetched=len(diff.to_fetch),
        chunks_reused=diff.reused,
        bytes_fetched=bytes_fetched,
    )


def materialize_file(store: CacheStore, manifest: IndexManifest, path: str) -> bytes:
    """Reassemble a synced file's bytes on demand from its chunks (fail-closed)."""
    entry = _file_entry(manifest, path)
    blob = b"".join(store.get_chunk(ref.hash) for ref in entry.chunks)
    if _sha256(blob) != entry.file_sha256:
        raise IntegrityError(f"file {path} failed reassembly check")
    return blob


def _file_entry(manifest: IndexManifest, path: str) -> FileEntry:
    for entry in manifest.files:
        if entry.path == path:
            return entry
    raise KeyError(path)
