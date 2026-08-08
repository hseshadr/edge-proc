#!/usr/bin/env python3
"""Mutation harness: break a guard on purpose and see whether the suite notices.

WHY this exists. A green suite proves the tests ran, not that they can fail. Every hole
this codebase has had was a refusal path -- an ``if not verified: raise`` -- that no test
ever drove. So this script edits one real guard at a time, runs the suite, and records
CAUGHT (the suite went red) or SURVIVED (the suite stayed green). A survivor names a line
nothing is guarding.

Two instrument checks make the ratio trustworthy, because a harness that cannot fail is
the same defect it is hunting:

1. The verdict comes from pytest's EXIT CODE, never from grepped output. Exit 1 is a
   caught mutation. Exit 2/3/4/5 mean pytest could not run at all, and those are reported
   as ERROR -- never scored as a catch.
2. Every run carries an in-process probe that proves the interpreter running the tests
   loaded the MUTATED bytecode. CPython invalidates a ``.pyc`` on ``(mtime, size)``, so a
   same-size edit written inside one mtime tick can leave the ORIGINAL bytecode executing;
   the mutation would then report "survived" without ever having run.

Run it:  ``uv run poe mutants``
"""

from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import CodeType, ModuleType
from typing import Final

# --------------------------------------------------------------------------------------
# Contracts
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Mutation:
    """One deliberate defect, declared as data rather than guessed by a regex.

    ``old`` must appear EXACTLY once in ``target``; a mutation that cannot be placed
    unambiguously is an ERROR, not a silent skip. ``tests`` optionally narrows the pytest
    run; empty means the whole suite.
    """

    id: str
    target: str
    old: str
    new: str
    invariant: str
    tests: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one test run reported: its exit code, and whether the mutation was loaded."""

    returncode: int
    probe: str
    caught_by: str = ""


@dataclass(frozen=True, slots=True)
class Result:
    """Scored outcome for one mutation."""

    mutation_id: str
    target: str
    verdict: str
    detail: str


#: A runner takes the mutation, the repo root, and the UNMUTATED source of the target.
#: The baseline bytes are passed in rather than re-read, because by the time the runner is
#: called the file on disk already carries the defect.
Runner = Callable[[Mutation, Path, bytes], RunOutcome]

CAUGHT: Final = "CAUGHT"
SURVIVED: Final = "SURVIVED"
ERROR: Final = "ERROR"

PROBE_OK: Final = "OK"
PROBE_PLUGIN: Final = "mutation_harness"

ENV_MODULE: Final = "EDGEPROC_MUT_MODULE"
ENV_BASELINE: Final = "EDGEPROC_MUT_BASELINE"
ENV_OUT: Final = "EDGEPROC_MUT_OUT"

#: Only these trees are swept for bytecode. Sweeping the repo root would walk `.venv`.
_CODE_DIRS: Final = ("edgeproc", "tests", "scripts")

#: This harness's own tests are excluded from every mutation run. They assert that each
#: declared `old` string still matches its target exactly once -- which a live mutation
#: makes false by construction. Left in, it goes red for EVERY mutation and hands back a
#: 100% catch ratio that measures the harness's bookkeeping instead of any guard.
SELF_TEST: Final = "tests/test_mutation_harness.py"

# --------------------------------------------------------------------------------------
# The mutations
# --------------------------------------------------------------------------------------

MUTATIONS: Final[tuple[Mutation, ...]] = (
    Mutation(
        id="MS-EGRESS-REFUSAL",
        target="edgeproc/localvec/model_source.py",
        old="if not settings.allow_model_download:",
        new="if settings.allow_model_download:",
        invariant="no local model + no opt-in must REFUSE, never fetch from the hub",
    ),
    Mutation(
        id="MS-LOCAL-ONLY",
        target="edgeproc/localvec/model_source.py",
        old="reference=str(path), local_files_only=True",
        new="reference=str(path), local_files_only=False",
        invariant="a configured local model path is pinned to on-disk files",
    ),
    Mutation(
        id="MS-DIGEST-PIN-SKIP",
        target="edgeproc/localvec/model_source.py",
        old="if expected is None:",
        new="if expected is not None:",
        invariant="a configured EDGEPROC_MODEL_DIGEST is actually compared",
    ),
    Mutation(
        id="MS-PATH-NOT-DIR",
        target="edgeproc/localvec/model_source.py",
        old="if not path.is_dir():",
        new="if path.is_dir():",
        invariant="EDGEPROC_MODEL_PATH that is not a directory is refused",
    ),
    Mutation(
        id="MS-DIGEST-PATH-BINDING",
        target="edgeproc/localvec/model_source.py",
        old="digest.update(str(file.relative_to(path)).encode())",
        new="pass  # relative path no longer bound into the digest",
        invariant="the model digest binds each file's PATH, not just its bytes",
    ),
    Mutation(
        id="CT-BACKSLASH",
        target="edgeproc/bundles/containment.py",
        old=r'if not path or "\\" in path:',
        new="if not path:",
        invariant="a backslash (Windows traversal/drive vector) is refused",
    ),
    Mutation(
        id="CT-PARENT-TRAVERSAL",
        target="edgeproc/bundles/containment.py",
        old='if pure.is_absolute() or ".." in pure.parts:',
        new="if pure.is_absolute():",
        invariant="a `..` segment in a manifest path is refused",
    ),
    Mutation(
        id="CT-ESCAPE-REFUSAL",
        target="edgeproc/bundles/containment.py",
        old='raise UnsafePathError(f"path {relpath!r} escapes {root}")',
        new="pass  # resolved-escape refusal removed",
        invariant="a resolved target outside the output root is refused",
    ),
    Mutation(
        id="CAS-INGEST-DIGEST",
        target="edgeproc/bundles/cas.py",
        old=(
            "if _sha256(plaintext) != chunk_hash:\n"
            '                raise IntegrityError(f"fetched chunk'
        ),
        new='if False:\n                raise IntegrityError(f"fetched chunk',
        invariant="an ingested chunk is re-verified against its content address",
    ),
    Mutation(
        id="CAS-READ-DIGEST",
        target="edgeproc/bundles/cas.py",
        old='if _sha256(plaintext) != chunk_hash:\n            raise IntegrityError(f"chunk',
        new='if False:\n            raise IntegrityError(f"chunk',
        invariant="a chunk read back from the store is re-verified on read",
    ),
    Mutation(
        id="CAS-ZSTD-BOMB-CEILING",
        target="edgeproc/bundles/cas.py",
        old="if len(plaintext) > max_output_size:",
        new="if len(plaintext) > max_output_size + 1:",
        invariant="a zstd expansion bomb is refused at exactly the ceiling",
    ),
    Mutation(
        id="CAS-ROLLBACK-REFUSAL",
        target="edgeproc/bundles/cas.py",
        old="raise RollbackError(reason)",
        new="pass  # rollback refusal removed",
        invariant="a promote that cannot prove freshness is refused",
    ),
    Mutation(
        id="CAS-EQUAL-VERSION-FRESH",
        target="edgeproc/bundles/cas.py",
        old="if incoming_version == active_version:\n        return _Freshness.UNDECIDABLE",
        new="if incoming_version == active_version:\n        return _Freshness.FRESH",
        invariant="an EQUAL version proves nothing; it is a replay window, not a pass",
    ),
    Mutation(
        id="CAS-BAD-VERSION-FRESH",
        target="edgeproc/bundles/cas.py",
        old="except InvalidVersion:\n        return _Freshness.UNDECIDABLE",
        new="except InvalidVersion:\n        return _Freshness.FRESH",
        invariant="an unparseable version fails CLOSED, never open",
    ),
    Mutation(
        id="CAS-NO-PROOF-PASSES",
        target="edgeproc/bundles/cas.py",
        old="return _UNPROVABLE",
        new="return None",
        invariant="absence of proof on both comparisons refuses the promote",
    ),
    Mutation(
        id="SY-POINTER-SIGNATURE",
        target="edgeproc/bundles/sync.py",
        old="verifier.verify(pointer_signing_bytes(pointer), pointer.signature)",
        new="_ = (verifier, pointer_signing_bytes)  # signature never verified",
        invariant="the /latest pointer's detached signature is verified before use",
    ),
    Mutation(
        id="SY-MANIFEST-DIGEST",
        target="edgeproc/bundles/sync.py",
        old="if _sha256(raw) != pointer.manifest_hash:",
        new="if False:",
        invariant="the fetched manifest must hash to what the signed pointer names",
    ),
    Mutation(
        id="SY-AGGREGATE-CAP",
        target="edgeproc/bundles/sync.py",
        old="if fetched + len(compressed) > max_total_bytes:",
        new="if fetched > max_total_bytes:",
        invariant="the aggregate byte ceiling is enforced BEFORE the chunk is written",
    ),
    Mutation(
        id="SY-FILE-COUNT-CAP",
        target="edgeproc/bundles/sync.py",
        old="if len(manifest.files) > max_files:",
        new="if len(manifest.files) > max_files + 1:",
        invariant="the manifest file-count cap refuses at exactly the ceiling",
    ),
    Mutation(
        id="SY-REASSEMBLY-DIGEST",
        target="edgeproc/bundles/sync.py",
        old="if size != entry.size or digest.hexdigest() != entry.file_sha256:",
        new="if size != entry.size:",
        invariant="a reassembled file is checked by CONTENT hash, not only by length",
    ),
    Mutation(
        id="SG-VERIFY-REFUSAL",
        target="edgeproc/bundles/signing.py",
        old='raise SignatureError("signature verification failed") from exc',
        new="pass  # verification failure swallowed",
        invariant="Ed25519 verification failure raises instead of returning quietly",
    ),
    Mutation(
        id="MF-SEQUENCE-ABSENT",
        target="edgeproc/bundles/manifest.py",
        old="if incoming.sequence is None:\n        return False",
        new="if incoming.sequence is None:\n        return True",
        invariant="silence does not answer an active monotonic counter",
    ),
    Mutation(
        id="MF-SEQUENCE-STRICT",
        target="edgeproc/bundles/manifest.py",
        old="return incoming.sequence > active.sequence",
        new="return incoming.sequence >= active.sequence",
        invariant="only a STRICTLY greater sequence is fresh; equal is a replay",
    ),
    Mutation(
        id="RT-FIRST-ACCEPT",
        target="edgeproc/core/router.py",
        old="if runtime.can_handle(task) == CapabilityVerdict.ACCEPT:",
        new="if runtime.can_handle(task) != CapabilityVerdict.ACCEPT:",
        invariant="the router picks the first ACCEPTING runtime, in registration order",
    ),
)

# --------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------


def classify(returncode: int) -> str:
    """Turn a pytest exit code into a verdict. Only 0 and 1 are measurements.

    pytest exits 1 for "tests failed" -- the mutation was CAUGHT -- and 0 for a clean run,
    which means it SURVIVED. Everything else (2 interrupted, 3 internal error, 4 usage
    error, 5 nothing collected, or a signal) means the suite never judged the mutation, so
    scoring it as a catch would inflate the ratio with runs that only crashed.
    """
    if returncode == 0:
        return SURVIVED
    if returncode == 1:
        return CAUGHT
    return ERROR


def module_name(target: str) -> str:
    """`edgeproc/bundles/cas.py` -> `edgeproc.bundles.cas`, for the runtime probe."""
    return target.removesuffix(".py").replace("/", ".")


def purge_bytecode(root: Path) -> int:
    """Delete every ``__pycache__`` in the code trees; return how many were removed.

    Never rely on mtime. CPython invalidates a ``.pyc`` on ``(mtime, size)``, and both a
    same-size mutation and its restore can land inside one mtime tick -- leaving stale
    bytecode to shadow the source in either direction.
    """
    removed = 0
    for name in _CODE_DIRS:
        for cache in (root / name).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
            removed += 1
    return removed


def _result(mutation: Mutation, verdict: str, detail: str) -> Result:
    return Result(
        mutation_id=mutation.id,
        target=Path(mutation.target).name,
        verdict=verdict,
        detail=detail,
    )


def _reject_unusable(path: Path, mutation: Mutation) -> str | None:
    """Refuse before touching anything. A mutation that cannot be placed is an ERROR.

    Not-found and not-unique are both reported, never skipped: a stale ``old`` string that
    silently matched nothing is exactly how a harness reports a ratio it never measured.
    """
    if not path.is_file():
        return f"target missing: {mutation.target}"
    hits = path.read_text(encoding="utf-8").count(mutation.old)
    if hits == 0:
        return "old text not found in target"
    if hits > 1:
        return f"old text is not unique ({hits} matches)"
    return None


def _measure(mutation: Mutation, path: Path, original: bytes, root: Path, runner: Runner) -> Result:
    """Write the defect, prove it landed on disk, run the suite, read the verdict."""
    mutated = original.decode("utf-8").replace(mutation.old, mutation.new)
    path.write_text(mutated, encoding="utf-8")
    if path.read_text(encoding="utf-8") != mutated:
        return _result(mutation, ERROR, "mutated source did not persist to disk")
    purge_bytecode(root)
    outcome = runner(mutation, root, original)
    if outcome.probe != PROBE_OK:
        return _result(mutation, ERROR, f"probe: {outcome.probe or 'no probe output'}")
    return _result(mutation, classify(outcome.returncode), outcome.caught_by)


def _restore(path: Path, original: bytes, root: Path) -> None:
    """Put the source back byte-for-byte, then clear bytecode again.

    A clean ``git diff`` is not proof the runtime recovered: a ``.pyc`` compiled from the
    mutated source outlives the restore and would shadow the next baseline.
    """
    path.write_bytes(original)
    purge_bytecode(root)


def run_mutation(mutation: Mutation, root: Path, runner: Runner) -> Result:
    """Score one mutation. The ``finally`` is the contract: the source always comes back."""
    path = root / mutation.target
    problem = _reject_unusable(path, mutation)
    if problem is not None:
        return _result(mutation, ERROR, problem)
    original = path.read_bytes()
    try:
        return _measure(mutation, path, original, root, runner)
    finally:
        _restore(path, original, root)


# --------------------------------------------------------------------------------------
# The real runner
# --------------------------------------------------------------------------------------


#: Coverage OFF, because `--cov-fail-under` exiting 1 would be indistinguishable from a
#: caught mutation. Cache OFF, so no run inherits the last one's state. Probe IN.
_PYTEST_FLAGS: Final = (
    "-m",
    "pytest",
    "--no-cov",
    "-p",
    "no:cacheprovider",
    "-p",
    PROBE_PLUGIN,
    f"--ignore={SELF_TEST}",
    "-x",
    "-q",
    "--tb=no",
    "-rf",
)


def _pytest_argv(mutation: Mutation) -> list[str]:
    """`-x` is right here: the first failure already proves the catch."""
    return [sys.executable, *_PYTEST_FLAGS, *mutation.tests]


def _probe_env(root: Path, mutation: Mutation, baseline: Path, sentinel: Path) -> dict[str, str]:
    """Wire the probe in and forbid bytecode, so the run must compile from source."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(root / "scripts"), env.get("PYTHONPATH", "")) if p
    )
    env[ENV_MODULE] = module_name(mutation.target)
    env[ENV_BASELINE] = str(baseline)
    env[ENV_OUT] = str(sentinel)
    return env


