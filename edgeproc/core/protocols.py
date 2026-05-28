"""Structural seams of the substrate.

These Protocols are the extension points. A ``Runtime`` is anything that can say
whether it handles a task and then execute it; the Wasmtime / MLX / llama.cpp
kernels on the roadmap slot in here without any consumer change. The ``Router``
is a pure function of ``(Task, runtimes)`` — never an LLM, never stateful.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from edgeproc.core.models import CapabilityVerdict, ResultEnvelope, Task


@runtime_checkable
class Runtime(Protocol):
    """Something that can serve a subset of :class:`TaskKind`."""

    name: str

    def can_handle(self, task: Task) -> CapabilityVerdict:
        """Return ``ACCEPT`` to claim the task, or a ``REJECT_*`` reason."""
        ...

    async def execute(self, task: Task) -> ResultEnvelope:
        """Run the task. Encode failure as ``success=False``; never raise across this boundary."""
        ...


@runtime_checkable
class Router(Protocol):
    """Pure selector: pick the runtime that serves a task, or ``None`` if none can."""

    def pick(self, task: Task, runtimes: Sequence[Runtime]) -> Runtime | None:
        """Deterministic — identical inputs always yield the identical choice."""
        ...


@runtime_checkable
class TelemetrySink(Protocol):
    """The only observability path. Runtimes do not log, write files, or phone home.

    The shipped implementations are :class:`NullSink` (the default, discards everything)
    and :class:`BufferedSink` (an in-memory ring). The seam exists so an ``OTelSink``
    (OpenTelemetry spans) or ``FileSink`` (JSONL audit log) can drop in without any
    runtime change — a deployment swaps the sink at the facade, never inside a runtime.
    """

    def emit(self, envelope: ResultEnvelope) -> None:
        """Record one result envelope."""
        ...
