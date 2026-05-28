"""Producer: build a signed, content-addressed origin a device can ``sync_index``.

The counterpart to ``sync_index``: chunk each file (Gear-CDC), store each chunk in
a content-addressed store, record an ordered ``IndexManifest``, sign a tiny
``VersionPointer`` over the manifest's content hash, and lay the bytes out under a
flat origin dir matching the HTTP contract exactly — ``latest``, ``manifest/<hash>``,
``chunk/<hash>`` — so a ``FilesystemAdapter`` (or ``python -m http.server`` / Caddy)
serves it verbatim and a fresh device pulls it back byte-for-byte.

``store`` is the producer-side CAS: ``put_chunk`` dedupes + content-addresses every
chunk, ``put_manifest`` does the same for the manifest. The flat origin is then laid
out beside the store in the store's root so the origin dir IS what the CDN serves.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from edgeproc.bundles.manifest import (
    ChunkRef,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    manifest_digest,
)

if TYPE_CHECKING:
    from edgeproc.bundles.cas import FilesystemCacheStore
    from edgeproc.bundles.chunking import GearCDC
    from edgeproc.bundles.signing import Signer


def build_bundle(
    *,
    files: Mapping[str, bytes],
    store: FilesystemCacheStore,
    chunker: GearCDC,
    signer: Signer,
    bundle_id: str,
    version: str,
) -> VersionPointer:
    """Chunk + store ``files``, sign the manifest pointer, lay out the flat origin."""
    entries = [_file_entry(path, data, chunker, store) for path, data in files.items()]
    manifest = IndexManifest(bundle_id=bundle_id, version=version, files=entries)
    store.put_manifest(canonical_bytes(manifest))
    pointer = _sign_pointer(manifest_digest(manifest), version, signer)
    _lay_out_origin(store, manifest, pointer)
    return pointer


def _file_entry(
    path: str, plaintext: bytes, chunker: GearCDC, store: FilesystemCacheStore
) -> FileEntry:
    """Chunk one file, store each chunk, and record its ordered chunk refs."""
    refs = [ChunkRef(hash=store.put_chunk(c), size=len(c)) for c in chunker.chunk(plaintext)]
    return FileEntry(
        path=path,
        file_type=None,
        size=len(plaintext),
        file_sha256=hashlib.sha256(plaintext).hexdigest(),
        chunks=refs,
    )


def _sign_pointer(digest: str, version: str, signer: Signer) -> VersionPointer:
    """Sign the canonical (signature-excluded) pointer bytes; return the signed pointer."""
    unsigned = VersionPointer(manifest_hash=digest, version=version, signature="")
    signature = signer.sign(canonical_bytes(unsigned, exclude={"signature"}))
    return VersionPointer(manifest_hash=digest, version=version, signature=signature)


def _lay_out_origin(
    store: FilesystemCacheStore, manifest: IndexManifest, pointer: VersionPointer
) -> None:
    """Write the flat ``chunk/`` + ``manifest/`` + ``latest`` the CDN serves verbatim.

    For each chunk referenced by the new manifest, hardlink the store's compressed
    chunk file into ``chunk/<hash>`` if it isn't already there. A one-byte edit to a
    big file re-publishes O(changed) files, not O(all chunks). ``os.link`` falls back
    to a copy on cross-filesystem / unsupported-link errors.
    """
    root = store.root
    (root / "chunk").mkdir(parents=True, exist_ok=True)
    (root / "manifest").mkdir(parents=True, exist_ok=True)
    (root / "manifest" / pointer.manifest_hash).write_bytes(canonical_bytes(manifest))
    wanted = {ref.hash for entry in manifest.files for ref in entry.chunks}
    for chunk_hash in wanted:
        dst = root / "chunk" / chunk_hash
        if dst.exists():
            continue
        _link_or_copy(_store_chunk_path(store, chunk_hash), dst)
    (root / "latest").write_bytes(pointer.model_dump_json().encode("utf-8"))


def _store_chunk_path(store: FilesystemCacheStore, chunk_hash: str) -> Path:
    """The store-internal compressed chunk file (``chunks/<aa>/<hash>``)."""
    return store.root / "chunks" / chunk_hash[:2] / chunk_hash


def _link_or_copy(src: Path, dst: Path) -> None:
    """Hardlink ``src`` → ``dst``; fall back to copy on cross-fs / unsupported errors."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
