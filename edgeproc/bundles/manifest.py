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

from pydantic import BaseModel, Field

_SHA256_PREFIX = "sha256:"


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
