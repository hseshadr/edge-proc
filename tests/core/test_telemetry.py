"""NullSink discards; BufferedSink keeps a bounded, ordered, in-memory ring."""

from __future__ import annotations

from uuid import UUID

from edgeproc.core.models import PrivacyMode, Provenance, ResultEnvelope, TaskKind
from edgeproc.core.telemetry import BufferedSink, NullSink


def _envelope(n: int) -> ResultEnvelope:
    return ResultEnvelope(
        request_id=UUID(int=n),
        task_kind=TaskKind.EMBED,
        success=True,
        payload={"n": n},
        runtime_used="fake",
        privacy_mode=PrivacyMode.LOCAL_ONLY,
        confidence=1.0,
        latency_ms=0.0,
        provenance=Provenance(signature_status="unsigned", runtime_version="0.1.0"),
    )


def test_null_sink_discards_silently() -> None:
    sink = NullSink()
    sink.emit(_envelope(1))  # must not raise


def test_buffered_sink_keeps_order() -> None:
    sink = BufferedSink()
    sink.emit(_envelope(1))
    sink.emit(_envelope(2))
    assert [e.payload["n"] for e in sink.all()] == [1, 2]
    assert len(sink) == 2


def test_buffered_sink_caps_and_drops_oldest() -> None:
    sink = BufferedSink(maxlen=2)
    for n in range(1, 4):
        sink.emit(_envelope(n))
    assert [e.payload["n"] for e in sink.all()] == [2, 3]
    assert len(sink) == 2
