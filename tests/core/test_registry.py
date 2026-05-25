"""The registry holds runtimes in registration order and rejects name collisions."""

from __future__ import annotations

import pytest

from edgeproc.core.models import CapabilityVerdict, Task
from edgeproc.core.registry import RuntimeRegistry


class _Runtime:
    def __init__(self, name: str) -> None:
        self.name = name

    def can_handle(self, task: Task) -> CapabilityVerdict:
        return CapabilityVerdict.ACCEPT

    async def execute(self, task: Task) -> object:  # pragma: no cover
        raise NotImplementedError


def test_preserves_registration_order() -> None:
    registry = RuntimeRegistry()
    registry.register(_Runtime("a"))
    registry.register(_Runtime("b"))
    assert [r.name for r in registry.runtimes] == ["a", "b"]


def test_rejects_duplicate_names() -> None:
    registry = RuntimeRegistry()
    registry.register(_Runtime("a"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(_Runtime("a"))


def test_empty_registry_has_no_runtimes() -> None:
    assert list(RuntimeRegistry().runtimes) == []
