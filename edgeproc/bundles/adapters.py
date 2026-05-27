"""Pluggable fetch backends for bundle distribution (lifted from edge-reco).

``FetchAdapter`` is the seam: swap filesystem for HTTP/CDN (or a future S3/IPFS
adapter) without touching the sync logic.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import TracebackType
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

    def fetch_bytes(self, url: str) -> bytes: ...


class FilesystemAdapter:
    """Reads bundles from the local filesystem — ideal for dev and tests."""

    def fetch_manifest(self, source: str) -> BundleManifest:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        return BundleManifest.model_validate(data)

    def fetch_file(self, base: str, path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(base) / path, local_path)

    def fetch_bytes(self, url: str) -> bytes:
        return Path(url).read_bytes()


class HttpAdapter:
    """Streams bundles from an HTTP/CDN origin, reusing ONE client per adapter.

    A many-chunk ``sync_index`` calls ``fetch_bytes`` once per missing chunk, so the
    adapter owns a single lazily-created :class:`httpx.Client` (connection pooling /
    keep-alive) for its lifetime; ``close`` and the context-manager protocol release
    it. The ``transport=`` test seam is preserved.
    """

    def __init__(
        self, timeout: float | None = None, transport: httpx.BaseTransport | None = None
    ) -> None:
        self._timeout = timeout if timeout is not None else EdgeProcSettings().http_timeout
        self._transport = transport
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, transport=self._transport)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> HttpAdapter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def fetch_manifest(self, source: str) -> BundleManifest:
        response = self._get_client().get(source)
        response.raise_for_status()
        return BundleManifest.model_validate(response.json())

    def fetch_bytes(self, url: str) -> bytes:
        response = self._get_client().get(url)
        response.raise_for_status()
        return response.content

    def fetch_file(self, base: str, path: str, local_path: Path) -> None:
        url = urljoin(base.rstrip("/") + "/", path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_client().stream("GET", url) as response:
            response.raise_for_status()
            with local_path.open("wb") as sink:
                for chunk in response.iter_bytes(chunk_size=_CHUNK):
                    sink.write(chunk)
