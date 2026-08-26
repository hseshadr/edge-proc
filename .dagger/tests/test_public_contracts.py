"""Behavioral contracts for EdgeProc's typed Dagger release graph."""

from __future__ import annotations

import inspect
from typing import cast

import dagger

from edge_proc.main import EdgeProc


class RecordingWorkspace:
    """Record the explicit source directory selected by the constructor."""

    def __init__(self) -> None:
        self.path = ""

    def directory(self, path: str, **_options: object) -> dagger.Directory:
        self.path = path
        return cast(dagger.Directory, object())


def test_should_select_explicit_root_when_constructing_release_graph() -> None:
    # Given
    workspace = RecordingWorkspace()

    # When
    EdgeProc.create(cast(dagger.Workspace, workspace))

    # Then
    assert workspace.path == "/"


def test_should_require_typed_workspace_when_constructing_release_graph() -> None:
    # Given
    signature = inspect.signature(EdgeProc.create, eval_str=True)

    # When
    workspace = signature.parameters.get("workspace")

    # Then
    assert workspace is not None
    assert workspace.annotation is dagger.Workspace


def test_should_expose_one_canonical_check_and_release_boundaries() -> None:
    # Given
    expected = {
        "ci",
        "quality",
        "dependency_audit",
        "secret_scan",
        "workflow_security",
        "release_candidate",
    }

    # When
    available = {name for name in expected if hasattr(EdgeProc, name)}

    # Then
    assert available == expected


def test_should_require_typed_secret_for_hosted_release_eligibility() -> None:
    # Given
    signature = inspect.signature(EdgeProc.release_candidate, eval_str=True)

    # When
    token = signature.parameters.get("github_token")
    result = signature.return_annotation

    # Then
    assert token is not None
    assert token.annotation is dagger.Secret
    assert result is dagger.Directory


def test_should_actionlint_both_github_workflow_extensions() -> None:
    # Given / When
    command = inspect.getsource(EdgeProc._workflow_security)

    # Then
    assert "*.yml" in command
    assert "*.yaml" in command
