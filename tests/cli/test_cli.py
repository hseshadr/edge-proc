"""The Typer CLI: version, runtime availability, and bundle sync."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from edgeproc import __version__
from edgeproc.cli import app

runner = CliRunner()


def test_version_command_prints_the_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_list_runtimes_reports_extra_availability() -> None:
    result = runner.invoke(app, ["list-runtimes"])
    assert result.exit_code == 0
    assert "localvec" in result.stdout


def test_bundle_sync_downloads_and_verifies(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "index.faiss").write_bytes(b"FAISSDATA")
    digest = hashlib.sha256(b"FAISSDATA").hexdigest()
    manifest = {
        "bundle_id": "b1",
        "version": "1.0.0",
        "files": [{"path": "index.faiss", "checksum": f"sha256:{digest}"}],
    }
    (origin / "manifest.json").write_text(json.dumps(manifest))
    cache = tmp_path / "cache"

    result = runner.invoke(
        app,
        ["bundle-sync", str(origin / "manifest.json"), str(cache), str(origin)],
    )
    assert result.exit_code == 0
    assert "b1" in result.stdout
    assert (cache / "index.faiss").read_bytes() == b"FAISSDATA"
