"""EdgeProc — AI-native local execution substrate.

The whole core surface is importable from here: the facade, the contract models,
the Protocol seams, and the concrete defaults (``DefaultRouter``, ``NullSink``,
``BufferedSink``, ``RuntimeRegistry``) a consumer actually wires together.
Runtimes (e.g. ``LocalVecRuntime`` under the ``[localvec]`` extra) and the
bundle substrate (``[bundles]``) live in their own subpackages so the core stays
dependency-light.
"""

from __future__ import annotations

from edgeproc._version import __version__
from edgeproc.core.facade import EdgeProc
from edgeproc.core.memory import MemoryBudgetExceededError, MemoryManager
from edgeproc.core.models import (
    CapabilityVerdict,
    PrivacyMode,
    Provenance,
    ResultEnvelope,
    Task,
    TaskKind,
)
from edgeproc.core.protocols import Router, Runtime, TelemetrySink
from edgeproc.core.registry import RuntimeRegistry
from edgeproc.core.router import DefaultRouter
from edgeproc.core.telemetry import BufferedSink, NullSink

__all__ = [
    "BufferedSink",
    "CapabilityVerdict",
    "DefaultRouter",
    "EdgeProc",
    "MemoryBudgetExceededError",
    "MemoryManager",
    "NullSink",
    "PrivacyMode",
    "Provenance",
    "ResultEnvelope",
    "Router",
    "Runtime",
    "RuntimeRegistry",
    "Task",
    "TaskKind",
    "TelemetrySink",
    "__version__",
]
