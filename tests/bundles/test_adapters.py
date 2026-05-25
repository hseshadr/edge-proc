"""Fetch adapters: filesystem (local/dev) and HTTP/CDN. No network in tests."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from edgeproc.bundles.adapters import FetchAdapter, FilesystemAdapter, HttpAdapter

_MANIFEST = {
    "bundle_id": "b1",
    "version": "1.0.0",
    "files": [{"path": "index.faiss", "checksum": "sha256:abc"}],
}


def test_filesystem_adapter_conforms_to_protocol() -> None:
    assert isinstance(FilesystemAdapter(), FetchAdapter)
    assert isinstance(HttpAdapter(), FetchAdapter)


def test_filesystem_adapter_reads_manifest_and_copies_files(tmp_path: Path) -> None:
    source = tmp_path / "origin"
    source.mkdir()
    (source / "manifest.json").write_text(json.dumps(_MANIFEST))
    (source / "index.faiss").write_bytes(b"FAISSDATA")
    adapter = FilesystemAdapter()

    manifest = adapter.fetch_manifest(str(source / "manifest.json"))
    assert manifest.bundle_id == "b1"

    dest = tmp_path / "cache" / "index.faiss"
    adapter.fetch_file(str(source), "index.faiss", dest)
    assert dest.read_bytes() == b"FAISSDATA"


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json=_MANIFEST)
        return httpx.Response(200, content=b"FAISSDATA")

    return httpx.MockTransport(handler)


def test_http_adapter_fetches_manifest_and_streams_file(tmp_path: Path) -> None:
    adapter = HttpAdapter(transport=_mock_transport())
    manifest = adapter.fetch_manifest("https://cdn.example/bundle/manifest.json")
    assert manifest.files[0].path == "index.faiss"

    dest = tmp_path / "index.faiss"
    adapter.fetch_file("https://cdn.example/bundle/", "index.faiss", dest)
    assert dest.read_bytes() == b"FAISSDATA"
