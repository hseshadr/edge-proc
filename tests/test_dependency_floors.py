"""Security floors for the dependencies that perform fail-closed verification.

edge-proc's core promise is that an unsigned or tampered bundle is refused.
``cryptography`` is the library that actually performs that check, so a weak
floor here is a weak promise — and every project that depends on edge-proc
inherits it.

Two properties are guarded, and the second matters as much as the first:

1. The floor clears every known advisory. CVE-2026-69247 (PKCS#7 Bleichenbacher
   oracle) is only fixed in 50.0.0; CVE-2026-69248 and CVE-2026-69249 are fixed
   in 49.0.0. A ``>=44`` floor resolves happily onto all three.
2. There is no ceiling. A cap on a security-critical dependency turns the next
   major-delivered CVE fix into a blocked build — the upgrade you most need is
   the one the cap forbids. That is not hypothetical: a ``<49`` cap is what
   pinned a sibling project onto the vulnerable version in the first place.
"""

import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.specifiers import Specifier, SpecifierSet

ROOT = Path(__file__).resolve().parents[1]

# The first cryptography release clearing CVE-2026-69247, -69248 and -69249.
# Pinned as a literal on purpose: asserting the floor against itself would pass
# at any value.
MINIMUM_CRYPTOGRAPHY = "50"

_UPPER_BOUND_OPERATORS = frozenset({"<", "<=", "==", "===", "~="})


def _bundles_requirement(name: str) -> Requirement:
    """The parsed requirement for ``name`` in the ``bundles`` extra."""
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = manifest["project"]["optional-dependencies"]["bundles"]
    requirements = [Requirement(entry) for entry in extras]
    matches = [req for req in requirements if req.name == name]
    assert matches, f"{name} is not declared in the bundles extra"
    return matches[0]


def _lower_bounds(specifier: SpecifierSet) -> list[Specifier]:
    return [clause for clause in specifier if clause.operator == ">="]


def test_cryptography_floor_clears_every_known_advisory() -> None:
    """The floor must be >=50 — 49.0.0 still carries CVE-2026-69247."""
    bounds = _lower_bounds(_bundles_requirement("cryptography").specifier)
    assert len(bounds) == 1, f"expected exactly one >= clause, got {bounds}"
    assert bounds[0].version == MINIMUM_CRYPTOGRAPHY


def test_cryptography_floor_refuses_every_version_named_in_the_advisories() -> None:
    """Written as the predicate, not the shape: these versions must not resolve."""
    specifier = _bundles_requirement("cryptography").specifier
    for vulnerable in ("44.0.0", "46.0.7", "48.0.0", "48.0.1", "49.0.0"):
        assert vulnerable not in specifier, f"{vulnerable} is a known-vulnerable release"
    assert "50.0.0" in specifier


def test_cryptography_carries_no_ceiling() -> None:
    """A cap on the signing library blocks the CVE fix that arrives in a major."""
    specifier = _bundles_requirement("cryptography").specifier
    caps = [clause for clause in specifier if clause.operator in _UPPER_BOUND_OPERATORS]
    assert not caps, f"cryptography must stay uncapped, found {caps}"
