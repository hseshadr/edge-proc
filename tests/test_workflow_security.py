"""Workflow security invariants: immutable action pins, and provenance on every release.

Both are asserted against this repo's real ``.github/workflows``, and both are proven
falsifiable against synthetic fixtures — a guard nobody has watched fail is not a guard.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")
PYPI_PUBLISH = "pypa/gh-action-pypi-publish"
NO_ATTESTATIONS = "publish step does not set `with: attestations: true` (PEP 740)"
NO_OIDC = "publish job does not grant `permissions: id-token: write`"


def _workflow_files(directory: Path) -> list[Path]:
    """Every workflow file in ``directory``, under either extension GitHub accepts.

    Globs ``*.yaml`` as well as ``*.yml``: scanning only one extension lets a
    ``deploy.yaml`` smuggle an unpinned action or an unsigned release past a green test.
    """
    return sorted([*directory.glob("*.yml"), *directory.glob("*.yaml")])


def _audit(workflows: Path) -> tuple[list[str], int]:
    """Return (unpinned action refs, TOTAL action refs) across every workflow file.

    The ref count is returned so callers can prove the scan was not vacuous.
    """
    failures: list[str] = []
    total = 0
    for workflow in _workflow_files(workflows):
        for action in USES.findall(workflow.read_text(encoding="utf-8")):
            total += 1
            if not action.startswith("./") and PINNED.fullmatch(action) is None:
                failures.append(f"{workflow.name}: {action}")
    return failures, total


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    failures, total = _audit(WORKFLOWS)
    assert failures == []
    # Non-vacuity: zero refs means the scan found nothing to check, which must FAIL
    # rather than green-light the repo. A broken glob or a moved workflow dir lands here.
    assert total > 0, "workflow audit matched no action references — the scan is vacuous"


def test_audit_reports_zero_refs_when_there_is_nothing_to_scan(tmp_path: Path) -> None:
    """Proves the non-vacuity assertion above has teeth: an empty dir yields a zero count."""
    assert _audit(tmp_path) == ([], 0)


def test_audit_catches_an_unpinned_action_in_a_yaml_file(tmp_path: Path) -> None:
    """A ``.yaml`` workflow is scanned exactly like a ``.yml`` one — the glob hole."""
    (tmp_path / "deploy.yaml").write_text(
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n", encoding="utf-8"
    )
    assert _audit(tmp_path) == (["deploy.yaml: actions/checkout@v4"], 1)


# --- PEP 740 provenance: a release must be signed, and signable -----------------------
#
# `attestations: true` is what makes PyPI store a signed attestation next to the wheel,
# and `id-token: write` is what lets the job mint the OIDC token that signs it. Delete
# either line and releases keep succeeding — silently unsigned. Nothing else in this
# repo notices, so the assertions below are the only thing holding those two lines in.


class _PublishStep(NamedTuple):
    """One ``pypa/gh-action-pypi-publish`` step, reduced to the facts under audit."""

    where: str
    attested: bool
    oidc: bool


def _mapping(value: object) -> dict[str, object]:
    """``value`` as a string-keyed mapping; anything else reads as empty.

    Keys are stringified because YAML 1.1 parses a workflow's ``on:`` into the boolean
    key ``True`` — a lookup assuming plain string keys would trip over a real file.
    """
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _steps(job: Mapping[str, object]) -> list[dict[str, object]]:
    """The step list of ``job``, normalised to mappings."""
    steps = job.get("steps")
    return [_mapping(step) for step in steps] if isinstance(steps, list) else []


def _action(step: Mapping[str, object]) -> str:
    """The action a step runs, minus its ``@ref``. Empty for a plain ``run:`` step."""
    uses = step.get("uses")
    return uses.split("@")[0] if isinstance(uses, str) else ""


def _enabled(value: object) -> bool:
    """Whether an action input is switched on.

    Accepts the YAML boolean and the quoted string alike: GitHub coerces every input to
    a string, so ``attestations: "true"`` enables provenance exactly as ``true`` does.
    """
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _publish_steps(where: str, job: Mapping[str, object]) -> list[_PublishStep]:
    """The PyPI-publish steps of one job, each carrying that job's OIDC verdict.

    ``id-token: write`` must be granted *explicitly*. A blanket ``write-all`` would also
    confer it, but these workflows are least-privilege by policy, so it is not accepted.
    """
    oidc = _mapping(job.get("permissions")).get("id-token") == "write"
    return [
        _PublishStep(where, _enabled(_mapping(step.get("with")).get("attestations")), oidc)
        for step in _steps(job)
        if _action(step) == PYPI_PUBLISH
    ]


def _in_workflow(workflow: Path) -> list[_PublishStep]:
    """Every PyPI-publish step in one workflow file, across all of its jobs."""
    document = _mapping(yaml.safe_load(workflow.read_text(encoding="utf-8")))
    return [
        found
        for name, job in _mapping(document.get("jobs")).items()
        for found in _publish_steps(f"{workflow.name}:{name}", _mapping(job))
    ]


def _defects(step: _PublishStep) -> list[str]:
    """Every provenance invariant ``step`` breaks, as reader-facing failure lines."""
    broken: list[str] = []
    if not step.attested:
        broken.append(NO_ATTESTATIONS)
    if not step.oidc:
        broken.append(NO_OIDC)
    return [f"{step.where}: {reason}" for reason in broken]


def _audit_provenance(directory: Path) -> tuple[list[str], int]:
    """Return (provenance failures, PUBLISH STEPS examined) for ``directory``.

    The step count is returned for the same reason the ref count is above: renaming or
    deleting the publish step must turn this red, not quietly empty out the scan.
    """
    steps = [found for path in _workflow_files(directory) for found in _in_workflow(path)]
    return [defect for step in steps for defect in _defects(step)], len(steps)


OIDC_GRANTED = "    permissions:\n      id-token: write\n      contents: read\n"
NO_PERMISSIONS = ""
CONTENTS_READ_ONLY = "    permissions:\n      contents: read\n"
ATTESTED = "        with:\n          attestations: true\n"
ATTESTATIONS_OFF = "        with:\n          attestations: false\n"
ATTESTATIONS_MISSING = "        with:\n          print-hash: true\n"
ATTESTATIONS_ONLY_COMMENTED = (
    "        with:\n"
    "          # attestations: true  <- a comment configures nothing\n"
    "          print-hash: true\n"
)


def _workflow(permissions: str, publish_with: str) -> str:
    """A one-job publish workflow, parameterised on the two blocks under audit."""
    return (
        'name: Publish\non:\n  push:\n    tags: ["v*"]\n'
        "jobs:\n  publish:\n"
        f"{permissions}"
        "    steps:\n"
        f"      - uses: {PYPI_PUBLISH}@ba38be9e461d3875417946c167d0b5f3d385a247\n"
        f"{publish_with}"
    )


def test_the_pypi_publish_step_carries_pep740_provenance() -> None:
    """This repo's real release path is signed, and the scan actually found it."""
    failures, publish_steps = _audit_provenance(WORKFLOWS)
    assert failures == []
    assert publish_steps > 0, "vacuous scan: no PyPI publish step was examined"


