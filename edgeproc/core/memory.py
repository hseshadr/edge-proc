"""Deterministic in-flight memory reservations for runtime dispatch.

The manager bounds the sum of declared task budgets for one ``EdgeProc`` instance.
It protects concurrency and admission control; it does not pretend to cap native
allocations or process RSS, which remain the host's responsibility.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from threading import Lock
from typing import Final

from edgeproc.core.settings import EdgeProcSettings

BYTES_PER_MEGABYTE: Final[int] = 1024 * 1024


class MemoryBudgetExceededError(MemoryError):
    """A task's declared reservation cannot fit in the configured capacity."""

    def __init__(self, requested_bytes: int, available_bytes: int, capacity_bytes: int) -> None:
        self.requested_bytes = requested_bytes
        self.available_bytes = available_bytes
        self.capacity_bytes = capacity_bytes
        super().__init__(
            f"memory reservation {requested_bytes} bytes exceeds capacity; "
            f"{available_bytes} bytes available of {capacity_bytes}"
        )


class MemoryManager:
    """Thread-safe admission control for declared in-flight task memory."""

    def __init__(self, max_bytes: int | None = None) -> None:
        configured = EdgeProcSettings().max_in_flight_memory_mb * BYTES_PER_MEGABYTE
        self._capacity = max_bytes if max_bytes is not None else configured
        if self._capacity <= 0:
            raise ValueError("memory capacity must be positive")
        self._reserved = 0
        self._lock = Lock()

    @property
    def capacity_bytes(self) -> int:
        """The maximum sum of active declared reservations."""
        return self._capacity

    def reserve(self, requested_bytes: int) -> AbstractContextManager[None]:
        """Admit a positive reservation and return its releasing context manager."""
        if requested_bytes <= 0:
            raise ValueError("memory reservation must be positive")
        with self._lock:
            available = self._capacity - self._reserved
            if requested_bytes > available:
                raise MemoryBudgetExceededError(requested_bytes, available, self._capacity)
            self._reserved += requested_bytes
        return _reservation(self, requested_bytes)

    def _release(self, requested_bytes: int) -> None:
        with self._lock:
            self._reserved -= requested_bytes


@contextmanager
def _reservation(manager: MemoryManager, requested_bytes: int) -> Iterator[None]:
    try:
        yield
    finally:
        manager._release(requested_bytes)
