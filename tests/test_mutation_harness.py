"""Tests for the mutation harness — the instrument, not the code it measures.

A mutation harness reports a number that gates trust in every other test in this repo, so
it has to be shown capable of being WRONG. The portfolio has already shipped one harness
that reported 12/12 green while every run was crashing: it greppped stdout instead of
reading exit codes. These tests pin the three ways this one could lie:

- a mutation whose `old` text no longer matches must be an ERROR, never a silent skip;
- a pytest run that could not execute (exit 2) must be an ERROR, never a catch;
- a mutated source file must always come back byte-identical, even when the run explodes.

They use a temp directory and an injected runner: no real `edgeproc/` file is touched and
no real pytest is spawned, so the whole file runs in milliseconds.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_HARNESS_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mutation_harness.py"


def _load_harness() -> ModuleType:
    """Import the harness by path; `scripts/` is not an installed package.

    Registered in ``sys.modules`` BEFORE it executes because ``@dataclass`` resolves the
    defining module by name while the class body is being processed.
    """
    name = "mutation_harness_under_test"
    spec = importlib.util.spec_from_file_location(name, _HARNESS_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mh = _load_harness()

_SOURCE = "def guard(ok):\n    if not ok:\n        raise ValueError('refused')\n    return ok\n"


def _target(root: Path) -> Path:
    path = root / "pkg" / "guard.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SOURCE, encoding="utf-8")
    return path


def _mutation(**overrides: str) -> object:
    fields = {
        "id": "T-GUARD",
        "target": "pkg/guard.py",
        "old": "if not ok:",
        "new": "if ok:",
        "invariant": "the refusal fires",
    }
    fields.update(overrides)
    return mh.Mutation(**fields)


def _runner(returncode: int, probe: str | None = None, caught_by: str = ""):
    """A fake runner: records what it saw on disk, then returns a chosen exit code."""
    seen: dict[str, str] = {}

    def run(mutation: object, root: Path, baseline: bytes) -> object:
        seen["disk"] = (root / "pkg" / "guard.py").read_text(encoding="utf-8")
        seen["baseline"] = baseline.decode("utf-8")
        return mh.RunOutcome(returncode, mh.PROBE_OK if probe is None else probe, caught_by)

    run.seen = seen  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------------------
# Exit codes are the verdict
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (0, "SURVIVED"),
        (1, "CAUGHT"),
        (2, "ERROR"),
        (3, "ERROR"),
        (4, "ERROR"),
        (5, "ERROR"),
        (-9, "ERROR"),
    ],
)
def test_classify_scores_only_zero_and_one_as_measurements(returncode: int, expected: str) -> None:
    assert mh.classify(returncode) == expected


def test_collection_error_is_reported_as_error_not_caught(tmp_path: Path) -> None:
    """Exit 2 means pytest never judged the mutation. Scoring it CAUGHT inflates the ratio."""
    _target(tmp_path)
    result = mh.run_mutation(_mutation(), tmp_path, _runner(2))
    assert result.verdict == "ERROR"


def test_failing_suite_is_caught(tmp_path: Path) -> None:
    _target(tmp_path)
    result = mh.run_mutation(_mutation(), tmp_path, _runner(1, caught_by="tests/t.py::test_x"))
    assert result.verdict == "CAUGHT"
    assert result.detail == "tests/t.py::test_x"


def test_green_suite_is_survived(tmp_path: Path) -> None:
    _target(tmp_path)
    result = mh.run_mutation(_mutation(), tmp_path, _runner(0))
    assert result.verdict == "SURVIVED"


# --------------------------------------------------------------------------------------
# A mutation that cannot be placed is an ERROR, never a skip
# --------------------------------------------------------------------------------------


def test_missing_old_text_is_error_and_never_runs_the_suite(tmp_path: Path) -> None:
    """A stale `old` string that matches nothing must not be quietly dropped."""
    _target(tmp_path)
    runner = _runner(1)
    result = mh.run_mutation(_mutation(old="if not verified:"), tmp_path, runner)
    assert result.verdict == "ERROR"
    assert "not found" in result.detail
    assert runner.seen == {}


def test_non_unique_old_text_is_error(tmp_path: Path) -> None:
    """An ambiguous match would mutate more than one site and misattribute the result."""
    path = _target(tmp_path)
    path.write_text(_SOURCE + _SOURCE, encoding="utf-8")
    result = mh.run_mutation(_mutation(), tmp_path, _runner(1))
    assert result.verdict == "ERROR"
    assert "not unique" in result.detail


def test_missing_target_file_is_error(tmp_path: Path) -> None:
    result = mh.run_mutation(_mutation(target="pkg/gone.py"), tmp_path, _runner(1))
    assert result.verdict == "ERROR"
    assert "missing" in result.detail


# --------------------------------------------------------------------------------------
# The runtime probe gates the score
# --------------------------------------------------------------------------------------


def test_failed_probe_is_error_even_when_the_suite_went_red(tmp_path: Path) -> None:
    """A red run that never loaded the mutation is not evidence of a catch."""
    _target(tmp_path)
    runner = _runner(1, probe="interpreter loaded UNMUTATED bytecode")
    result = mh.run_mutation(_mutation(), tmp_path, runner)
    assert result.verdict == "ERROR"
    assert "UNMUTATED" in result.detail


def test_absent_probe_output_is_error(tmp_path: Path) -> None:
    _target(tmp_path)
    result = mh.run_mutation(_mutation(), tmp_path, _runner(0, probe=""))
    assert result.verdict == "ERROR"
    assert "no probe output" in result.detail


def test_probe_distinguishes_mutated_from_baseline_bytecode() -> None:
    """The probe's core comparison: a real edit must produce a code object the baseline cannot."""
    mutated = _SOURCE.replace("if not ok:", "if ok:")
    only_mutated = mh._fingerprints(mutated.encode()) - mh._fingerprints(_SOURCE.encode())
    assert only_mutated
    assert not (mh._fingerprints(_SOURCE.encode()) & only_mutated)


