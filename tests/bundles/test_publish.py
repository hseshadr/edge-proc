"""``build_bundle`` producer + ``edgeproc publish`` CLI — the producer counterpart.

These pin the SHIPPED producer (``edgeproc.bundles.publish.build_bundle``) and its
CLI surface (``edgeproc publish`` / ``edgeproc keygen``) as the counterpart to
``sync_index``. The headline is the producer→consumer round-trip: ``build_bundle``
lays out an origin that a FRESH ``sync_index`` over a ``FilesystemAdapter`` pulls
back byte-for-byte, with the signed pointer verifying under the matching verifier.

Determinism (same files → same ``manifest_hash``) and signature validity (verifies
under the signer's key, FAILS under a different key) are pinned alongside, plus the
CLI publish→sync round-trip and a fail-closed missing-key error (exit 1, no
traceback). These exercise the shipped code the test-only ``_producer`` now
delegates to, so the wave-5/6 integration tests ride the same producer.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from edgeproc.bundles.cas import FilesystemCacheStore
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import VersionPointer, pointer_signing_bytes
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    SignatureError,
    generate_keypair,
)
from edgeproc.bundles.sync import materialize_file, sync_index
from edgeproc.cli import app

runner = CliRunner()

# A small file, a sub-min-chunk file, and a >256 KiB file that must split into ≥2 chunks.
_BIG = (b"the quick brown fox jumps over the lazy dog. " * 12_000)[: 400 * 1024]
_FILES: dict[str, bytes] = {
    "norm.json": b'{"a":1,"b":2}',
    "small.bin": b"a small payload under the min chunk size",
    "index.faiss": _BIG,
}


def _publish(
    origin: Path, signer: Ed25519Signer, files: dict[str, bytes], version: str
) -> VersionPointer:
    return build_bundle(
        files=files,
        store=FilesystemCacheStore(origin),
        chunker=GearCDC(),
        signer=signer,
        bundle_id="b",
        version=version,
    )


def test_build_bundle_round_trips_into_fresh_store(tmp_path: Path) -> None:
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    pointer = _publish(origin, Ed25519Signer(private), _FILES, "1.0.0")
    verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())

    store = FilesystemCacheStore(tmp_path / "cache")
    result = sync_index(base_url=str(origin), store=store, adapter=_fs_adapter(), verifier=verifier)

    assert result.chunks_reused == 0
    assert store.read_active() == pointer
    manifest = _manifest_at(origin, pointer)
    for path, original in _FILES.items():
        assert materialize_file(store, manifest, path) == original
    faiss = next(e for e in manifest.files if e.path == "index.faiss")
    assert len(faiss.chunks) >= 2  # the >256 KiB file really split


def test_republish_unchanged_catalog_touches_zero_chunk_files(tmp_path: Path) -> None:
    """A second ``build_bundle`` over the same files mustn't rewrite existing chunks."""
    private, _public = generate_keypair()
    origin = tmp_path / "origin"
    _publish(origin, Ed25519Signer(private), _FILES, "1.0.0")

    chunk_dir = origin / "chunk"
    pre = {p.name: p.stat().st_ino for p in chunk_dir.iterdir()}
    assert pre  # non-vacuous: two empty dirs would satisfy `pre == post` and prove nothing
    _publish(origin, Ed25519Signer(private), _FILES, "1.0.1")
    post = {p.name: p.stat().st_ino for p in chunk_dir.iterdir()}

    # Same inode for every chunk → not rewritten (hardlink reused, not replaced).
    assert pre == post


def test_publish_crash_keeps_previous_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private, _public = generate_keypair()
    origin = tmp_path / "origin"
    signer = Ed25519Signer(private)
    _publish(origin, signer, _FILES, "1.0.0")
    previous = (origin / "latest").read_bytes()
    real_replace = os.replace

    def crash_before_latest(src: object, dst: object) -> None:
        if Path(dst).name == "latest":
            raise OSError("simulated publisher crash")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash_before_latest)
    with pytest.raises(OSError, match="publisher crash"):
        _publish(origin, signer, {**_FILES, "new.bin": b"new"}, "1.0.1")
    assert (origin / "latest").read_bytes() == previous


def test_build_bundle_is_deterministic(tmp_path: Path) -> None:
    private, _public = generate_keypair()
    a = _publish(tmp_path / "a", Ed25519Signer(private), _FILES, "1.0.0")
    b = _publish(tmp_path / "b", Ed25519Signer(private), _FILES, "1.0.0")
    assert a.manifest_hash == b.manifest_hash