def _first_failure(stdout: str) -> str:
    """Name the test that caught it. Display only -- the verdict is the exit code."""
    for line in stdout.splitlines():
        if line.startswith("FAILED "):
            return line.removeprefix("FAILED ").split(" - ")[0]
    return ""


def suite_runner(mutation: Mutation, root: Path, baseline_source: bytes) -> RunOutcome:
    """Run the suite in a fresh interpreter and collect the in-process probe's verdict.

    Not named ``pytest_*``: this module is itself loaded as a pytest plugin, and pluggy
    rejects any ``pytest_``-prefixed callable that is not one of its known hooks.
    """
    with tempfile.TemporaryDirectory() as tmp:
        baseline, sentinel = Path(tmp) / "baseline.py", Path(tmp) / "probe.txt"
        baseline.write_bytes(baseline_source)
        env = _probe_env(root, mutation, baseline, sentinel)
        done = _spawn(_pytest_argv(mutation), root, env)
        probe = sentinel.read_text(encoding="utf-8").strip() if sentinel.is_file() else ""
    return RunOutcome(done.returncode, probe, _first_failure(done.stdout))


def _spawn(argv: list[str], root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`returncode` is the verdict; stdout is captured for the failing test's NAME only."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=root, env=env, capture_output=True, text=True, check=False
    )


