from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHADOW = ROOT / ".github/workflows/dagger-shadow.yml"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _steps(job: Mapping[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps")
    return [_mapping(step) for step in steps] if isinstance(steps, list) else []


def test_should_run_shadow_ci_only_through_pinned_checkout_and_dagger() -> None:
    # Given
    document = _mapping(yaml.safe_load(SHADOW.read_text(encoding="utf-8")))
    jobs = _mapping(document.get("jobs"))
    job = _mapping(jobs.get("dagger-shadow"))
    steps = _steps(job)

    # When
    uses = [str(step.get("uses", "")) for step in steps]
    checkout = _mapping(steps[0].get("with"))
    invocation = _mapping(steps[1].get("with"))

    # Then
    assert len(steps) == 2
    assert job.get("name") == "Dagger"
    assert all(PINNED.fullmatch(action) for action in uses)
    assert uses[0].startswith("actions/checkout@")
    assert checkout.get("persist-credentials") is False
    assert uses[1].startswith("dagger/dagger-for-github@")
    assert invocation.get("version") == "0.21.8"
    assert invocation.get("verb") == "call"
    assert invocation.get("args") == "ci --commit-sha=${{ github.sha }}"
    assert "secrets." not in SHADOW.read_text(encoding="utf-8")
