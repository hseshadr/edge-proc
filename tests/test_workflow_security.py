"""GitHub Actions must resolve third-party code from immutable commits."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def _audit(workflows: Path) -> tuple[list[str], int]:
    """Return (unpinned action refs, TOTAL action refs) across every workflow file.

    Globs ``*.yaml`` as well as ``*.yml``: GitHub Actions accepts both, so scanning only
    one extension lets a ``deploy.yaml`` smuggle in an unpinned action past a green test.
    The ref count is returned so callers can prove the scan was not vacuous.
    """
    failures: list[str] = []
    total = 0
    for workflow in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        for action in USES.findall(workflow.read_text(encoding="utf-8")):
            total += 1
            if not action.startswith("./") and PINNED.fullmatch(action) is None:
                failures.append(f"{workflow.name}: {action}")
    return failures, total


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    failures, total = _audit(ROOT / ".github/workflows")
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
