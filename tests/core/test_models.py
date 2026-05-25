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