def test_pointer_signature_verifies_and_rejects_other_key(tmp_path: Path) -> None:
    private, public = generate_keypair()
    pointer = _publish(tmp_path / "origin", Ed25519Signer(private), _FILES, "1.0.0")
    # The signed preimage is `pointer_signing_bytes` (identity-aware), not the naive
    # `canonical_bytes(exclude={"signature"})` — the two now differ by the null identity keys.
    signed = pointer_signing_bytes(pointer)

    Ed25519Verifier.from_public_bytes(public.public_bytes_raw()).verify(signed, pointer.signature)
    _other_private, other_public = generate_keypair()
    with pytest.raises(SignatureError):
        Ed25519Verifier.from_public_bytes(other_public.public_bytes_raw()).verify(
            signed, pointer.signature
        )


def test_cli_publish_then_sync_round_trips(tmp_path: Path) -> None:
    src = _write_src(tmp_path / "src", _FILES)
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0

    origin = tmp_path / "origin"
    pub = runner.invoke(
        app,
        [
            "publish",
            "--src",
            str(src),
            "--origin-dir",
            str(origin),
            "--key",
            str(keys / "private.key"),
            "--bundle-id",
            "b",
            "--version",
            "1.0.0",
        ],
    )
    assert pub.exit_code == 0, pub.stdout

    cache = tmp_path / "cache"
    sync = runner.invoke(
        app,
        [
            "sync",
            "--base-url",
            str(origin),
            "--cache-dir",
            str(cache),
            "--key",
            str(keys / "public.key"),
        ],
    )
    assert sync.exit_code == 0, sync.stdout
    _assert_cache_materializes(cache, origin)


def test_cli_sync_materialize_writes_real_files(tmp_path: Path) -> None:
    """``--materialize-to`` must reassemble every published file byte-for-byte."""
    src = _write_src(tmp_path / "src", _FILES)
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0
    origin = tmp_path / "origin"
    pub = runner.invoke(
        app,
        [
            "publish",
            "--src", str(src),
            "--origin-dir", str(origin),
            "--key", str(keys / "private.key"),
            "--bundle-id", "b",
            "--version", "1.0.0",
        ],
    )  # fmt: skip
    assert pub.exit_code == 0, pub.stdout

    out = tmp_path / "materialized"
    sync_result = runner.invoke(
        app,
        [
            "sync",
            "--base-url", str(origin),
            "--cache-dir", str(tmp_path / "cache"),
            "--key", str(keys / "public.key"),
            "--materialize-to", str(out),
        ],
    )  # fmt: skip
    assert sync_result.exit_code == 0, sync_result.stdout
    for path, original in _FILES.items():
        assert (out / path).read_bytes() == original


