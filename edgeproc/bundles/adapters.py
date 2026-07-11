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


class ResponseTooLargeError(httpx.HTTPError):
    """An origin returned a body larger than the configured cap (fail-closed).

    Subclasses ``httpx.HTTPError`` so every existing fetch-failure handler already
    catches it — an oversized body is just another way the fetch failed.
    """


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
        self,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
        *,
        max_bytes: int | None = None,
    ) -> None:
        settings = EdgeProcSettings()
        self._timeout = timeout if timeout is not None else settings.http_timeout
        self._max_bytes = max_bytes if max_bytes is not None else settings.max_fetch_bytes
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
        with self._get_client().stream("GET", url) as response:
            response.raise_for_status()
            return self._read_capped(response)

    def _read_capped(self, response: httpx.Response) -> bytes:
        """Buffer the streamed body, refusing anything past ``max_bytes`` (fail-closed)."""
        buffer = bytearray()
        for chunk in response.iter_bytes():
            buffer.extend(chunk)
            if len(buffer) > self._max_bytes:
                raise ResponseTooLargeError(f"response body exceeds {self._max_bytes}-byte cap")
        return bytes(buffer)
