"""sync_bundle fetches a manifest, downloads + checksum-verifies files, caches locally."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.sync import sync_bundle


def _origin(tmp_path: Path, *, checksum: str) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "index.faiss").write_bytes(b"FAISSDATA")
    manifest = {
        "bundle_id": "b1",
        "version": "1.0.0",
        "files": [{"path": "index.faiss", "checksum": checksum}],
    }
    (origin / "manifest.json").write_text(json.dumps(manifest))
    return origin


def test_sync_downloads_verifies_and_caches(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"FAISSDATA").hexdigest()
    origin = _origin(tmp_path, checksum=f"sha256:{digest}")
    cache = tmp_path / "cache"

    manifest = sync_bundle(
        manifest_url=str(origin / "manifest.json"),
        cache_dir=cache,
        adapter=FilesystemAdapter(),
        file_base_url=str(origin),
    )

    assert manifest.bundle_id == "b1"
    assert (cache / "index.faiss").read_bytes() == b"FAISSDATA"
    assert (cache / "manifest.json").exists()


def test_sync_fails_closed_on_checksum_mismatch(tmp_path: Path) -> None:
    origin = _origin(tmp_path, checksum="sha256:deadbeef")
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="checksum"):
        sync_bundle(
            manifest_url=str(origin / "manifest.json"),
            cache_dir=cache,
            adapter=FilesystemAdapter(),
            file_base_url=str(origin),
        )
