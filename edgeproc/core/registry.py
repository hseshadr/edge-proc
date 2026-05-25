"""Ordered registry of runtimes. Registration order is router priority."""

from __future__ import annotations

from collections.abc import Sequence

from edgeproc.core.protocols import Runtime


class RuntimeRegistry:
    """Holds runtimes in registration order; names must be unique (fail closed)."""

    def __init__(self) -> None:
        self._runtimes: list[Runtime] = []

    def register(self, runtime: Runtime) -> None:
        if any(existing.name == runtime.name for existing in self._runtimes):
            raise ValueError(f"runtime {runtime.name!r} already registered")
        self._runtimes.append(runtime)

    @property
    def runtimes(self) -> Sequence[Runtime]:
        return tuple(self._runtimes)
