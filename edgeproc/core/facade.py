"""The one import surface a consumer uses: build an EdgeProc, hand it a Task.

v0 ``local_default()`` returns an instance with an empty registry — consumers
register a runtime explicitly (e.g. ``EdgeProc(registry)`` with a configured
``LocalVecRuntime``). Auto-probing installed kernels is roadmap.
"""

from __future__ import annotations

from edgeproc._version import __version__
from edgeproc.core.models import Provenance, ResultEnvelope, Task
from edgeproc.core.protocols import Router, Runtime, TelemetrySink
from edgeproc.core.registry import RuntimeRegistry
from edgeproc.core.router import DefaultRouter
from edgeproc.core.telemetry import NullSink


class EdgeProc:
    """Routes a task to a runtime, emits the result, and fails closed on no match."""

    def __init__(
        self,
        registry: RuntimeRegistry,
        router: Router | None = None,
        sink: TelemetrySink | None = None,
    ) -> None:
        self._registry = registry
        # Explicit None checks: a sink/router may be falsy (e.g. an empty BufferedSink
        # has __len__ == 0), so `x or default` would silently discard a real instance.
        self._router = router if router is not None else DefaultRouter()
        self._sink = sink if sink is not None else NullSink()

    @classmethod
    def local_default(cls) -> EdgeProc:
        """Pure-deterministic router + null sink + empty registry."""
        return cls(registry=RuntimeRegistry())

    async def run(self, task: Task) -> ResultEnvelope:
        runtime = self._router.pick(task, self._registry.runtimes)
        envelope = await self._dispatch(task, runtime)
        self._sink.emit(envelope)
        return envelope

    async def _dispatch(self, task: Task, runtime: Runtime | None) -> ResultEnvelope:
        if runtime is None:
            return _no_runtime_envelope(task)
        return await runtime.execute(task)


def _no_runtime_envelope(task: Task) -> ResultEnvelope:
    """The fail-closed result when no runtime accepts — no silent local fallback."""
    return ResultEnvelope(
        request_id=task.request_id,
        task_kind=task.kind,
        success=False,
        payload={},
        runtime_used="none",
        privacy_mode=task.privacy_mode,
        confidence=0.0,
        latency_ms=0.0,
        provenance=Provenance(signature_status="unsigned", runtime_version=__version__),
        error="no_runtime_accepted",
    )
