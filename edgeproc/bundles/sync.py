"""Sync a bundle from an origin into a local cache, verifying every file.

Lifted from edge-reco's ``sync_catalog`` and generalised over ``FetchAdapter``.
Fails closed: a checksum mismatch raises rather than caching corrupt bytes.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from edgeproc.bundles.adapters import FetchAdapter
from edgeproc.bundles.manifest import BundleFile, BundleManifest, validate_checksum

log = structlog.get_logger(__name__)


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
    (cache_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
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
