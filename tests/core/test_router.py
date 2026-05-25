"""The router is a pure, deterministic, first-accept selector over registration order."""

from __future__ import annotations

from edgeproc.core.models import CapabilityVerdict, PrivacyMode, Task, TaskKind
from edgeproc.core.router import DefaultRouter


class _Runtime:
    def __init__(self, name: str, verdict: CapabilityVerdict) -> None:
        self.name = name
        self._verdict = verdict

    def can_handle(self, task: Task) -> CapabilityVerdict:
        return self._verdict

    async def execute(self, task: Task) -> object:  # pragma: no cover - unused in router tests
        raise NotImplementedError


def _task() -> Task:
    return Task(kind=TaskKind.EMBED, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)


def test_picks_the_first_runtime_that_accepts() -> None:
    a = _Runtime("a", CapabilityVerdict.ACCEPT)
    b = _Runtime("b", CapabilityVerdict.ACCEPT)
    assert DefaultRouter().pick(_task(), [a, b]) is a


def test_skips_rejecting_runtimes() -> None:
    a = _Runtime("a", CapabilityVerdict.REJECT_KIND)
    b = _Runtime("b", CapabilityVerdict.ACCEPT)
    assert DefaultRouter().pick(_task(), [a, b]) is b


def test_returns_none_when_all_reject() -> None:
    a = _Runtime("a", CapabilityVerdict.REJECT_BUDGET)
    b = _Runtime("b", CapabilityVerdict.REJECT_CAPABILITY)
    assert DefaultRouter().pick(_task(), [a, b]) is None


def test_returns_none_for_empty_registry() -> None:
    assert DefaultRouter().pick(_task(), []) is None


def test_is_deterministic_across_repeated_calls() -> None:
    runtimes = [
        _Runtime("a", CapabilityVerdict.REJECT_KIND),
        _Runtime("b", CapabilityVerdict.ACCEPT),
        _Runtime("c", CapabilityVerdict.ACCEPT),
    ]
    router = DefaultRouter()
    task = _task()
    first = router.pick(task, runtimes)
    assert all(router.pick(task, runtimes) is first for _ in range(10))
    assert first is runtimes[1]
