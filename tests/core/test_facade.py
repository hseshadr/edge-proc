"""The facade routes a task to a runtime, emits the result, and never raises on miss."""

from __future__ import annotations

import pytest

from edgeproc.core.facade import EdgeProc
from edgeproc.core.memory import MemoryManager
from edgeproc.core.models import (
    DEFAULT_SIGNATURE_STATUS,
    CapabilityVerdict,
    PrivacyMode,
    Provenance,
    ResultEnvelope,
    Task,
    TaskKind,
)
from edgeproc.core.registry import RuntimeRegistry
from edgeproc.core.telemetry import BufferedSink

pytestmark = pytest.mark.asyncio


class _Runtime:
    def __init__(self, name: str, verdict: CapabilityVerdict) -> None:
        self.name = name
        self._verdict = verdict
        self.calls = 0

    def can_handle(self, task: Task) -> CapabilityVerdict:
        return self._verdict

    async def execute(self, task: Task) -> ResultEnvelope:
        self.calls += 1
        return ResultEnvelope(
            request_id=task.request_id,
            task_kind=task.kind,
            success=True,
            payload={"served": True},
            runtime_used=self.name,
            privacy_mode=task.privacy_mode,
            confidence=1.0,
            latency_ms=2.5,
            provenance=Provenance(signature_status="unsigned", runtime_version="0.1.0"),
        )


def _task() -> Task:
    return Task(kind=TaskKind.EMBED, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)


def _edgeproc(
    *runtimes: _Runtime,
    sink: BufferedSink,
    memory_manager: MemoryManager | None = None,
) -> EdgeProc:
    registry = RuntimeRegistry()
    for runtime in runtimes:
        registry.register(runtime)
    return EdgeProc(registry=registry, sink=sink, memory_manager=memory_manager)


async def test_dispatches_to_the_accepting_runtime() -> None:
    sink = BufferedSink()
    ep = _edgeproc(_Runtime("vec", CapabilityVerdict.ACCEPT), sink=sink)
    result = await ep.run(_task())
    assert result.success is True
    assert result.runtime_used == "vec"
    assert result.payload == {"served": True}


async def test_emits_the_result_to_the_sink() -> None:
    sink = BufferedSink()
    ep = _edgeproc(_Runtime("vec", CapabilityVerdict.ACCEPT), sink=sink)
    await ep.run(_task())
    assert len(sink) == 1


async def test_returns_failure_envelope_when_no_runtime_accepts() -> None:
    sink = BufferedSink()
    ep = _edgeproc(_Runtime("vec", CapabilityVerdict.REJECT_KIND), sink=sink)
    task = _task()
    result = await ep.run(task)
    assert result.success is False
    assert result.error == "no_runtime_accepted"
    assert result.request_id == task.request_id
    assert result.runtime_used == "none"
    # Provenance status flows from the single shared constant, not a stray literal.
    assert result.provenance.signature_status == DEFAULT_SIGNATURE_STATUS


async def test_emits_the_failure_envelope_too() -> None:
    sink = BufferedSink()
    ep = _edgeproc(_Runtime("vec", CapabilityVerdict.REJECT_BUDGET), sink=sink)
    await ep.run(_task())
    assert len(sink) == 1
    assert sink.all()[0].success is False


async def test_refuses_task_when_declared_memory_exceeds_capacity() -> None:
    sink = BufferedSink()
    runtime = _Runtime("vec", CapabilityVerdict.ACCEPT)
    ep = _edgeproc(runtime, sink=sink, memory_manager=MemoryManager(max_bytes=1))
    task = _task().model_copy(update={"budget_memory_mb": 2})

    result = await ep.run(task)

    assert result.success is False
    assert result.error == "memory_budget_exceeded"
    assert runtime.calls == 0
    assert ep.memory_manager.reserved_bytes == 0


async def test_local_default_builds_a_usable_instance() -> None:
    ep = EdgeProc.local_default()
    # No runtimes are guaranteed installed in v0-core, so a bare task fails closed.
    result = await ep.run(_task())
    assert isinstance(result, ResultEnvelope)
