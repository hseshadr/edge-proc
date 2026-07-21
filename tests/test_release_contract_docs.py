"""Release-contract checks for security, privacy, reliability, and performance docs."""

import re
import sys
from pathlib import Path

import pytest

ROOT_FOR_IMPORT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_FOR_IMPORT / "benchmarks"))

from benchmark import BUDGETS  # noqa: E402

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


def test_budget_copy_distinguishes_admission_from_native_rss_enforcement() -> None:
    readme = _read("README.md")
    assert "MemoryManager" in readme
    assert "not an enforcement boundary for allocations inside FAISS" in readme


def test_operations_contract_links_a_repeatable_benchmark() -> None:
    operations = _read("docs/OPERATIONS.md")
    assert "benchmarks/benchmark.py" in operations
    assert (ROOT / "benchmarks/benchmark.py").is_file()


def test_settings_copy_matches_host_environment_behavior() -> None:
    readme = _read("README.md")
    assert "rejects unknown fields" not in readme
    assert "ignores unrelated host variables" in readme


# --- performance-claim drift guard ----------------------------------------------------
#
# The bug this exists to prevent: README claimed "55 ms cold-sync p95" while OPERATIONS.md
# said 111.0 ms. The 55 was that run's p50, mislabeled as a p95, and the two documents had
# silently diverged. Every assertion below compares COMMITTED CONSTANTS against COMMITTED
# TEXT — it never runs the benchmark — so it cannot fail on machine variance, only on a
# real documentation defect.

# The metric label in the OPERATIONS.md evidence table -> its key in benchmarks BUDGETS.
_METRIC_BUDGETS = {
    "vector search": "search_p95_ms",
    "cold sync": "cold_p95_ms",
    "warm sync": "warm_p95_ms",
}
# A documented p95 must sit at least this far under the budget the gate enforces. Wide
# enough that normal machine-to-machine spread never trips it; tight enough that a claim
# which has crept up to the gate's edge (where CI would start flaking) is caught first.
_REQUIRED_HEADROOM = 3.0
_EVIDENCE_ROW = re.compile(
    r"^\|\s*([a-z ]+?)\s*\|\s*([\d.]+) ms\s*\|\s*([\d.]+) ms\s*\|\s*([\d.]+) ms\s*\|$",
    re.MULTILINE,
)


def _documented_measurements() -> dict[str, tuple[float, float, float]]:
    """Parse the committed evidence table in OPERATIONS.md -> {metric: (p50, p95, budget)}."""
    return {
        metric: (float(p50), float(p95), float(budget))
        for metric, p50, p95, budget in _EVIDENCE_ROW.findall(_read("docs/OPERATIONS.md"))
    }


def test_operations_documents_every_benchmarked_metric() -> None:
    assert set(_documented_measurements()) == set(_METRIC_BUDGETS)


def test_documented_budgets_match_the_committed_benchmark_budgets() -> None:
    """The budget column is a copy of `benchmarks/benchmark.py` — it must stay a true copy."""
    for metric, (_, _, budget) in _documented_measurements().items():
        assert budget == BUDGETS[_METRIC_BUDGETS[metric]], (
            f"{metric}: doc budget {budget} != committed budget {BUDGETS[_METRIC_BUDGETS[metric]]}"
        )


@pytest.mark.parametrize("metric", sorted(_METRIC_BUDGETS))
def test_documented_p50_is_strictly_below_its_p95(metric: str) -> None:
    """The mislabel guard — this is the exact defect that shipped.

    A p50 copied into the p95 column lands as ``p50 == p95``, so the comparison is STRICT.
    Genuine measurements never tie here: these are floating-point millisecond timings, and
    a tie would mean at least half the samples came back bit-identical.
    """
    p50, p95, _ = _documented_measurements()[metric]
    assert p50 < p95, (
        f"{metric}: documented p50 {p50} ms is not below its p95 {p95} ms — "
        "a p50 was very likely pasted into the p95 column"
    )


@pytest.mark.parametrize("metric", sorted(_METRIC_BUDGETS))
def test_documented_p95_keeps_headroom_under_the_gate_budget(metric: str) -> None:
    p50, p95, budget = _documented_measurements()[metric]
    assert p95 * _REQUIRED_HEADROOM <= budget, (
        f"{metric}: documented p95 {p95} ms is within {_REQUIRED_HEADROOM}x of the "
        f"{budget} ms gate budget — re-measure and re-set the budget before CI flakes"
    )
    assert p50 > 0, f"{metric}: documented p50 must be a real measurement"


def test_readme_defers_percentile_figures_to_the_operations_contract() -> None:
    """ONE source of truth: percentile measurements live in OPERATIONS.md and nowhere else.

    README restating them is exactly how the 55-vs-111 drift happened, so the README must
    link to the contract rather than copy numbers out of it.
    """
    readme = _read("README.md")
    restated = [percentile for percentile in ("p50", "p95") if percentile in readme]
    assert restated == [], (
        f"README restates percentile figures {restated}; link to docs/OPERATIONS.md instead"
    )
