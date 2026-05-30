"""Behavioral contract for the core dispatch models."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from edgeproc.core.models import (
    CapabilityVerdict,
    PrivacyMode,
    Provenance,
    ResultEnvelope,
    Task,
    TaskKind,
)


def test_task_autogenerates_unique_request_id() -> None:
    one = Task(kind=TaskKind.EMBED, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)
    two = Task(kind=TaskKind.EMBED, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)
    assert isinstance(one.request_id, UUID)
    assert one.request_id != two.request_id


def test_task_applies_default_budgets() -> None:
    task = Task(kind=TaskKind.SEARCH, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)
    assert task.budget_ms == 5000
    assert task.budget_memory_mb == 256


def test_task_budget_defaults_come_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The default budget is sourced from EdgeProcSettings (one source of truth), not a
    # bare literal on the model: an env override flows into a Task built without budgets.
    monkeypatch.setenv("EDGEPROC_TASK_BUDGET_MS", "7777")
    monkeypatch.setenv("EDGEPROC_TASK_BUDGET_MEMORY_MB", "333")

    task = Task(kind=TaskKind.SEARCH, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)

    assert task.budget_ms == 7777
    assert task.budget_memory_mb == 333


def test_explicit_per_task_budget_overrides_the_settings_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A caller passing explicit budgets must always win over the settings default.
    monkeypatch.setenv("EDGEPROC_TASK_BUDGET_MS", "7777")
    monkeypatch.setenv("EDGEPROC_TASK_BUDGET_MEMORY_MB", "333")

    task = Task(
        kind=TaskKind.SEARCH,
        payload={},
        privacy_mode=PrivacyMode.LOCAL_ONLY,
        budget_ms=11,
        budget_memory_mb=22,
    )

    assert task.budget_ms == 11
    assert task.budget_memory_mb == 22


def test_task_capability_token_defaults_to_empty_v0_passthrough() -> None:
    task = Task(kind=TaskKind.EMBED, payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)
    assert task.capability_token == ""


def test_task_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Task(kind="teleport", payload={}, privacy_mode=PrivacyMode.LOCAL_ONLY)  # type: ignore[arg-type]


def test_task_payload_accepts_nested_json_values() -> None:
    task = Task(
        kind=TaskKind.EMBED,
        payload={"texts": ["a", "b"], "opts": {"k": 3}},
        privacy_mode=PrivacyMode.LOCAL_ONLY,
    )
    assert task.payload["opts"] == {"k": 3}


def test_result_envelope_cost_defaults_to_zero() -> None:
    env = ResultEnvelope(
        request_id=UUID(int=0),
        task_kind=TaskKind.EMBED,
        success=True,
        payload={},
        runtime_used="fake",
        privacy_mode=PrivacyMode.LOCAL_ONLY,
        confidence=1.0,
        latency_ms=1.2,
        provenance=Provenance(signature_status="unsigned", runtime_version="0.1.0"),
    )
    assert env.cost_usd == 0.0
    assert env.error is None


def test_capability_verdict_has_reject_reasons() -> None:
    assert CapabilityVerdict.ACCEPT == "accept"
    assert {"reject_capability", "reject_budget", "reject_kind"} <= {
        v.value for v in CapabilityVerdict
    }
