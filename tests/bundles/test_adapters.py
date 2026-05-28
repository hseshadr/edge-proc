"""Fetch adapters: filesystem (local/dev) and HTTP/CDN. No network in tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from edgeproc.bundles.adapters import FetchAdapter, FilesystemAdapter, HttpAdapter


def test_adapters_conform_to_protocol() -> None:
    assert isinstance(FilesystemAdapter(), FetchAdapter)
    assert isinstance(HttpAdapter(), FetchAdapter)


def test_filesystem_adapter_fetch_bytes_reads_file(tmp_path: Path) -> None:
    blob = tmp_path / "chunk"
    blob.write_bytes(b"HELLO")
    assert FilesystemAdapter().fetch_bytes(str(blob)) == b"HELLO"


def _mock_transport() -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"CHUNKBYTES")

    return httpx.MockTransport(handler)


def test_http_adapter_fetch_bytes_returns_response_body() -> None:
    with HttpAdapter(transport=_mock_transport()) as adapter:
        assert adapter.fetch_bytes("https://cdn.example/chunk/abc") == b"CHUNKBYTES"


def test_http_adapter_default_timeout_honors_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGEPROC_HTTP_TIMEOUT", "7.0")
    adapter = HttpAdapter()
    assert adapter._timeout == 7.0


def test_http_adapter_close_is_idempotent() -> None:
    adapter = HttpAdapter(transport=_mock_transport())
    adapter.fetch_bytes("https://cdn.example/chunk/abc")
    adapter.close()
    adapter.close()  # safe to call again