# --------------------------------------------------------------------------------------
# Restore is guaranteed
# --------------------------------------------------------------------------------------


def test_the_runner_sees_the_mutation_on_disk(tmp_path: Path) -> None:
    _target(tmp_path)
    runner = _runner(1)
    mh.run_mutation(_mutation(), tmp_path, runner)
    assert "if ok:" in runner.seen["disk"]
    assert "if not ok:" in runner.seen["baseline"]


def test_restore_returns_exact_original_bytes(tmp_path: Path) -> None:
    path = _target(tmp_path)
    mh.run_mutation(_mutation(), tmp_path, _runner(0))
    assert path.read_bytes() == _SOURCE.encode("utf-8")


def test_restore_survives_a_runner_that_raises(tmp_path: Path) -> None:
    """try/finally is the contract: an exception must never leave a mutated file behind."""
    path = _target(tmp_path)

    def exploding(mutation: object, root: Path, baseline: bytes) -> object:
        raise RuntimeError("pytest died")

    with pytest.raises(RuntimeError):
        mh.run_mutation(_mutation(), tmp_path, exploding)
    assert path.read_bytes() == _SOURCE.encode("utf-8")


def test_verify_restored_names_a_file_that_did_not_come_back(tmp_path: Path) -> None:
    """A clean git diff is not the check; the byte comparison is."""
    path = _target(tmp_path)
    baseline = {"pkg/guard.py": _SOURCE.encode("utf-8")}
    assert mh.verify_restored(tmp_path, baseline) == ()
    path.write_text("tampered\n", encoding="utf-8")
    assert mh.verify_restored(tmp_path, baseline) == ("pkg/guard.py",)


def test_purge_bytecode_removes_pycache(tmp_path: Path) -> None:
    cache = tmp_path / "edgeproc" / "sub" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "x.cpython-313.pyc").write_bytes(b"stale")
    assert mh.purge_bytecode(tmp_path) == 1
    assert not cache.exists()


# --------------------------------------------------------------------------------------
# The declared mutation table must stay honest
# --------------------------------------------------------------------------------------


def test_every_declared_mutation_still_matches_its_target_exactly_once() -> None:
    """Guards move. A mutation whose `old` text has drifted would score as an ERROR at
    best and be quietly ignored at worst, so drift is a gate failure here instead."""
    root = _HARNESS_PATH.resolve().parents[1]
    stale = [
        m.id
        for m in mh.MUTATIONS
        if (root / m.target).read_text(encoding="utf-8").count(m.old) != 1
    ]
    assert stale == []


def test_module_name_maps_target_path_to_dotted_module() -> None:
    assert mh.module_name("edgeproc/bundles/cas.py") == "edgeproc.bundles.cas"


def test_mutation_runs_exclude_this_files_own_bookkeeping_test() -> None:
    """Without this, every mutation is "caught" by the drift check above and the ratio
    reports 100% while measuring nothing about `edgeproc/` at all. It is the exact
    measuring-shape-not-property failure the harness exists to find."""
    argv = mh._pytest_argv(_mutation())
    assert f"--ignore={mh.SELF_TEST}" in argv
    assert "tests/" + Path(__file__).name == mh.SELF_TEST


def test_mutation_runs_disable_coverage_and_the_pytest_cache() -> None:
    """Coverage would let `--cov-fail-under` exit 1 for a reason that is not a caught
    mutation, and a warm cache would let one run's state leak into the next."""
    argv = mh._pytest_argv(_mutation())
    assert "--no-cov" in argv
    assert argv[argv.index("-p") + 1] == "no:cacheprovider"
    assert mh.PROBE_PLUGIN in argv
    assert "-x" in argv
