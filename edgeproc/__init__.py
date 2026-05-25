"""EdgeProc — AI-native local execution substrate.

The whole core surface is importable from here. Runtimes (e.g. ``LocalVecRuntime``
under the ``[localvec]`` extra) and the bundle substrate (``[bundles]``) are
imported from their own subpackages so the core stays dependency-light.
"""

from __future__ import annotations

from edgeproc._version import __version__
from edgeproc.core.facade import EdgeProc
from edgeproc.core.models import (
    CapabilityVerdict,
    PrivacyMode,
    Provenance,
    ResultEnvelope,
    Task,
    TaskKind,
)
from edgeproc.core.protocols import Router, Runtime, TelemetrySink

__all__ = [
    "CapabilityVerdict",
    "EdgeProc",
    "PrivacyMode",
    "Provenance",
    "ResultEnvelope",
    "Router",
    "Runtime",
    "Task",
    "TaskKind",
    "TelemetrySink",
    "__version__",
]
