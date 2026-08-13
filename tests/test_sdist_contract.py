"""The source archive carries every support file its shipped test suite consumes."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path
from shutil import which


def test_sdist_contains_every_support_file_needed_by_its_tests(tmp_path: Path) -> None:
    # Given the actual source archive built through the project's declared backend
    uv = which("uv")
    assert uv is not None
    result = subprocess.run(  # noqa: S603 - fixed local tool + literal argv, no caller input
        [uv, "build", "--sdist", "--out-dir", str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    archive = next(tmp_path.glob("*.tar.gz"))

    # When its members are compared to what the included tests import and read
    with tarfile.open(archive, "r:gz") as package:
        members = {Path(name).parts[1:] for name in package.getnames() if "/" in name}
    required = {
        (".env.example",),
        (".github", "workflows", "ci.yml"),
        (".github", "workflows", "publish.yml"),
        (".github", "workflows", "security-audit.yml"),
        ("CITATION.cff",),
        ("CLAUDE.md",),
        ("ROADMAP.md",),
        ("benchmarks", "benchmark.py"),
        ("docs", "ARCHITECTURE.md"),
        ("docs", "OPERATIONS.md"),
        ("docs", "QUICKSTART.md"),
        ("scripts", "mutation_harness.py"),
        ("uv.lock",),
    }

    # Then a recipient can run the shipped suite without reaching back into the Git repo
    assert required <= members

    extract = tmp_path / "extracted"
    extract.mkdir()
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(extract, filter="data")
    source = next(extract.iterdir())
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(tmp_path / "sdist-venv")}
    shipped = subprocess.run(  # noqa: S603 - fixed local tool + literal test path
        [
            uv,
            "run",
            "--all-extras",
            "pytest",
            "--no-cov",
            "tests/test_release_contract_docs.py",
        ],
        cwd=source,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert shipped.returncode == 0, shipped.stdout + shipped.stderr
