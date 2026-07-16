"""Deterministic in-flight task-memory reservations."""

from __future__ import annotations

import pytest

from edgeproc.core.memory import MemoryBudgetExceededError, MemoryManager


def test_reservation_releases_declared_bytes_after_scope() -> None:
    manager = MemoryManager(max_bytes=10)

    with manager.reserve(6):
        assert manager.reserved_bytes == 6

    assert manager.reserved_bytes == 0


def test_reservation_rejects_when_capacity_would_be_exceeded() -> None:
    manager = MemoryManager(max_bytes=10)

    with manager.reserve(6), pytest.raises(MemoryBudgetExceededError, match="capacity"):
        manager.reserve(5)


def test_reservation_rejects_a_request_larger_than_capacity() -> None:
    manager = MemoryManager(max_bytes=10)

    with pytest.raises(MemoryBudgetExceededError, match="capacity"):
        manager.reserve(11)


def test_reservation_releases_bytes_when_work_raises() -> None:
    manager = MemoryManager(max_bytes=10)

    with pytest.raises(RuntimeError, match="boom"), manager.reserve(6):
        raise RuntimeError("boom")

    assert manager.reserved_bytes == 0


def test_reservation_rejects_non_positive_bytes() -> None:
    manager = MemoryManager(max_bytes=10)

    with pytest.raises(ValueError, match="positive"):
        manager.reserve(0)
