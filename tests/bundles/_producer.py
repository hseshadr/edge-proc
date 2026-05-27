"""Test-only origin producer: builds a signed, content-addressed origin to sync from.

A real shipped ``build_bundle`` / ``edgeproc publish`` is a deliberate LATER step
(it owns CLI surface, key management, and incremental re-publish). This helper is
the minimum needed to PRODUCE an origin so the consumer (``sync_index``) can be
tested end-to-end: chunk each file, store zstd chunks in a producer-side CAS, then
lay the bytes out in a flat ``origin`` dir that matches the HTTP contract's URL
scheme exactly — ``latest``, ``manifest/<hash>``, ``chunk/<hash>`` — so a
``FilesystemAdapter`` (or ``python -m http.server``) can serve it verbatim.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from edgeproc.bundles.cas import FilesystemCacheStore
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import (
    ChunkRef,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    manifest_digest,
)
from edgeproc.bundles.signing import Ed25519Signer


def _file_entry(
    path: str, plaintext: bytes, chunker: GearCDC, store: FilesystemCacheStore
) -> FileEntry:
    refs = [ChunkRef(hash=store.put_chunk(c), size=len(c)) for c in chunker.chunk(plaintext)]
    return FileEntry(
        path=path,
        file_type=None,
        size=len(plaintext),
        file_sha256=hashlib.sha256(plaintext).hexdigest(),
        chunks=refs,
    )


def build_origin(
    *,
    files: dict[str, bytes],
    origin: Path,
    chunker: GearCDC,
    signer: Ed25519Signer,
    version: str = "1.0.0",
    bundle_id: str = "b",
) -> VersionPointer:
    """Chunk + lay out ``files`` under ``origin`` and return the signed pointer."""
    store = FilesystemCacheStore(origin / "_producer_cas")
    entries = [_file_entry(p, data, chunker, store) for p, data in files.items()]
    manifest = IndexManifest(bundle_id=bundle_id, version=version, files=entries)
    digest = manifest_digest(manifest)
    _lay_out(origin, manifest, digest, store)
    return _sign_and_publish(origin, digest, version, signer)


def _lay_out(
    origin: Path, manifest: IndexManifest, digest: str, store: FilesystemCacheStore
) -> None:
    (origin / "chunk").mkdir(parents=True, exist_ok=True)
    (origin / "manifest").mkdir(parents=True, exist_ok=True)
    (origin / "manifest" / digest).write_bytes(canonical_bytes(manifest))
    for entry in manifest.files:
        for ref in entry.chunks:
            src = store._chunk_path(ref.hash)
            (origin / "chunk" / ref.hash).write_bytes(src.read_bytes())


def _sign_and_publish(
    origin: Path, digest: str, version: str, signer: Ed25519Signer
) -> VersionPointer:
    unsigned = VersionPointer(manifest_hash=digest, version=version, signature="")
    signature = signer.sign(canonical_bytes(unsigned, exclude={"signature"}))
    pointer = VersionPointer(manifest_hash=digest, version=version, signature=signature)
    write_latest(origin, pointer)
    return pointer


def write_latest(origin: Path, pointer: VersionPointer) -> None:
    """(Re)write the origin's mutable ``latest`` pointer object."""
    (origin / "latest").write_bytes(pointer.model_dump_json().encode("utf-8"))
