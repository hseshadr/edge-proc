"""Fail-closed identity checks for Dagger-built EdgeProc release candidates."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import tarfile
import zipfile
from collections.abc import Callable, Sequence
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Final
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict

PROJECT: Final = "edge-proc"
SHA_PATTERN: Final = re.compile(r"[0-9a-f]{40}")
ARCHIVE_COUNT: Final = 2


class ReleaseContractError(ValueError):
    """A release candidate cannot prove its exact identity."""


class CheckRun(BaseModel):
    """The hosted-check fields needed for exact release eligibility."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    head_sha: str
    conclusion: str | None


class HostedPayload(BaseModel):
    """The exact main identity and hosted checks observed from GitHub."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    main_sha: str
    check_runs: tuple[CheckRun, ...]


def _assigned_version(node: ast.stmt) -> object | None:
    if not isinstance(node, ast.Assign):
        return None
    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
    return ast.literal_eval(node.value) if "__version__" in names else None


def _source_version(root: Path) -> str:
    tree = ast.parse((root / "edgeproc/_version.py").read_text(encoding="utf-8"))
    values = [value for node in tree.body if (value := _assigned_version(node)) is not None]
    if len(values) != 1 or not isinstance(values[0], str):
        raise ReleaseContractError("release identity: source must assign one string version")
    return values[0]


def _stable_version(root: Path) -> str:
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    headings = re.findall(r"^## \[([^]]+)]", changelog, flags=re.MULTILINE)
    stable = next((heading for heading in headings if heading.lower() != "unreleased"), "")
    if not stable:
        raise ReleaseContractError("release identity: changelog has no stable release")
    return stable


def validate_source_identity(root: Path, tag: str) -> None:
    """Require the tag, source version, and top stable changelog entry to agree."""
    version = _source_version(root)
    if tag != f"v{version}" or _stable_version(root) != version:
        raise ReleaseContractError("release identity: tag, source, and changelog differ")


def _only[T](paths: Sequence[T], kind: str) -> T:
    if len(paths) != 1:
        raise ReleaseContractError(
            f"distribution metadata: expected one {kind}, found {len(paths)}"
        )
    return paths[0]


def _wheel_metadata(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        member = _only([Path(name) for name in members], "wheel METADATA")
        return archive.read(str(member))


def _sdist_metadata(path: Path) -> bytes:
    with tarfile.open(path, "r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name.endswith("/PKG-INFO")]
        member = _only(members, "sdist PKG-INFO")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise ReleaseContractError("distribution metadata: cannot read sdist PKG-INFO")
        return extracted.read()


def _metadata_matches(payload: bytes, version: str) -> bool:
    metadata: Message = BytesParser().parsebytes(payload)
    name = re.sub(r"[-_.]+", "-", metadata.get("Name", "")).lower()
    return name == PROJECT and metadata.get("Version") == version


def _distribution_payloads(dist: Path) -> tuple[bytes, bytes]:
    wheel = _only(sorted(dist.glob("*.whl")), "wheel")
    sdist = _only(sorted(dist.glob("*.tar.gz")), "sdist")
    return _wheel_metadata(wheel), _sdist_metadata(sdist)


def validate_distributions(root: Path, dist: Path, tag: str) -> None:
    """Require both built archives to carry the exact source project and version."""
    validate_source_identity(root, tag)
    version = tag.removeprefix("v")
    if not all(_metadata_matches(payload, version) for payload in _distribution_payloads(dist)):
        raise ReleaseContractError("distribution metadata: project or version differs")


def _valid_sha(value: str) -> bool:
    return SHA_PATTERN.fullmatch(value) is not None


def _is_green_dagger(run: CheckRun, expected_sha: str) -> bool:
    return run.name == "Dagger" and run.head_sha == expected_sha and run.conclusion == "success"


def validate_hosted_eligibility(payload: str, expected_sha: str) -> None:
    """Require exact current main and a successful Dagger check on that commit."""
    observed = HostedPayload.model_validate_json(payload)
    identity_matches = _valid_sha(expected_sha) and observed.main_sha == expected_sha
    matching = any(_is_green_dagger(run, expected_sha) for run in observed.check_runs)
    if not identity_matches or not matching:
        raise ReleaseContractError("hosted eligibility: exact main lacks a green Dagger check")


def _github_json(url: str, token: str) -> dict[str, object]:
    request = Request(  # noqa: S310 -- caller supplies a fixed GitHub API origin
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 -- fixed GitHub API origin
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ReleaseContractError("hosted eligibility: GitHub returned a non-object")
    return payload


def _check_run(value: object) -> CheckRun:
    if not isinstance(value, dict):
        raise ReleaseContractError("hosted eligibility: GitHub check runs are invalid")
    fields = ("name", "head_sha", "conclusion")
    return CheckRun.model_validate({field: value.get(field) for field in fields})


def _check_runs(value: object) -> tuple[CheckRun, ...]:
    if not isinstance(value, list):
        raise ReleaseContractError("hosted eligibility: GitHub check runs are invalid")
    return tuple(_check_run(item) for item in value)


def fetch_hosted_payload(repository: str, commit: str, token: str) -> str:
    """Fetch and reduce GitHub state to the strict release eligibility schema."""
    api = f"https://api.github.com/repos/{repository}"
    main = _github_json(f"{api}/commits/main", token)
    checks = _github_json(f"{api}/commits/{commit}/check-runs?per_page=100", token)
    if not isinstance(main.get("sha"), str):
        raise ReleaseContractError("hosted eligibility: GitHub response fields are invalid")
    selected = _check_runs(checks.get("check_runs"))
    return HostedPayload(main_sha=str(main["sha"]), check_runs=selected).model_dump_json()


def write_checksums(dist: Path, output: Path) -> None:
    """Record literal SHA-256 identities for the two validated archives."""
    archives = sorted([*dist.glob("*.whl"), *dist.glob("*.tar.gz")])
    if len(archives) != ARCHIVE_COUNT:
        raise ReleaseContractError("distribution metadata: expected two candidate archives")
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  dist/{path.name}" for path in archives
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("identity", "distributions"):
        child = subparsers.add_parser(command)
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("--tag", required=True)
    subparsers.choices["distributions"].add_argument("--dist", type=Path, required=True)
    hosted = subparsers.add_parser("hosted")
    hosted.add_argument("--payload", type=Path, required=True)
    hosted.add_argument("--sha", required=True)
    github = subparsers.add_parser("github")
    github.add_argument("--repository", required=True)
    github.add_argument("--sha", required=True)
    checksums = subparsers.add_parser("checksums")
    checksums.add_argument("--dist", type=Path, required=True)
    checksums.add_argument("--output", type=Path, required=True)
    return parser


def _identity(args: argparse.Namespace) -> None:
    validate_source_identity(args.root, args.tag)


def _distributions(args: argparse.Namespace) -> None:
    validate_distributions(args.root, args.dist, args.tag)


def _hosted(args: argparse.Namespace) -> None:
    validate_hosted_eligibility(args.payload.read_text(encoding="utf-8"), args.sha)


def _github(args: argparse.Namespace) -> None:
    token = os.environ.get("GITHUB_TOKEN", "")
    payload = fetch_hosted_payload(args.repository, args.sha, token)
    validate_hosted_eligibility(payload, args.sha)


def _checksums(args: argparse.Namespace) -> None:
    write_checksums(args.dist, args.output)


def main() -> None:
    """Validate one release boundary selected by the CLI subcommand."""
    args = _parser().parse_args()
    handlers: dict[str, Callable[[argparse.Namespace], None]] = {
        "identity": _identity,
        "distributions": _distributions,
        "hosted": _hosted,
        "github": _github,
        "checksums": _checksums,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
