"""Bundle manifest model + sha256 checksum validation.

Generalised from edge-reco's catalog manifest: a bundle is just a versioned set of
content-addressed files plus opaque ``metadata`` the consumer defines (e.g.
``embedding_model``). The signed / atomically-swapped ``BundleManager`` (Sigstore +
content-addressed storage) is roadmap; v0 ships the functional fetch + verify path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

_SHA256_PREFIX: Final[str] = "sha256:"

# Mirrors shared-libs' convention: opaque metadata values are scalars, never `Any`.
Scalar = str | int | float | bool | None


class BundleFile(BaseModel):
    """One content-addressed file in a bundle."""

    path: str
    checksum: str
    file_type: str | None = None
    rows: int | None = None


class BundleManifest(BaseModel):
    """A versioned bundle of files with consumer-defined metadata."""

    bundle_id: str
    version: str
    files: list[BundleFile]
    metadata: dict[str, str] = Field(default_factory=dict)


def parse_manifest(path: Path) -> BundleManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    return BundleManifest.model_validate(data)


def validate_checksum(file_path: Path, expected: str) -> bool:
    """True iff ``expected`` is ``sha256:<hex>`` and matches the file's digest."""
    if not expected.startswith(_SHA256_PREFIX):
        return False
    actual = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return actual == expected[len(_SHA256_PREFIX) :]


class ChunkRef(BaseModel):
    """One content-defined chunk: ``hash`` is the bare hex sha256 of its plaintext."""

    model_config = ConfigDict(extra="forbid")

    hash: str
    size: int  # uncompressed chunk length in bytes


class FileEntry(BaseModel):
    """A file as an ordered list of chunks (order = reassembly order)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    file_type: str | None = None
    size: int  # total uncompressed file length
    file_sha256: str  # bare hex sha256 of the whole reassembled file
    chunks: list[ChunkRef]


class IndexManifest(BaseModel):
    """v2 chunked manifest; authenticated by its content hash, not an embedded sig."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    bundle_id: str
    version: str
    files: list[FileEntry]
    metadata: dict[str, Scalar] = Field(default_factory=dict)


class VersionPointer(BaseModel):
    """Signed pointer to a manifest; ``signature`` is detached over the rest."""

    model_config = ConfigDict(extra="forbid")

    manifest_hash: str  # hex sha256 of the manifest's canonical bytes
    version: str
    signature: str  # ed25519 over canonical_bytes(self, exclude={"signature"})


def canonical_bytes(model: BaseModel, *, exclude: set[str] | None = None) -> bytes:
    """Deterministic, reproducible byte encoding — the exact bytes hashed/signed.

    ``sort_keys`` sorts dict keys recursively but preserves list order, so a
    ``FileEntry.chunks`` sequence keeps its reassembly order.
    """
    payload = model.model_dump(mode="json", exclude=exclude)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def manifest_digest(manifest: IndexManifest) -> str:
    """Bare hex sha256 of the manifest's canonical bytes (a ``VersionPointer`` target)."""
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()
