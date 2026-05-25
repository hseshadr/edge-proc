"""Bundle manifest parsing and sha256 checksum validation (lifted from edge-reco)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from edgeproc.bundles.manifest import BundleManifest, parse_manifest, validate_checksum


def test_parse_manifest_round_trips_from_json(tmp_path: Path) -> None:
    payload = {
        "bundle_id": "products-2026-05-25",
        "version": "1.0.0",
        "files": [{"path": "index.faiss", "checksum": "sha256:abc"}],
        "metadata": {"embedding_model": "all-MiniLM-L6-v2"},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload))
    manifest = parse_manifest(manifest_path)
    assert isinstance(manifest, BundleManifest)
    assert manifest.bundle_id == "products-2026-05-25"
    assert manifest.files[0].path == "index.faiss"
    assert manifest.metadata["embedding_model"] == "all-MiniLM-L6-v2"


def test_validate_checksum_accepts_matching_sha256(tmp_path: Path) -> None:
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"hello edgeproc")
    digest = hashlib.sha256(b"hello edgeproc").hexdigest()
    assert validate_checksum(blob, f"sha256:{digest}") is True


def test_validate_checksum_rejects_wrong_hash(tmp_path: Path) -> None:
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"hello edgeproc")
    assert validate_checksum(blob, "sha256:deadbeef") is False


def test_validate_checksum_rejects_unprefixed_value(tmp_path: Path) -> None:
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"x")
    digest = hashlib.sha256(b"x").hexdigest()
    assert validate_checksum(blob, digest) is False  # missing "sha256:" prefix
