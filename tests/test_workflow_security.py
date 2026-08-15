"""Workflow security invariants: immutable action pins, and provenance on every release.

Both are asserted against this repo's real ``.github/workflows``, and both are proven
falsifiable against synthetic fixtures — a guard nobody has watched fail is not a guard.
"""

import hashlib
import io
import os
import re
import subprocess
import tarfile
import zipfile
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
UPLOAD_ARTIFACT = "actions/upload-artifact"
DOWNLOAD_ARTIFACT = "actions/download-artifact"
NO_ATTESTATIONS = "publish step does not set `with: attestations: true` (PEP 740)"
NO_OIDC = "publish job does not grant `permissions: id-token: write`"
PUBLISH_WORKFLOW = WORKFLOWS / "publish.yml"
RELEASE_COMMANDS = (
    "uv run poe gate",
    "bash examples/run_loop.sh",
    "uv run python benchmarks/benchmark.py",
)
RELEASE_JOBS = {"release-eligibility"}
DEPENDENCY_AUDIT_WORKFLOW = (
    "hseshadr/ci/.github/workflows/security-audit.yml@605e51cbc86f452b56edcf1c9660921da797cbfe"
)
INLINE_DEPENDENCY_AUDIT = ("uv export --frozen", "uvx pip-audit")
GITLEAKS_ACTION = "gitleaks/gitleaks-action"
RELEASE_SCAN_COMMAND = 'gitleaks detect --redact --no-banner --source . --log-opts="--all"'
RELEASE_SCAN_STEP = "Scan release history and tree for secrets"
SOURCE_IDENTITY_STEP = "Verify source release identity"
HOSTED_ELIGIBILITY_STEP = "Verify exact main and hosted checks"
BUILT_METADATA_STEP = "Verify built distribution metadata"
DOWNLOADED_ARTIFACT_STEP = "Verify downloaded release artifact"
REGISTRY_VERIFY_STEP = "Verify the release is actually on PyPI"


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


def _step_names(job: Mapping[str, object]) -> set[str]:
    """Every explicit step name in one workflow job."""
    return {str(step["name"]) for step in _steps(job) if "name" in step}


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


def _workflow_document(path: Path = PUBLISH_WORKFLOW) -> dict[str, object]:
    """Load one workflow with YAML's boolean ``on`` quirk normalised away."""
    return _mapping(yaml.safe_load(path.read_text(encoding="utf-8")))


def _job(document: Mapping[str, object], name: str) -> dict[str, object]:
    """Return a named workflow job, or an empty mapping when it is absent."""
    return _mapping(_mapping(document.get("jobs")).get(name))


def _needs(job: Mapping[str, object]) -> set[str]:
    """Normalise a job's dependency declaration to a set of job names."""
    value = job.get("needs")
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value} if isinstance(value, list) else set()


def _named_run(job: Mapping[str, object], name: str) -> str:
    """Return the executable body of one named step, ignoring comments."""
    matches = [step.get("run") for step in _steps(job) if step.get("name") == name]
    return matches[0] if len(matches) == 1 and isinstance(matches[0], str) else ""


