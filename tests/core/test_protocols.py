"""The core Protocols are structural seams: any conforming object is a Runtime."""

from __future__ import annotations

from uuid import UUID

from edgeproc.core.models import (
    CapabilityVerdict,
    PrivacyMode,
    Provenance,
    ResultEnvelope,
    Task,
    TaskKind,
)
from edgeproc.core.protocols import Runtime, TelemetrySink


class _AcceptingRuntime:
    name = "fake"

    def can_handle(self, task: Task) -> CapabilityVerdict:
        return CapabilityVerdict.ACCEPT

    async def execute(self, task: Task) -> ResultEnvelope:
        return ResultEnvelope(
            request_id=task.request_id,
            task_kind=task.kind,
            success=True,
            payload={},
            runtime_used=self.name,
            privacy_mode=task.privacy_mode,
            confidence=1.0,
            latency_ms=0.0,
            provenance=Provenance(signature_status="unsigned", runtime_version="0.1.0"),
        )


class _CountingSink:
    def __init__(self) -> None:
        self.count = 0

    def emit(self, envelope: ResultEnvelope) -> None:
        self.count += 1


def test_conforming_object_is_a_runtime() -> None:
    assert isinstance(_AcceptingRuntime(), Runtime)


def test_plain_object_is_not_a_runtime() -> None:
    assert not isinstance(object(), Runtime)


def test_conforming_object_is_a_telemetry_sink() -> None:
    assert isinstance(_CountingSink(), TelemetrySink)


def test_envelope_round_trips_through_a_sink() -> None:
    sink = _CountingSink()
    env = ResultEnvelope(
        request_id=UUID(int=1),
        task_kind=TaskKind.EMBED,
        success=True,
        payload={},
        runtime_used="fake",
        privacy_mode=PrivacyMode.LOCAL_ONLY,
        confidence=1.0,
        latency_ms=0.0,
        provenance=Provenance(signature_status="unsigned", runtime_version="0.1.0"),
    )
    sink.emit(env)
    assert sink.count == 1
