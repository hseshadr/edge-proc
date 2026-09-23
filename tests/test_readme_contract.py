"""README contract: the first screen stays readable, true, and wired to real files.

This is the portfolio README template's contract test. It is deliberately dumb — string
and regex checks only, no markdown parser — because its job is to stop the first screen
drifting (tagline vs package metadata, badge creep, a missing label, a dead link), not to
judge prose.
"""

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
TAGLINE_LIMIT = 120
BADGE_LIMIT = 4
REQUIRED_LABELS = (
    "**What it does**",
    "**Who it's for**",
    "**What stays on your device / what leaves it**",
    "**Runs on**",
    "**Not for**",
    "**Status**",
)
ARCHITECTURE_MAP = "docs/architecture/index.html"
ARCHITECTURE_SOURCE = "docs/architecture/runtime.architecture.json"
# [text](target) — the target stops at whitespace or ")"; an optional "title" is ignored.
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def _first_screen() -> str:
    return README.split("## Try it in 60 seconds", maxsplit=1)[0]


def _tagline() -> str:
    lines = [line.strip() for line in README.splitlines()[1:]]
    return next(line for line in lines if line and not line.startswith("[!["))


def test_first_line_is_the_project_name() -> None:
    assert README.splitlines()[0] == "# EdgeProc"


def test_tagline_is_short_and_equals_the_package_description() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    tagline = _tagline()

    assert len(tagline) <= TAGLINE_LIMIT
    assert tagline == project["description"]


def test_first_screen_carries_at_most_four_badges() -> None:
    before_glance = README.split("## At a glance", maxsplit=1)[0]
    assert before_glance.count("[![") <= BADGE_LIMIT


@pytest.mark.parametrize("label", REQUIRED_LABELS)
def test_first_screen_has_every_at_a_glance_label(label: str) -> None:
    assert label in _first_screen()


def test_try_it_comes_before_how_it_works() -> None:
    assert README.index("## Try it in 60 seconds") < README.index("## How it works")


def test_hero_caption_precedes_the_example() -> None:
    caption = README.index("Real output of the example below")
    assert caption < README.index("## Try it in 60 seconds")


def test_readme_links_the_interactive_architecture_map() -> None:
    linked = [
        target
        for text, target in LINK.findall(README)
        if "Explore the interactive architecture map" in text
    ]
    assert linked == [ARCHITECTURE_MAP]
    assert (ROOT / ARCHITECTURE_SOURCE).is_file()


def _relative_targets() -> list[str]:
    targets = (target for _text, target in LINK.findall(README))
    return [
        target.split("#", maxsplit=1)[0]
        for target in targets
        if not re.match(r"^[a-z][a-z0-9+.-]*:", target) and not target.startswith("#")
    ]


def test_readme_has_relative_links_to_check() -> None:
    """Guards the link check below against a regex that silently matches nothing."""
    assert ARCHITECTURE_MAP in _relative_targets()


@pytest.mark.parametrize("target", sorted(set(_relative_targets())))
def test_every_relative_link_resolves(target: str) -> None:
    assert (ROOT / target).exists(), f"README links {target!r}, which is not in the repo"
