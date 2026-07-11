"""The Typer CLI: version, runtime availability, and route over a saved index."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest
from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding
from typer.testing import CliRunner

from edgeproc import __version__
from edgeproc.cli import app
from edgeproc.localvec.faiss_index import FaissVectorIndex

from ..localvec._fakes import FakeEncoder

runner = CliRunner()

# `from edgeproc.cli import app` rebinds the name `app` to the Typer instance, so the
# submodule `edgeproc.cli.app` can't be reached by attribute access. import_module
# returns the real module object (from sys.modules) so we can patch the encoder seam.
_cli_app_module = importlib.import_module("edgeproc.cli.app")


def test_version_command_prints_the_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_list_runtimes_reports_extra_availability() -> None:
    result = runner.invoke(app, ["list-runtimes"])
    assert result.exit_code == 0
    assert "localvec" in result.stdout


def _save_catalog_index(directory: Path) -> None:
    encoder = FakeEncoder()
    index = FaissVectorIndex("catalog", IndexConfig(dimension=encoder.dim))
    ids = ["p1", "p2", "p3", "p4"]
    texts = ["red shoes", "blue boots", "green dress", "red shoes"]
    vectors = encoder.encode_texts(texts)

    async def _fill() -> None:
        await index.insert(
            [
                VectorEmbedding(entity_id=entity_id, embedding=vector.tolist())
                for entity_id, vector in zip(ids, vectors, strict=True)
            ]
        )

    asyncio.run(_fill())
    index.save(directory)


def _write_task(path: Path, *, kind: str = "search") -> None:
    path.write_text(
        json.dumps(
            {
                "kind": kind,
                "payload": {"query": "red shoes", "k": 2},
                "privacy_mode": "local_only",
            }
        )
    )


def test_route_executes_search_over_a_saved_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cli_app_module, "build_encoder", lambda _model: FakeEncoder())
    index_dir = tmp_path / "idx"
    _save_catalog_index(index_dir)
    task = tmp_path / "task.json"
    _write_task(task)

    result = runner.invoke(app, ["route", "--index-dir", str(index_dir), "--task", str(task)])

    assert result.exit_code == 0
    assert '"success": true' in result.stdout
    assert "results" in result.stdout


def test_route_pretty_summarizes_the_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cli_app_module, "build_encoder", lambda _model: FakeEncoder())
    index_dir = tmp_path / "idx"
    _save_catalog_index(index_dir)
    task = tmp_path / "task.json"
    _write_task(task)

    result = runner.invoke(
        app, ["route", "--index-dir", str(index_dir), "--task", str(task), "--pretty"]
    )

    assert result.exit_code == 0
    assert "runtime=localvec" in result.stdout
    assert "p1" in result.stdout


def test_route_no_accepting_runtime_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cli_app_module, "build_encoder", lambda _model: FakeEncoder())
    index_dir = tmp_path / "idx"
    _save_catalog_index(index_dir)
    task = tmp_path / "task.json"
    _write_task(task, kind="generate")

    result = runner.invoke(
        app, ["route", "--index-dir", str(index_dir), "--task", str(task), "--pretty"]
    )

    assert result.exit_code == 1
    assert "no_runtime_accepted" in result.stdout
    assert "runtime=none" in result.stdout


def test_route_invalid_task_json_fails_closed(tmp_path: Path) -> None:
    task = tmp_path / "task.json"
    task.write_text("{not valid json")

    result = runner.invoke(
        app, ["route", "--index-dir", str(tmp_path / "idx"), "--task", str(task)]
    )

    assert result.exit_code == 1
    assert '"success"' not in result.stdout


def test_route_missing_index_dir_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_cli_app_module, "build_encoder", lambda _model: FakeEncoder())
    task = tmp_path / "task.json"
    _write_task(task)

    result = runner.invoke(
        app, ["route", "--index-dir", str(tmp_path / "absent"), "--task", str(task)]
    )

    assert result.exit_code == 1
    assert '"success"' not in result.stdout


def test_materialize_refuses_traversal_before_writing(tmp_path: Path) -> None:
    # Defense-in-depth: even if a manifest with a traversal path somehow reached the
    # write loop (bypassing model validation via model_construct here), the loop must
    # refuse it BEFORE any write — nothing lands outside the output dir.
    from edgeproc.bundles.containment import UnsafePathError  # noqa: PLC0415
    from edgeproc.bundles.manifest import FileEntry, IndexManifest  # noqa: PLC0415

    out = tmp_path / "out"
    evil = FileEntry.model_construct(
        path="../evil.txt", file_type=None, size=0, file_sha256="00" * 32, chunks=[]
    )
    manifest = IndexManifest.model_construct(
        bundle_id="b", version="1.0.0", files=[evil], metadata={}
    )

    with pytest.raises(UnsafePathError):
        _cli_app_module._materialize_files(object(), manifest, out)

    assert not (tmp_path / "evil.txt").exists()
