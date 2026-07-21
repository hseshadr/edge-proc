"""The Typer CLI: version, runtime availability, and route over a saved index."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from pathlib import Path

import pytest
from edgeproc_core.vector_mgmt.core.types import IndexConfig, VectorEmbedding
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


def test_keygen_writes_private_key_owner_only(tmp_path: Path) -> None:
    # A signing key on a shared box must not be world-readable: `private.key` is a secret,
    # so keygen writes it 0600 (owner rw only), never the default world-readable 0644.
    result = runner.invoke(app, ["keygen", "--out", str(tmp_path)])
    assert result.exit_code == 0
    private = tmp_path / "private.key"
    assert private.is_file()
    mode = private.stat().st_mode & 0o777
    assert oct(mode) == "0o600", f"private key mode is {oct(mode)}, expected 0o600"
    # The public key is not a secret — it stays readable so a verifier can pin it.
    assert (tmp_path / "public.key").is_file()


def test_keygen_creates_output_directory_owner_only(tmp_path: Path) -> None:
    # Given
    out = tmp_path / "keys"
    previous_umask = os.umask(0)

    # When
    try:
        result = runner.invoke(app, ["keygen", "--out", str(out)])
    finally:
        os.umask(previous_umask)

    # Then
    assert result.exit_code == 0
    assert out.stat().st_mode & 0o777 == 0o700


def test_keygen_tightens_existing_output_directory(tmp_path: Path) -> None:
    # Given
    out = tmp_path / "keys"
    out.mkdir(mode=0o755)
    out.chmod(0o755)

    # When
    result = runner.invoke(app, ["keygen", "--out", str(out)])

    # Then
    assert result.exit_code == 0
    assert out.stat().st_mode & 0o777 == 0o700


def test_keygen_refuses_symlinked_output_directory(tmp_path: Path) -> None:
    # Given
    outside = tmp_path / "outside"
    outside.mkdir()
    out = tmp_path / "keys"
    out.symlink_to(outside, target_is_directory=True)

    # When
    result = runner.invoke(app, ["keygen", "--out", str(out)])

    # Then
    assert result.exit_code != 0
    assert not (outside / "private.key").exists()
    assert not (outside / "public.key").exists()


def test_keygen_refuses_symlinked_key_path(tmp_path: Path) -> None:
    # An attacker pre-plants a symlink where private.key will be written, aimed at a victim
    # file. keygen must NOT follow it (O_NOFOLLOW): it fails closed and the victim is intact.
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"SECRET-ORIGINAL")
    out = tmp_path / "keys"
    out.mkdir()
    (out / "private.key").symlink_to(victim)

    result = runner.invoke(app, ["keygen", "--out", str(out)])

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert victim.read_bytes() == b"SECRET-ORIGINAL"  # the symlink target was NOT clobbered


def test_sync_missing_trust_key_fails_closed(tmp_path: Path) -> None:
    # A pinned trust-root pubkey path that does not exist must fail CLOSED with a clean
    # message, never a raw traceback: an unreadable key file is operator error, not a crash.
    result = runner.invoke(
        app,
        [
            "sync",
            "--base-url",
            str(tmp_path / "origin"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--key",
            str(tmp_path / "absent.key"),
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "trust-root key" in result.stderr
    # The operator sees the canonical code, not just prose: a missing key file is config.missing.
    assert "[config.missing]" in result.stderr


def test_sync_malformed_trust_key_fails_closed(tmp_path: Path) -> None:
    # A present but wrong-length pubkey (not 32 raw ed25519 bytes) must fail CLOSED cleanly,
    # mirroring how `publish` handles a malformed signing key — no traceback escapes.
    bad = tmp_path / "public.key"
    bad.write_bytes(b"short")  # not a 32-byte raw ed25519 public key
    result = runner.invoke(
        app,
        [
            "sync",
            "--base-url",
            str(tmp_path / "origin"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--key",
            str(bad),
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "malformed trust-root key" in result.stderr
    # A present-but-corrupt key is a DIFFERENT canonical code than an absent one.
    assert "[config.invalid]" in result.stderr


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


def _publish(origin: Path, src: Path, key: Path, *, version: str) -> None:
    """Drive the real `publish` command to lay a signed bundle into `origin`."""
    result = runner.invoke(
        app,
        [
            "publish",
            "--src",
            str(src),
            "--origin-dir",
            str(origin),
            "--key",
            str(key),
            "--bundle-id",
            "catalog",
            "--version",
            version,
        ],
    )
    assert result.exit_code == 0, result.output


def _sync(origin: Path, cache: Path, pubkey: Path) -> None:
    """Drive the real `sync` command to pull `origin` into `cache`."""
    result = runner.invoke(
        app,
        ["sync", "--base-url", str(origin), "--cache-dir", str(cache), "--key", str(pubkey)],
    )
    assert result.exit_code == 0, result.output


def test_gc_command_reclaims_superseded_chunks(tmp_path: Path) -> None:
    """`edgeproc gc` is the operator entry point docs/OPERATIONS.md tells operators to run.

    Drives the REAL CLI end to end — keygen, publish v1, sync, publish v2, sync, gc — so
    the command is proven reachable, not merely that `FilesystemCacheStore.gc()` exists.
    """
    # Given: a cache synced through two bundle versions, so v1's chunks are now unreferenced
    keys, src, origin, cache = (tmp_path / n for n in ("keys", "src", "origin", "cache"))
    src.mkdir()
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0
    (src / "catalog.txt").write_bytes(b"version one payload " * 512)
    _publish(origin, src, keys / "private.key", version="1.0.0")
    _sync(origin, cache, keys / "public.key")
    (src / "catalog.txt").write_bytes(b"version two payload " * 512)
    _publish(origin, src, keys / "private.key", version="2.0.0")
    _sync(origin, cache, keys / "public.key")

    # When
    result = runner.invoke(app, ["gc", "--cache-dir", str(cache)])

    # Then: it reports what it reclaimed, and the active bundle is still intact
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"] > 0
    out = tmp_path / "out"
    verify = runner.invoke(
        app,
        [
            "sync",
            "--base-url",
            str(origin),
            "--cache-dir",
            str(cache),
            "--key",
            str(keys / "public.key"),
            "--materialize-to",
            str(out),
        ],
    )
    assert verify.exit_code == 0, verify.output
    assert (out / "catalog.txt").read_bytes() == b"version two payload " * 512


def test_gc_on_a_store_with_no_active_pointer_is_a_safe_no_op(tmp_path: Path) -> None:
    """Fail-safe: GC must never wipe a store that has nothing promoted."""
    result = runner.invoke(app, ["gc", "--cache-dir", str(tmp_path / "empty")])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed"] == 0


def test_gc_fails_closed_with_a_canonical_code_on_a_corrupt_store(tmp_path: Path) -> None:
    """A GC that hits a corrupt object must exit 1 with the canonical code, not a traceback."""
    # Given: a synced cache whose stored manifest has been tampered with
    keys, src, origin, cache = (tmp_path / n for n in ("keys", "src", "origin", "cache"))
    src.mkdir()
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0
    (src / "catalog.txt").write_bytes(b"payload " * 256)
    _publish(origin, src, keys / "private.key", version="1.0.0")
    _sync(origin, cache, keys / "public.key")
    manifests = sorted((cache / "manifests").iterdir())
    assert manifests, "sync should have stored a manifest"
    manifests[0].write_bytes(b'{"bundle_id":"tampered","version":"9.9.9","files":[],"metadata":{}}')

    # When
    result = runner.invoke(app, ["gc", "--cache-dir", str(cache)])

    # Then
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "[bundle.integrity_failed]" in result.stderr
    assert "gc failed" in result.stderr


def test_gc_pretty_prints_a_human_summary(tmp_path: Path) -> None:
    result = runner.invoke(app, ["gc", "--cache-dir", str(tmp_path / "store"), "--pretty"])
    assert result.exit_code == 0, result.output
    assert "reclaimed 0 objects" in result.stdout
