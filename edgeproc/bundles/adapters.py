"""Pluggable fetch backends for bundle distribution (lifted from edge-reco).

``FetchAdapter`` is the seam: swap filesystem for HTTP/CDN (or a future S3/IPFS
adapter) without touching the sync logic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Final, Protocol, runtime_checkable
from urllib.parse import urljoin

import httpx

from edgeproc.bundles.manifest import BundleManifest
from edgeproc.core.settings import EdgeProcSettings

_CHUNK: Final[int] = 8192


@runtime_checkable
class FetchAdapter(Protocol):
    """Fetches a bundle manifest and its files from some source."""

    def fetch_manifest(self, source: str) -> BundleManifest: ...

    def fetch_file(self, base: str, path: str, local_path: Path) -> None: ...


class FilesystemAdapter:
    """Reads bundles from the local filesystem — ideal for dev and tests."""

    def fetch_manifest(self, source: str) -> BundleManifest:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        return BundleManifest.model_validate(data)

    def fetch_file(self, base: str, path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(base) / path, local_path)


class HttpAdapter:
    """Streams bundles from an HTTP/CDN origin."""

    def __init__(
        self, timeout: float | None = None, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._timeout = timeout if timeout is not None else EdgeProcSettings().http_timeout
        self._transport = transport

    def fetch_manifest(self, source: str) -> BundleManifest:
        with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
            response = client.get(source)
            response.raise_for_status()
            return BundleManifest.model_validate(response.json())

    def fetch_file(self, base: str, path: str, local_path: Path) -> None:
        url = urljoin(base.rstrip("/") + "/", path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            httpx.Client(timeout=self._timeout, transport=self._transport) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            with local_path.open("wb") as sink:
                for chunk in response.iter_bytes(chunk_size=_CHUNK):
                    sink.write(chunk)