def _missing_release_commands(job: Mapping[str, object]) -> list[str]:
    """Commands from the documented release contract absent from executable steps."""
    lines = [
        line.strip()
        for step in _steps(job)
        for line in str(step.get("run", "")).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return [
        command
        for command in RELEASE_COMMANDS
        if not any(line.startswith(command) for line in lines)
    ]


def _release_contract_defects(document: Mapping[str, object]) -> list[str]:
    """Report checks whose absence could let a tag upload ineligible bytes."""
    eligible = _job(document, "release-eligibility")
    publish = _job(document, "publish")
    defects = [
        f"missing executable release check: {item}" for item in _missing_release_commands(eligible)
    ]
    defects.extend(_secret_scan_defects(eligible))
    defects.extend(_hosted_check_defects(eligible))
    if not RELEASE_JOBS.issubset(_needs(publish)):
        defects.append("publish does not wait for every release eligibility job")
    return defects


def _secret_scan_defects(eligible: Mapping[str, object]) -> list[str]:
    actions = {_action(step) for step in _steps(eligible)}
    if GITLEAKS_ACTION not in actions or RELEASE_SCAN_COMMAND not in _named_run(
        eligible, RELEASE_SCAN_STEP
    ):
        return ["release secret scan is absent"]
    return []


def _hosted_check_defects(eligible: Mapping[str, object]) -> list[str]:
    hosted = _named_run(eligible, HOSTED_ELIGIBILITY_STEP)
    hosted_invariants = (
        "refs/remotes/origin/main",
        '"gate"',
        '"Secret scan / gitleaks"',
        '"pip-audit"',
    )
    return [
        f"hosted release eligibility omits {invariant}"
        for invariant in hosted_invariants
        if invariant not in hosted
    ]


def _run_validator(script: str, directory: Path, tag: str) -> subprocess.CompletedProcess[str]:
    """Execute a workflow validator exactly as bash would on a tag runner."""
    environment = {**os.environ, "GITHUB_REF_NAME": tag}
    return subprocess.run(  # noqa: S603 - the script is the trusted workflow under test.
        ["/bin/bash", "-c", script],
        cwd=directory,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _write_release_source(directory: Path, version: str, changelog: str) -> None:
    """Write the two source-of-truth files consumed by the identity validator."""
    package = directory / "edgeproc"
    package.mkdir()
    (package / "_version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (directory / "CHANGELOG.md").write_text(changelog, encoding="utf-8")


def _metadata(name: str, version: str) -> bytes:
    """Minimal valid core metadata for a synthetic distribution artifact."""
    return f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n\n".encode()


def _write_wheel(directory: Path, name: str, version: str) -> None:
    """Create a wheel-shaped archive carrying controlled project metadata."""
    wheel = directory / "dist" / f"edge_proc-{version}-py3-none-any.whl"
    wheel.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"edge_proc-{version}.dist-info/METADATA", _metadata(name, version))


def _write_sdist(directory: Path, name: str, version: str) -> None:
    """Create an sdist-shaped archive carrying controlled project metadata."""
    sdist = directory / "dist" / f"edge_proc-{version}.tar.gz"
    payload = _metadata(name, version)
    member = tarfile.TarInfo(f"edge_proc-{version}/PKG-INFO")
    member.size = len(payload)
    with tarfile.open(sdist, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(payload))


def _write_distributions(directory: Path, name: str, version: str) -> None:
    """Write both artifact forms uploaded by the release workflow."""
    _write_wheel(directory, name, version)
    _write_sdist(directory, name, version)


def _write_digest_manifest(directory: Path) -> None:
    """Record the two release archive digests in coreutils check format."""
    archives = sorted((directory / "dist").iterdir())
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  dist/{path.name}" for path in archives
    ]
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n")


def _validator(job: str, step: str) -> str:
    """One named validator from the real release workflow."""
    return _named_run(_job(_workflow_document(), job), step)


def test_tag_publish_waits_for_every_documented_release_check() -> None:
    """Removing any exact-ref eligibility lane must block the upload job."""
    assert _release_contract_defects(_workflow_document()) == []


def test_tag_publish_delegates_dependency_audit_before_release_eligibility() -> None:
    """A release cannot bypass or duplicate the shared exact-ref dependency audit."""
    document = _workflow_document()
    audit = _job(document, "dependency-audit")
    eligible = _job(document, "release-eligibility")

    assert audit.get("uses") == DEPENDENCY_AUDIT_WORKFLOW
    assert _mapping(audit.get("with")).get("run-python-audit") is True
    assert _mapping(audit.get("permissions")) == {"contents": "read"}
    assert _needs(eligible) == {"dependency-audit"}
    commands = "\n".join(str(step.get("run", "")) for step in _steps(eligible))
    assert not any(command in commands for command in INLINE_DEPENDENCY_AUDIT)


def test_tag_publish_requires_the_exact_protected_main_commit() -> None:
    """A tag on an unmerged branch cannot reach the OIDC-bearing upload job."""
    script = _validator("release-eligibility", HOSTED_ELIGIBILITY_STEP)
    assert "tagged_sha" in script
    assert "main_sha" in script
    assert '"${tagged_sha}" != "${main_sha}"' in script


def test_source_identity_validator_accepts_the_matching_release(tmp_path: Path) -> None:
    """The exact tag, source version, and first stable changelog release agree."""
    _write_release_source(tmp_path, "0.4.1", "# Changelog\n\n## [Unreleased]\n\n## [0.4.1]\n")
    script = _validator("release-eligibility", SOURCE_IDENTITY_STEP)

    assert script, "release workflow has no source identity validator"
    result = _run_validator(script, tmp_path, "v0.4.1")

    assert result.returncode == 0, result.stderr


def test_source_identity_validator_rejects_a_mismatched_tag(tmp_path: Path) -> None:
    """A typoed or stale tag cannot build the source version under another name."""
    _write_release_source(tmp_path, "0.4.1", "## [0.4.1]\n")
    script = _validator("release-eligibility", SOURCE_IDENTITY_STEP)

    assert script, "release workflow has no source identity validator"
    result = _run_validator(script, tmp_path, "v0.4.0")

    assert result.returncode != 0
    assert "tag" in result.stderr.lower()


def test_source_identity_validator_rejects_a_stale_changelog(tmp_path: Path) -> None:
    """A release tag cannot jump ahead of the first stable changelog section."""
    _write_release_source(tmp_path, "0.4.1", "## [Unreleased]\n\n## [0.4.0]\n")
    script = _validator("release-eligibility", SOURCE_IDENTITY_STEP)

    assert script, "release workflow has no source identity validator"
    result = _run_validator(script, tmp_path, "v0.4.1")

    assert result.returncode != 0
    assert "changelog" in result.stderr.lower()


def test_built_metadata_validator_accepts_matching_artifacts(tmp_path: Path) -> None:
    """Both distribution formats identify the tagged project and version."""
    _write_release_source(tmp_path, "0.4.1", "## [0.4.1]\n")
    _write_distributions(tmp_path, "edge-proc", "0.4.1")
    script = _validator("release-eligibility", BUILT_METADATA_STEP)

    assert script, "release workflow has no built-metadata validator"
    result = _run_validator(script, tmp_path, "v0.4.1")

    assert result.returncode == 0, result.stderr


def test_built_metadata_validator_rejects_the_wrong_project(tmp_path: Path) -> None:
    """An artifact for another project can never reach the trusted upload action."""
    _write_release_source(tmp_path, "0.4.1", "## [0.4.1]\n")
    _write_distributions(tmp_path, "edgeproc-impostor", "0.4.1")
    script = _validator("release-eligibility", BUILT_METADATA_STEP)

    assert script, "release workflow has no built-metadata validator"
    result = _run_validator(script, tmp_path, "v0.4.1")

    assert result.returncode != 0
    assert "project" in result.stderr.lower()


def test_built_metadata_validator_rejects_the_wrong_version(tmp_path: Path) -> None:
    """A backend metadata drift cannot upload artifacts under the wrong version."""
    _write_release_source(tmp_path, "0.4.1", "## [0.4.1]\n")
    _write_distributions(tmp_path, "edge-proc", "0.4.0")
    script = _validator("release-eligibility", BUILT_METADATA_STEP)

    assert script, "release workflow has no built-metadata validator"
    result = _run_validator(script, tmp_path, "v0.4.1")

    assert result.returncode != 0
    assert "version" in result.stderr.lower()


def test_built_metadata_validation_precedes_the_oidc_upload() -> None:
    """The credentialed job consumes only eligibility-validated release artifacts."""
    eligible = _job(_workflow_document(), "release-eligibility")
    publish = _job(_workflow_document(), "publish")
    steps = _steps(publish)
    metadata = next(
        (index for index, step in enumerate(steps) if step.get("name") == DOWNLOADED_ARTIFACT_STEP),
        -1,
    )
    upload = next((index for index, step in enumerate(steps) if _action(step) == PYPI_PUBLISH), -1)

    assert BUILT_METADATA_STEP in _step_names(eligible)
    assert _needs(publish) == {"release-eligibility"}
    assert metadata >= 0
    assert upload > metadata


def _assert_minimal_oidc_job(publish: Mapping[str, object]) -> None:
    actions = {_action(step) for step in _steps(publish)} - {""}
    assert {DOWNLOAD_ARTIFACT, PYPI_PUBLISH} == actions
    assert _step_names(publish) == {
        "Download verified release artifact",
        DOWNLOADED_ARTIFACT_STEP,
        "Publish to PyPI (OIDC Trusted Publishing)",
    }


def test_oidc_job_cannot_checkout_install_build_or_verify_the_registry() -> None:
    """Only immutable-artifact verification and official upload run with PyPI OIDC."""
    document = _workflow_document()
    eligible = _job(document, "release-eligibility")
    verify = _job(document, "verify-published")
    _assert_minimal_oidc_job(_job(document, "publish"))
    assert {UPLOAD_ARTIFACT} <= {_action(step) for step in _steps(eligible)}
    assert {"Build sdist + wheel", BUILT_METADATA_STEP} <= _step_names(eligible)
    assert REGISTRY_VERIFY_STEP in _step_names(verify)
    assert _needs(verify) == {"publish"}
    assert all(
        _mapping(job.get("permissions")).get("id-token") != "write" for job in (eligible, verify)
    )


def test_downloaded_artifact_validator_accepts_unchanged_archives(tmp_path: Path) -> None:
    """The OIDC job independently verifies digest, project, and tagged version."""
    release = tmp_path / "release"
    release.mkdir()
    _write_distributions(release, "edge-proc", "0.4.1")
    _write_digest_manifest(release)

    result = _run_validator(_validator("publish", DOWNLOADED_ARTIFACT_STEP), tmp_path, "v0.4.1")

    assert result.returncode == 0, result.stderr


def test_downloaded_artifact_validator_rejects_transit_mutation(tmp_path: Path) -> None:
    """Artifact-service or handoff corruption cannot reach the official upload action."""
    release = tmp_path / "release"
    release.mkdir()
    _write_distributions(release, "edge-proc", "0.4.1")
    _write_digest_manifest(release)
    wheel = next((release / "dist").glob("*.whl"))
    wheel.write_bytes(wheel.read_bytes() + b"changed after eligibility")

    result = _run_validator(_validator("publish", DOWNLOADED_ARTIFACT_STEP), tmp_path, "v0.4.1")

    assert result.returncode != 0


def test_checksum_manifest_is_not_in_the_pypi_packages_directory() -> None:
    """The official publisher receives distributions only, never the handoff manifest."""
    document = _workflow_document()
    eligible = _job(document, "release-eligibility")
    publish = _job(document, "publish")
    record = _named_run(eligible, "Record release artifact digests")
    upload = next(step for step in _steps(publish) if _action(step) == PYPI_PUBLISH)

    assert "> SHA256SUMS" in record
    assert _mapping(upload.get("with")).get("packages-dir") == "release/dist"
