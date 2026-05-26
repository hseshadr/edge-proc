"""Telemetry sinks. The default ships a no-op; nothing phones home by default.

``BufferedSink`` is the capped in-memory ring lifted from edge-reco's
``EventBuffer``, generalised over :class:`ResultEnvelope`.
"""

from __future__ import annotations

from collections import deque
from typing import Final

from edgeproc.core.models import ResultEnvelope

DEFAULT_MAXLEN: Final[int] = 10_000


class NullSink:
    """Discards every envelope. The fail-quiet default observability path."""

    def emit(self, envelope: ResultEnvelope) -> None:
        return None


class BufferedSink:
    """Bounded ring buffer of recent envelopes; drops oldest past ``maxlen``."""

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._buffer: deque[ResultEnvelope] = deque(maxlen=maxlen)

    def emit(self, envelope: ResultEnvelope) -> None:
        self._buffer.append(envelope)

    def all(self) -> list[ResultEnvelope]:
        return list(self._buffer)

    def __len__(self) -> int:
        return len(self._buffer)