def test_cli_publish_missing_key_fails_closed(tmp_path: Path) -> None:
    src = _write_src(tmp_path / "src", {"a.bin": b"x"})
    result = runner.invoke(
        app,
        [
            "publish",
            "--src",
            str(src),
            "--origin-dir",
            str(tmp_path / "origin"),
            "--key",
            str(tmp_path / "nope.key"),
            "--bundle-id",
            "b",
            "--version",
            "1.0.0",
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "could not read signing key" in result.stderr  # missing FILE branch
    assert result.stderr.strip()  # an error message went to stderr


def test_cli_publish_malformed_key_fails_closed(tmp_path: Path) -> None:
    # A key file that EXISTS but holds the wrong number of bytes is malformed: it must
    # give a distinct message from a missing/unreadable file (the two except branches).
    src = _write_src(tmp_path / "src", {"a.bin": b"x"})
    bad_key = tmp_path / "bad.key"
    bad_key.write_bytes(b"not-a-32-byte-ed25519-key")
    result = runner.invoke(
        app,
        [
            "publish",
            "--src",
            str(src),
            "--origin-dir",
            str(tmp_path / "origin"),
            "--key",
            str(bad_key),
            "--bundle-id",
            "b",
            "--version",
            "1.0.0",
        ],
    )
    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "malformed signing key" in result.stderr  # malformed-key branch


def _publish_src(tmp_path: Path, src: Path) -> Result:
    """Invoke `publish` with a valid signing key so ``src`` is the only thing under test."""
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0
    return runner.invoke(
        app,
        [
            "publish",
            "--src",
            str(src),
            "--origin-dir",
            str(tmp_path / "origin"),
            "--key",
            str(keys / "private.key"),
            "--bundle-id",
            "b",
            "--version",
            "1.0.0",
        ],
    )


def test_cli_publish_refuses_a_src_that_is_not_a_directory(tmp_path: Path) -> None:
    """Publishing a FILE (or a nonexistent path) as --src fails closed, not with a traceback.

    Surfaced by branch coverage: the guard's refusing edge had never been driven, so
    nothing proved this operator mistake produces a clean message instead of a crash.
    """
    a_file = tmp_path / "not-a-dir"
    a_file.write_bytes(b"x")

    result = _publish_src(tmp_path, a_file)

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "src is not a directory" in result.stderr


def test_cli_publish_refuses_an_empty_src_directory(tmp_path: Path) -> None:
    """An empty --src must refuse rather than sign and publish a contentless bundle.

    Publishing "nothing" would mint a validly-signed pointer whose manifest lists no
    files — a consumer would sync it and silently replace real content with an empty set.
    """
    empty = tmp_path / "empty-src"
    empty.mkdir()

    result = _publish_src(tmp_path, empty)

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "no files to publish" in result.stderr
    assert not (tmp_path / "origin").exists()  # nothing was minted


def test_cli_publish_bind_identity_then_sync_with_matching_pin(tmp_path: Path) -> None:
    """`--bind-identity`/`--channel`/`--sequence` + a matching `--expected-*` pin round-trips."""
    src = _write_src(tmp_path / "src", _FILES)
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0
    origin = tmp_path / "origin"
    pub = runner.invoke(
        app,
        [
            "publish",
            "--src", str(src),
            "--origin-dir", str(origin),
            "--key", str(keys / "private.key"),
            "--bundle-id", "b",
            "--version", "1.0.0",
            "--bind-identity",
            "--channel", "stable",
            "--sequence", "3",
        ],
    )  # fmt: skip
    assert pub.exit_code == 0, pub.stdout

    sync = runner.invoke(
        app,
        [
            "sync",
            "--base-url", str(origin),
            "--cache-dir", str(tmp_path / "cache"),
            "--key", str(keys / "public.key"),
            "--expected-bundle-id", "b",
            "--expected-channel", "stable",
        ],
    )  # fmt: skip
    assert sync.exit_code == 0, sync.stdout


def test_cli_sync_rejects_mismatched_expected_bundle_id(tmp_path: Path) -> None:
    """A cross-bundle replay is refused fail-closed (exit 1, no traceback)."""
    src = _write_src(tmp_path / "src", _FILES)
    keys = tmp_path / "keys"
    assert runner.invoke(app, ["keygen", "--out", str(keys)]).exit_code == 0
    origin = tmp_path / "origin"
    pub = runner.invoke(
        app,
        [
            "publish",
            "--src", str(src),
            "--origin-dir", str(origin),
            "--key", str(keys / "private.key"),
            "--bundle-id", "b",
            "--version", "1.0.0",
            "--bind-identity",
        ],
    )  # fmt: skip
    assert pub.exit_code == 0, pub.stdout

    sync = runner.invoke(
        app,
        [
            "sync",
            "--base-url", str(origin),
            "--cache-dir", str(tmp_path / "cache"),
            "--key", str(keys / "public.key"),
            "--expected-bundle-id", "not-b",
        ],
    )  # fmt: skip
    assert sync.exit_code == 1
    assert "Traceback" not in sync.stderr
    assert "sync failed" in sync.stderr


def _fs_adapter() -> object:
    from edgeproc.bundles.adapters import FilesystemAdapter  # noqa: PLC0415

    return FilesystemAdapter()


def _manifest_at(origin: Path, pointer: VersionPointer) -> object:
    from edgeproc.bundles.manifest import IndexManifest  # noqa: PLC0415

    raw = (origin / "manifest" / pointer.manifest_hash).read_bytes()
    return IndexManifest.model_validate_json(raw)


def _write_src(src: Path, files: dict[str, bytes]) -> Path:
    for path, data in files.items():
        target = src / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return src


def _assert_cache_materializes(cache: Path, origin: Path) -> None:
    store = FilesystemCacheStore(cache)
    pointer = VersionPointer.model_validate_json((origin / "latest").read_bytes())
    manifest = _manifest_at(origin, pointer)
    for path, original in _FILES.items():
        assert materialize_file(store, manifest, path) == original  # type: ignore[arg-type]
