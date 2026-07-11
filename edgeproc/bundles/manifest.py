"""Bundle manifest models — v2 chunked, content-addressed, signed by version pointer.

A bundle is a versioned set of content-addressed files split into deduped chunks.
The :class:`VersionPointer` is the *only* signed object; it names the
:class:`IndexManifest` by its content hash, and the manifest names each chunk by
content hash. Tampering with any layer fails its hash or signature check.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from edgeproc.bundles.containment import ensure_safe_relpath

# Mirrors shared-libs' convention: opaque metadata values are scalars, never `Any`.
Scalar = str | int | float | bool | None


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

    @field_validator("path")
    @classmethod
    def _reject_unsafe_path(cls, value: str) -> str:
        """Refuse traversal/absolute paths at the model boundary (fail-closed).

        The path is written to disk on materialize; a compromised or malformed
        origin must not be able to smuggle ``../`` or ``/abs`` past parsing.
        """
        return ensure_safe_relpath(value)


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

    CRITICAL: this serialization format is frozen by ``VersionPointer.manifest_hash``.
    Any change (key order, separators, encoding) re-hashes every manifest and breaks
    verification against all existing bundles — it must never change without a migration.
    """
    payload = model.model_dump(mode="json", exclude=exclude)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def manifest_digest(manifest: IndexManifest) -> str:
    """Bare hex sha256 of the manifest's canonical bytes (a ``VersionPointer`` target)."""
    return hashlib.sha256(canonical_bytes(manifest)).hexdigest()
