"""Executable contracts for thin Dagger ingress and source-free PyPI OIDC."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
DAGGER_ACTION = "dagger/dagger-for-github"
CHECKOUT_ACTION = "actions/checkout"
UPLOAD_ACTION = "actions/upload-artifact"
DOWNLOAD_ACTION = "actions/download-artifact"
PUBLISH_ACTION = "pypa/gh-action-pypi-publish"


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {"on" if key is True else str(key): item for key, item in value.items()}


def _workflow(name: str) -> dict[str, object]:
    path = WORKFLOWS / name
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")))


def _job(document: Mapping[str, object], name: str) -> dict[str, object]:
    return _mapping(_mapping(document.get("jobs")).get(name))


def _steps(job: Mapping[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps")
    return [_mapping(step) for step in steps] if isinstance(steps, list) else []


def _action(step: Mapping[str, object]) -> str:
    uses = step.get("uses")
    return str(uses).split("@")[0] if isinstance(uses, str) else ""


def _workflow_files(directory: Path) -> list[Path]:
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


def _external_actions(directory: Path) -> list[str]:
    uses = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
    return [
        action
        for path in _workflow_files(directory)
        for action in uses.findall(path.read_text(encoding="utf-8"))
        if not action.startswith("./")
    ]


def _assert_thin_dagger(job: Mapping[str, object], args: str) -> None:
    steps = _steps(job)
    assert [_action(step) for step in steps] == [CHECKOUT_ACTION, DAGGER_ACTION]
    assert all(PINNED.fullmatch(str(step.get("uses"))) for step in steps)
    checkout = _mapping(steps[0].get("with"))
    invocation = _mapping(steps[1].get("with"))
    assert checkout.get("fetch-depth") == 0
    assert checkout.get("persist-credentials") is False
    assert invocation == {"version": "0.21.8", "verb": "call", "args": args}


def test_should_pin_every_external_action_in_yml_and_yaml() -> None:
    actions = _external_actions(WORKFLOWS)
    assert actions
    assert [action for action in actions if PINNED.fullmatch(action) is None] == []


def test_should_fail_pin_audit_for_a_yaml_bypass(tmp_path: Path) -> None:
    (tmp_path / "bypass.yaml").write_text(
        "jobs:\n  bypass:\n    steps:\n      - uses: actions/checkout@v4\n",
        encoding="utf-8",
    )
    assert _external_actions(tmp_path) == ["actions/checkout@v4"]


def test_should_route_pull_request_and_main_ci_only_through_dagger() -> None:
    document = _workflow("dagger.yml")
    job = _job(document, "dagger")
    _assert_thin_dagger(job, "ci --commit-sha=${{ github.sha }}")
    assert job.get("name") == "Dagger"


def test_should_route_scheduled_dependency_audit_with_exact_caller_identity() -> None:
    document = _workflow("security-audit.yml")
    _assert_thin_dagger(
        _job(document, "dependency-audit"),
        "dependency-audit --commit-sha=${{ github.sha }}",
    )


def test_should_make_release_manual_and_dagger_proven() -> None:
    document = _workflow("release-candidate.yml")
    triggers = _mapping(document.get("on"))
    candidate = _job(document, "candidate")
    steps = _steps(candidate)
    assert set(triggers) == {"workflow_dispatch"}
    assert [_action(step) for step in steps] == [CHECKOUT_ACTION, DAGGER_ACTION, UPLOAD_ACTION]
    assert all(PINNED.fullmatch(str(step.get("uses"))) for step in steps)
    invocation = _mapping(steps[1].get("with"))
    assert invocation.get("verb") == "call"
    assert str(invocation.get("args", "")).startswith("release-candidate ")
    assert "--commit-sha=${{ github.sha }}" in str(invocation.get("args"))
    assert "--workflow-run-id" not in str(invocation.get("args"))
    assert "--run-attempt" not in str(invocation.get("args"))
    assert "--github-token=env:GITHUB_TOKEN" in str(invocation.get("args"))
    assert "export --path=release" in str(invocation.get("args"))
    upload = _mapping(steps[2].get("with"))
    assert upload.get("name") == "edge-proc-${{ github.sha }}"


def test_should_keep_oidc_publisher_source_free_and_shell_free() -> None:
    document = _workflow("publish.yml")
    publish = _job(document, "publish")
    steps = _steps(publish)
    triggers = _mapping(document.get("on"))
    workflow_run = _mapping(triggers.get("workflow_run"))
    assert workflow_run.get("workflows") == ["Dagger release candidate"]
    assert workflow_run.get("types") == ["completed"]
    condition = str(publish.get("if", ""))
    assert "workflow_run.conclusion == 'success'" in condition
    assert "workflow_run.event == 'workflow_dispatch'" in condition
    assert "workflow_run.head_branch == github.event.repository.default_branch" in condition
    assert _mapping(publish.get("permissions")) == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert [_action(step) for step in steps] == [DOWNLOAD_ACTION, PUBLISH_ACTION]
    assert all("run" not in step for step in steps)
    download = _mapping(steps[0].get("with"))
    assert download.get("name") == "edge-proc-${{ github.event.workflow_run.head_sha }}"
    assert download.get("run-id") == "${{ github.event.workflow_run.id }}"
    assert download.get("github-token") == "${{ github.token }}"
    settings = _mapping(steps[1].get("with"))
    assert settings.get("packages-dir") == "release/dist"
    assert settings.get("attestations") is True


@pytest.mark.parametrize("workflow", ["ci.yml", "dagger-shadow.yml"])
def test_should_delete_superseded_ci_ingress(workflow: str) -> None:
    assert not (WORKFLOWS / workflow).exists()
