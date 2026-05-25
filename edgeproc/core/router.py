"""The pure deterministic router.

v0 policy: pick the first registered runtime whose ``can_handle`` returns
``ACCEPT``. Registration order is the priority — stable, replayable, and
sub-microsecond. Cost-based ranking across accepting runtimes is roadmap; it
does not change the no-LLM, no-hidden-state guarantee.
"""

from __future__ import annotations

from collections.abc import Sequence

from edgeproc.core.models import CapabilityVerdict, Task
from edgeproc.core.protocols import Runtime


class DefaultRouter:
    """First-accept selector over registration order. A pure function with no state."""

    def pick(self, task: Task, runtimes: Sequence[Runtime]) -> Runtime | None:
        for runtime in runtimes:
            if runtime.can_handle(task) == CapabilityVerdict.ACCEPT:
                return runtime
        return None
