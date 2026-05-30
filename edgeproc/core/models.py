"""Core dispatch models — the typed contract every runtime speaks.

A consumer hands EdgeProc a :class:`Task`; every runtime returns a
:class:`ResultEnvelope`. Failures are encoded with ``success=False`` and an
``error`` string — never raised across the runtime boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from edgeproc.core.settings import EdgeProcSettings

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
"""JSON-shaped payload value. Deliberately not ``Any`` — payloads stay inspectable.

A PEP 695 ``type`` alias (not a plain assignment) so Pydantic resolves the
recursion lazily instead of blowing the schema builder's stack.
"""

# Single source of truth for the unsigned-provenance marker, shared by every runtime
# (facade + localvec) so the literal lives in exactly one place.
DEFAULT_SIGNATURE_STATUS: Final[str] = "unsigned"


def _default_budget_ms() -> int:
    """Per-task time budget default, sourced from settings (read lazily, not at import)."""
    return EdgeProcSettings().task_budget_ms


def _default_budget_memory_mb() -> int:
    """Per-task memory budget default, sourced from settings (read lazily)."""
    return EdgeProcSettings().task_budget_memory_mb


class PrivacyMode(StrEnum):
    """Where a task is allowed to run. Explicit at dispatch; no implicit fallback."""

    LOCAL_ONLY = "local_only"
    LOCAL_DRAFT_CLOUD_VERIFY = "local_draft_cloud_verify"
    CLOUD_PREMIUM = "cloud_premium"


class TaskKind(StrEnum):
    """The kind of work a task represents. Runtimes accept a subset of these."""

    DETERMINISTIC = "deterministic"
    EMBED = "embed"
    SEARCH = "search"
    RANK = "rank"
    GENERATE = "generate"
    CLASSIFY = "classify"
    CUSTOM_WASM = "custom_wasm"


class CapabilityVerdict(StrEnum):
    """A runtime's verdict on whether it can serve a task."""

    ACCEPT = "accept"
    REJECT_CAPABILITY = "reject_capability"
    REJECT_BUDGET = "reject_budget"
    REJECT_KIND = "reject_kind"


class Provenance(BaseModel):
    """Where a result came from — recorded on every envelope for audit."""

    signature_status: str
    runtime_version: str
    model_id: str | None = None
    model_hash: str | None = None
    bundle_id: str | None = None


class Task(BaseModel):
    """A unit of work routed to exactly one runtime.

    ``capability_token`` defaults to empty: v0 Biscuit gating is a documented
    pass-through stub (see spec decision #11), kept as a forward-compatible seam.
    """

    request_id: UUID = Field(default_factory=uuid4)
    kind: TaskKind
    payload: dict[str, JsonValue]
    privacy_mode: PrivacyMode
    capability_token: str = ""
    # Defaults flow from EdgeProcSettings (one source of truth); an explicit value still wins.
    budget_ms: int = Field(default_factory=_default_budget_ms)
    budget_memory_mb: int = Field(default_factory=_default_budget_memory_mb)
    path_signature: str | None = None


class ResultEnvelope(BaseModel):
    """The single return type of every runtime. Failures are data, not exceptions."""

    request_id: UUID
    task_kind: TaskKind
    success: bool
    payload: dict[str, JsonValue]
    runtime_used: str
    privacy_mode: PrivacyMode
    confidence: float
    latency_ms: float
    provenance: Provenance
    cost_usd: float = 0.0
    edit_distance: float | None = None
    error: str | None = None