@pytest.mark.parametrize(
    ("permissions", "publish_with", "reason"),
    [
        pytest.param(OIDC_GRANTED, ATTESTATIONS_OFF, NO_ATTESTATIONS, id="attestations-false"),
        pytest.param(OIDC_GRANTED, ATTESTATIONS_MISSING, NO_ATTESTATIONS, id="attestations-absent"),
        pytest.param(
            OIDC_GRANTED,
            ATTESTATIONS_ONLY_COMMENTED,
            NO_ATTESTATIONS,
            id="attestations-only-in-a-comment",
        ),
        pytest.param(NO_PERMISSIONS, ATTESTED, NO_OIDC, id="no-permissions-block"),
        pytest.param(CONTENTS_READ_ONLY, ATTESTED, NO_OIDC, id="contents-read-only"),
    ],
)
def test_the_provenance_audit_rejects_an_unsigned_release_path(
    tmp_path: Path, permissions: str, publish_with: str, reason: str
) -> None:
    """Break the property, not the form: each fixture is a real way to lose provenance.

    ``attestations-only-in-a-comment`` is the load-bearing case — the file contains the
    literal text ``attestations: true``, but only in a comment, so a grep-based check
    would wave it through. This audit parses YAML, so it does not.
    """
    (tmp_path / "publish.yml").write_text(_workflow(permissions, publish_with), encoding="utf-8")
    assert _audit_provenance(tmp_path) == ([f"publish.yml:publish: {reason}"], 1)


def test_the_provenance_audit_accepts_a_correctly_signed_release_path(tmp_path: Path) -> None:
    """Guard the opposite direction: a correct workflow must not be flagged."""
    (tmp_path / "publish.yml").write_text(_workflow(OIDC_GRANTED, ATTESTED), encoding="utf-8")
    assert _audit_provenance(tmp_path) == ([], 1)


def test_a_workflow_that_never_publishes_is_not_flagged(tmp_path: Path) -> None:
    """Only the release path is in scope — a test job with no OIDC scope is fine."""
    (tmp_path / "ci.yaml").write_text(
        "jobs:\n  test:\n    steps:\n      - run: pytest\n", encoding="utf-8"
    )
    assert _audit_provenance(tmp_path) == ([], 0)
