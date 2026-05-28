"""Pluggable fetch backends for v2 bundle distribution.

``FetchAdapter`` is the seam: swap filesystem for HTTP/CDN (or a future S3/IPFS
adapter) without touching the sync logic. The substrate only needs
``fetch_bytes`` — the pointer, manifest, and each chunk are all just bytes.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Protocol, runtime_checkable

import httpx

from edgeproc.core.settings import EdgeProcSettings


@runtime_checkable
class FetchAdapter(Protocol):
    """Fetches opaque bytes by URL — pointer, manifest, and chunk all share this seam."""

    def fetch_bytes(self, url: str) -> bytes: ...


class FilesystemAdapter:
    """Reads bytes from the local filesystem — ideal for dev and tests."""

    def fetch_bytes(self, url: str) -> bytes:
        return Path(url).read_bytes()


class HttpAdapter:
    """Streams bytes from an HTTP/CDN origin, reusing ONE client per adapter.

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

    def fetch_bytes(self, url: str) -> bytes:
        response = self._get_client().get(url)
        response.raise_for_status()
        return response.content
