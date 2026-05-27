"""Sync a bundle from an origin into a local cache, verifying every file.

Lifted from edge-reco's ``sync_catalog`` and generalised over ``FetchAdapter``.
Fails closed: a checksum mismatch raises rather than caching corrupt bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

import structlog
from pydantic import BaseModel

from edgeproc.bundles.adapters import FetchAdapter
from edgeproc.bundles.cas import CacheStore, IntegrityError
from edgeproc.bundles.manifest import (
    BundleFile,
    BundleManifest,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    validate_checksum,
)
from edgeproc.bundles.signing import Verifier

log = structlog.get_logger(__name__)

_MANIFEST_FILE: Final[str] = "manifest.json"


def sync_bundle(
    *,
    manifest_url: str,
    cache_dir: Path,
    adapter: FetchAdapter,
    file_base_url: str,
) -> BundleManifest:
    """Fetch the manifest, download + verify each file, then cache the manifest."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    log.info("fetching manifest", url=manifest_url)
    manifest = adapter.fetch_manifest(manifest_url)
    for entry in manifest.files:
        _fetch_and_verify(adapter, file_base_url, entry, cache_dir)
    (cache_dir / _MANIFEST_FILE).write_text(manifest.model_dump_json(indent=2))
    log.info("sync complete", bundle_id=manifest.bundle_id, version=manifest.version)
    return manifest


def _fetch_and_verify(
    adapter: FetchAdapter,
    file_base_url: str,
    entry: BundleFile,
    cache_dir: Path,
) -> None:
    local_path = cache_dir / entry.path
    log.info("downloading", path=entry.path, local=str(local_path))
    adapter.fetch_file(file_base_url, entry.path, local_path)
    if not validate_checksum(local_path, entry.checksum):
        raise ValueError(f"checksum validation failed for {entry.path}")


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


def _missing_chunks(manifest: IndexManifest, store: CacheStore) -> tuple[set[str], int]:
    """Return (chunks to fetch, reused count) over the manifest's deduped chunk set."""
    wanted = {ref.hash for entry in manifest.files for ref in entry.chunks}
    missing = {h for h in wanted if not store.has_chunk(h)}
    return missing, len(wanted) - len(missing)


def _fetch_missing(
    base_url: str, missing: set[str], adapter: FetchAdapter, store: CacheStore
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
    missing, reused = _missing_chunks(manifest, store)
    bytes_fetched = _fetch_missing(base_url, missing, adapter, store)
    _verify_reassembly(manifest, store)
    store.promote(pointer)
    return SyncResult(
        version=pointer.version,
        manifest_hash=pointer.manifest_hash,
        chunks_fetched=len(missing),
        chunks_reused=reused,
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
