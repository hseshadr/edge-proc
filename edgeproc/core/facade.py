"""The one import surface a consumer uses: build an EdgeProc, hand it a Task.

Construct it with an explicit registry — ``EdgeProc(RuntimeRegistry())`` for the
deterministic core, or a registry holding a configured runtime (e.g.
``LocalVecRuntime``). There is deliberately no zero-argument convenience default:
an EdgeProc with an empty registry refuses every task, so a "default" constructor
would advertise a working object that cannot do any work. Auto-probing installed
kernels is roadmap.
"""

from __future__ import annotations

from edgeproc._version import __version__
from edgeproc.core.memory import BYTES_PER_MEGABYTE, MemoryBudgetExceededError, MemoryManager
from edgeproc.core.models import DEFAULT_SIGNATURE_STATUS, Provenance, ResultEnvelope, Task
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
        memory_manager: MemoryManager | None = None,
    ) -> None:
        self._registry = registry
        # Identity (`is not None`), never truthiness: a real but empty BufferedSink has
        # __len__ == 0, so `x or default` would silently swap the caller's sink for the
        # default and drop their telemetry. The None check guards that data-loss bug.
        self._router = router if router is not None else DefaultRouter()
        self._sink = sink if sink is not None else NullSink()
        self._memory_manager = memory_manager if memory_manager is not None else MemoryManager()

    @property
    def memory_manager(self) -> MemoryManager:
        """The admission controller; share it across facades to share one capacity."""
        return self._memory_manager

    async def run(self, task: Task) -> ResultEnvelope:
        runtime = self._router.pick(task, self._registry.runtimes)
        envelope = await self._run_with_memory(task, runtime)
        self._sink.emit(envelope)
        return envelope

    async def _run_with_memory(self, task: Task, runtime: Runtime | None) -> ResultEnvelope:
        if runtime is None:
            return self._no_runtime_envelope(task)
        if task.budget_memory_mb <= 0:
            return self._invalid_memory_budget_envelope(task, runtime)
        try:
            with self._memory_manager.reserve(task.budget_memory_mb * BYTES_PER_MEGABYTE):
                return await self._dispatch(task, runtime)
        except MemoryBudgetExceededError:
            return self._memory_rejection_envelope(task, runtime)

    async def _dispatch(self, task: Task, runtime: Runtime | None) -> ResultEnvelope:
        if runtime is None:
            return self._no_runtime_envelope(task)
        return await runtime.execute(task)

    @staticmethod
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
            provenance=Provenance(
                signature_status=DEFAULT_SIGNATURE_STATUS, runtime_version=__version__
            ),
            error="no_runtime_accepted",
        )

    @staticmethod
    def _memory_rejection_envelope(task: Task, runtime: Runtime) -> ResultEnvelope:
        """Report admission refusal without invoking the selected runtime."""
        return ResultEnvelope(
            request_id=task.request_id,
            task_kind=task.kind,
            success=False,
            payload={},
            runtime_used=runtime.name,
            privacy_mode=task.privacy_mode,
            confidence=0.0,
            latency_ms=0.0,
            provenance=Provenance(
                signature_status=DEFAULT_SIGNATURE_STATUS, runtime_version=__version__
            ),
            error="memory_budget_exceeded",
        )

    @staticmethod
    def _invalid_memory_budget_envelope(task: Task, runtime: Runtime) -> ResultEnvelope:
        """Encode a forged/unvalidated non-positive task budget as data."""
        return ResultEnvelope(
            request_id=task.request_id,
            task_kind=task.kind,
            success=False,
            payload={},
            runtime_used=runtime.name,
            privacy_mode=task.privacy_mode,
            confidence=0.0,
            latency_ms=0.0,
            provenance=Provenance(
                signature_status=DEFAULT_SIGNATURE_STATUS, runtime_version=__version__
            ),
            error="invalid_memory_budget",
        )