# --------------------------------------------------------------------------------------
# The runtime probe (loaded INSIDE the pytest process via `-p mutation_harness`)
# --------------------------------------------------------------------------------------


def _fingerprint(code: CodeType) -> str:
    """Identify a code object by what the compiler emitted, ignoring its filename."""
    parts = (
        code.co_name,
        code.co_code.hex(),
        repr(code.co_names),
        repr(code.co_varnames),
        repr([c for c in code.co_consts if not isinstance(c, CodeType)]),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _walk_code(code: CodeType) -> frozenset[str]:
    """Fingerprint every NESTED code object.

    The module's own top-level frame is excluded because nothing keeps a reference to it
    after import, so it could never be matched against the loaded module.
    """
    found: set[str] = set()
    for const in code.co_consts:
        if isinstance(const, CodeType):
            found.add(_fingerprint(const))
            found |= _walk_code(const)
    return frozenset(found)


def _fingerprints(source: bytes) -> frozenset[str]:
    return _walk_code(compile(source, "<mutation-probe>", "exec"))


def _code_bearing(module: ModuleType) -> Iterator[object]:
    """Module-level callables plus everything defined inside module-level classes."""
    for value in vars(module).values():
        yield getattr(value, "__func__", value)
        if isinstance(value, type):
            for member in vars(value).values():
                yield getattr(member, "__func__", member)


def _loaded_fingerprints(module: ModuleType) -> frozenset[str]:
    """Fingerprint the bytecode this interpreter is ACTUALLY holding."""
    found: set[str] = set()
    for obj in _code_bearing(module):
        code = getattr(obj, "__code__", None)
        if isinstance(code, CodeType):
            found.add(_fingerprint(code))
            found |= _walk_code(code)
    return frozenset(found)


def probe_verdict() -> str:
    """Prove this interpreter is executing the MUTATED bytecode, not a stale ``.pyc``.

    Compiles the baseline and the current source, then demands the loaded module carry a
    code object that ONLY the mutated source can produce. A stale ``.pyc`` fails this: its
    bytecode still matches the baseline.
    """
    module = importlib.import_module(os.environ[ENV_MODULE])
    source = getattr(module, "__file__", None)
    if source is None:
        return "module has no source file"
    baseline = Path(os.environ[ENV_BASELINE]).read_bytes()
    only_mutated = _fingerprints(Path(source).read_bytes()) - _fingerprints(baseline)
    return _match_verdict(only_mutated, _loaded_fingerprints(module))


def _match_verdict(only_mutated: frozenset[str], loaded: frozenset[str]) -> str:
    """An unmeasurable mutation and an unloaded one are both refusals, not passes."""
    if not only_mutated:
        return "mutation compiles to identical bytecode"
    if not (loaded & only_mutated):
        return "interpreter loaded UNMUTATED bytecode"
    return PROBE_OK


def _write_probe() -> None:
    """Record the verdict to a file. Never raises.

    A probe that raised would change pytest's exit code, and a harness cannot tell that
    apart from a caught mutation.
    """
    out = Path(os.environ[ENV_OUT])
    try:
        out.write_text(probe_verdict(), encoding="utf-8")
    except Exception as exc:
        out.write_text(f"probe crashed: {exc!r}", encoding="utf-8")


# --------------------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------------------


def _snapshot(root: Path, mutations: tuple[Mutation, ...]) -> dict[str, bytes]:
    targets = {m.target for m in mutations}
    return {t: (root / t).read_bytes() for t in sorted(targets) if (root / t).is_file()}


def verify_restored(root: Path, baseline: dict[str, bytes]) -> tuple[str, ...]:
    """Names of files that did NOT come back byte-identical. Empty means every one did."""
    return tuple(t for t, data in baseline.items() if (root / t).read_bytes() != data)


def _print_table(results: tuple[Result, ...]) -> None:
    print(f"{'MUTATION':<24} {'TARGET':<18} {'VERDICT':<9} DETAIL")
    for r in results:
        print(f"{r.mutation_id:<24} {r.target:<18} {r.verdict:<9} {r.detail[:58]}")


def _tally(results: tuple[Result, ...], verdict: str) -> int:
    return sum(1 for r in results if r.verdict == verdict)


def _restore_line(dirty: tuple[str, ...]) -> str:
    """Say it plainly either way. An unverified restore must never read as success."""
    if dirty:
        return f"restore FAILED, did not come back byte-identical: {dirty}"
    return "restore: every target byte-identical"


def _print_summary(results: tuple[Result, ...], dirty: tuple[str, ...]) -> None:
    caught, survived = _tally(results, CAUGHT), _tally(results, SURVIVED)
    scored = caught + survived
    ratio = f"{caught / scored:.0%}" if scored else "n/a"
    print(f"\ncatch ratio {caught}/{scored} ({ratio})   errors {_tally(results, ERROR)}")
    print(_restore_line(dirty))


def _exit_code(results: tuple[Result, ...], dirty: tuple[str, ...]) -> int:
    clean = all(r.verdict == CAUGHT for r in results)
    return 0 if clean and not dirty else 1


def main() -> int:
    """Score every mutation, print the table, and fail if any survived or errored."""
    root = Path(__file__).resolve().parent.parent
    baseline = _snapshot(root, MUTATIONS)
    results = []
    for index, mutation in enumerate(MUTATIONS, start=1):
        print(f"[{index}/{len(MUTATIONS)}] {mutation.id}", file=sys.stderr, flush=True)
        results.append(run_mutation(mutation, root, suite_runner))
    scored, dirty = tuple(results), verify_restored(root, baseline)
    _print_table(scored)
    _print_summary(scored, dirty)
    return _exit_code(scored, dirty)


if os.environ.get(ENV_OUT):  # imported as a pytest plugin inside a mutation run
    _write_probe()

if __name__ == "__main__":
    raise SystemExit(main())
