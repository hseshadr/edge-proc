"""Release-contract checks for security, privacy, reliability, and performance docs."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "heading",
    [
        "## Threat model and trust boundaries",
        "## Privacy and data flow",
        "## Reliability and recovery contract",
        "## Measured performance contract",
    ],
)
def test_operations_contract_exposes_distinguished_engineer_gates(heading: str) -> None:
    assert heading in _read("docs/OPERATIONS.md")


def test_readme_links_the_operations_contract() -> None:
    assert "docs/OPERATIONS.md" in _read("README.md")


def test_budget_copy_does_not_claim_unimplemented_enforcement() -> None:
    readme = _read("README.md")
    assert "declaration, not an enforcement boundary" in readme


def test_operations_contract_links_a_repeatable_benchmark() -> None:
    operations = _read("docs/OPERATIONS.md")
    assert "benchmarks/northstar.py" in operations
    assert (ROOT / "benchmarks/northstar.py").is_file()


def test_settings_copy_matches_host_environment_behavior() -> None:
    readme = _read("README.md")
    assert "rejects unknown fields" not in readme
    assert "ignores unrelated host variables" in readme
