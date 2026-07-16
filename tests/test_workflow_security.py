"""GitHub Actions must resolve third-party code from immutable commits."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USES = re.compile(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)", re.MULTILINE)
PINNED = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def test_external_actions_are_pinned_to_full_commit_shas() -> None:
    failures: list[str] = []
    for workflow in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for action in USES.findall(workflow.read_text(encoding="utf-8")):
            if not action.startswith("./") and PINNED.fullmatch(action) is None:
                failures.append(f"{workflow.name}: {action}")
    assert failures == []
