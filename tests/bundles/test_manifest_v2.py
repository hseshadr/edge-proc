"""v2 chunked manifest models + canonical serialization (Phase A wave 1).

Canonical bytes are the load-bearing input to hashing + signing: they must be
deterministic, byte-stable across equal models, and order-significant for chunk
lists. These tests pin that contract before the signing layer depends on it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from edgeproc.bundles.manifest import (
    ChunkRef,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    manifest_digest,
)


@pytest.mark.parametrize(
    "bad_path",
    [
        "../escape.txt",
        "a/b/../../../etc/passwd",
        "/etc/passwd",
        "a\\..\\evil",
        "",
    ],
)
def test_file_entry_rejects_unsafe_paths(bad_path: str) -> None:
    # A FileEntry whose path could escape its output root must never parse: this
    # stops a malformed/compromised origin's traversal path at the model boundary,
    # before any consumer joins it to a directory.
    with pytest.raises(ValidationError):
        FileEntry(path=bad_path, file_type=None, size=0, file_sha256="00" * 32, chunks=[])


def test_file_entry_accepts_plain_relative_path() -> None:
    entry = FileEntry(path="sub/dir/index.faiss", size=0, file_sha256="00" * 32, chunks=[])
    assert entry.path == "sub/dir/index.faiss"


def _manifest() -> IndexManifest:
    return IndexManifest(
        bundle_id="products-2026-05-27",
        version="2.0.0",
        files=[
            FileEntry(
                path="index.faiss",
                file_type="faiss",
                size=6,
                file_sha256="ab" * 32,
                chunks=[
                    ChunkRef(hash="aa" * 32, size=3),
                    ChunkRef(hash="bb" * 32, size=3),
                ],
            )
        ],
        metadata={"embedding_model": "all-MiniLM-L6-v2", "dim": 384, "ok": True},
    )


def test_index_manifest_round_trips() -> None:
    manifest = _manifest()
    reparsed = IndexManifest.model_validate_json(manifest.model_dump_json())
    assert reparsed == manifest
    assert reparsed.schema_version == 2
    assert reparsed.files[0].chunks[0].hash == "aa" * 32
    assert reparsed.metadata["dim"] == 384


def test_canonical_bytes_byte_stable_regardless_of_key_order() -> None:
    first = _manifest()
    # Same logical content; metadata dict built with a different key insertion order.
    second = IndexManifest(
        bundle_id="products-2026-05-27",
        version="2.0.0",
        files=[
            FileEntry(
                path="index.faiss",
                file_type="faiss",
                size=6,
                file_sha256="ab" * 32,
                chunks=[
                    ChunkRef(hash="aa" * 32, size=3),
                    ChunkRef(hash="bb" * 32, size=3),
                ],
            )
        ],
        metadata={"ok": True, "dim": 384, "embedding_model": "all-MiniLM-L6-v2"},
    )
    assert canonical_bytes(first) == canonical_bytes(second)
    assert manifest_digest(first) == manifest_digest(second)


def test_canonical_bytes_preserve_chunk_order() -> None:
    base = _manifest()
    swapped = base.model_copy(deep=True)
    swapped.files[0].chunks.reverse()
    assert canonical_bytes(base) != canonical_bytes(swapped)
    assert manifest_digest(base) != manifest_digest(swapped)


def test_manifest_digest_is_hex64_and_changes_with_content() -> None:
    manifest = _manifest()
    digest = manifest_digest(manifest)
    assert len(digest) == 64
    assert int(digest, 16) >= 0  # valid lowercase hex
    changed = manifest.model_copy(update={"version": "2.0.1"})
    assert manifest_digest(changed) != digest


def test_pointer_excluded_canonical_bytes_independent_of_signature() -> None:
    base = VersionPointer(manifest_hash="cd" * 32, version="2.0.0", signature="sigA")
    other = VersionPointer(manifest_hash="cd" * 32, version="2.0.0", signature="sigB")
    assert canonical_bytes(base, exclude={"signature"}) == canonical_bytes(
        other, exclude={"signature"}
    )
    # Without the exclusion the differing signature must show through.
    assert canonical_bytes(base) != canonical_bytes(other)


def test_models_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ChunkRef.model_validate({"hash": "aa" * 32, "size": 3, "rogue": 1})
    with pytest.raises(ValidationError):
        IndexManifest.model_validate(
            {
                "bundle_id": "b",
                "version": "2.0.0",
                "files": [],
                "unexpected": "x",
            }
        )
