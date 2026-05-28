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
    canonical_bytes,
)
from edgeproc.bundles.signing import Verifier

log = structlog.get_logger(__name__)


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
    """Fetch ``/latest`` and verify its detached signature (fail-closed)."""
    pointer = VersionPointer.model_validate_json(adapter.fetch_bytes(base_url + "/latest"))
    verifier.verify(canonical_bytes(pointer, exclude={"signature"}), pointer.signature)
    return pointer


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
    wanted = {ref.hash for entry in manifest.files for ref in entry.chunks}
    missing = frozenset(h for h in wanted if not store.has_chunk(h))
    return MissingChunks(to_fetch=missing, reused=len(wanted) - len(missing))


def _fetch_missing(
    base_url: str, missing: frozenset[str], adapter: FetchAdapter, store: CacheStore
) -> int:
    """Fetch + verbatim-ingest each missing chunk (fail-closed); return bytes fetched."""
    fetched = 0
    for chunk_hash in missing:
        compressed = adapter.fetch_bytes(base_url + "/chunk/" + chunk_hash)
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
    *, base_url: str, store: CacheStore, adapter: FetchAdapter, verifier: Verifier
) -> SyncResult:
    """Pull a signed pointer, diff + fetch missing chunks, verify, atomically swap."""
    pointer = _fetch_pointer(base_url, adapter, verifier)
    manifest = _fetch_manifest(base_url, pointer, adapter, store)
    diff = _missing_chunks(manifest, store)
    bytes_fetched = _fetch_missing(base_url, diff.to_fetch, adapter, store)
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
