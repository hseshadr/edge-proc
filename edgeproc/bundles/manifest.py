"""Bundle manifest models — v2 chunked, content-addressed, signed by version pointer.

A bundle is a versioned set of content-addressed files split into deduped chunks.
The :class:`VersionPointer` is the *only* signed object; it names the
:class:`IndexManifest` by its content hash, and the manifest names each chunk by
content hash. Tampering with any layer fails its hash or signature check.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

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
    """Signed pointer to a manifest; ``signature`` is detached over the rest.

    ``bundle_id``/``channel`` optionally BIND the signature to a bundle identity and
    release channel; ``sequence`` is an optional monotonic freshness counter. All three
    default ``None`` and are excluded from the signed preimage when unset (see
    :func:`pointer_signing_bytes`), so an already-signed legacy pointer — which carries
    none of them — verifies byte-for-byte and existing verification is unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    manifest_hash: str  # hex sha256 of the manifest's canonical bytes
    version: str
    bundle_id: str | None = None  # identity binding (optional; None ⇒ legacy preimage)
    channel: str | None = None  # release-channel binding (optional)
    sequence: int | None = None  # monotonic freshness counter (optional)
    signature: str  # ed25519 over pointer_signing_bytes(self)


# Identity/freshness fields added after v0. They are excluded from the signing preimage
# whenever they are unset, so a pointer carrying none of them hashes IDENTICALLY to the
# legacy {manifest_hash, version} bytes — every already-signed pointer still verifies.
_POINTER_OPTIONAL_FIELDS: Final = ("bundle_id", "channel", "sequence")


def pointer_signing_bytes(pointer: VersionPointer) -> bytes:
    """The exact bytes signed/verified for ``pointer`` (backward-compatible).

    Excludes ``signature`` plus any identity/freshness field left ``None``. A pointer that
    binds no identity therefore produces the byte-identical legacy preimage; a field that
    IS set is folded in, binding the signature to that bundle / channel / sequence.
    """
    exclude = {"signature"}
    exclude.update(f for f in _POINTER_OPTIONAL_FIELDS if getattr(pointer, f) is None)
    return canonical_bytes(pointer, exclude=exclude)


def is_fresh_sequence(incoming: VersionPointer, active: VersionPointer) -> bool:
    """Freshness predicate for a downstream anti-replay guard (monotonic ``sequence``).

    True only when ``incoming.sequence`` is STRICTLY greater than ``active.sequence`` — an
    equal or lower sequence is a stale replay (non-fresh). When either pointer carries no
    sequence the check is undecidable and returns True, so a legacy pointer is never called
    stale; the caller falls back to the version-based anti-rollback guard.
    """
    if incoming.sequence is None or active.sequence is None:
        return True
    return incoming.sequence > active.sequence


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
