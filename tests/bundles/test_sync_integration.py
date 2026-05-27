"""``edgeproc sync`` over a REAL local HTTP origin — the spec's verification gate.

This is the one integration test that stands up an honest HTTP origin (NOT
``httpx.MockTransport``) and drives the full ``sync_index`` engine across the wire
via ``HttpAdapter`` + the ``sync`` CLI command. The origin is Python's stdlib
``http.server`` (``ThreadingHTTPServer`` + ``SimpleHTTPRequestHandler``) rooted at a
produced CAS dir and bound to ``127.0.0.1:0`` for an ephemeral port — CI-safe, no
Docker. The producer helper (``_producer.build_origin``) lays out ``latest``,
``manifest/<hash>``, ``chunk/<hash>`` exactly per the HTTP contract, so the server
serves them verbatim.

The four behaviors pinned here are the Phase A exit criteria for the CLI: an
end-to-end round-trip over HTTP, a patch that fetches only the changed chunks, a
fail-closed rejection of a tampered origin (exit 1, no traceback), and a
fail-closed refusal when no trust root is configured.

PRODUCTION ANALOG: edge-reco's ``deploy/docker-compose.yml`` Caddy stack is the
edge/production analog of this ``http.server`` origin — same URL scheme. Its caching
evolves from TTL today to immutable-chunks + short-TTL-``/latest`` per this spec. We
deliberately do NOT add a Docker dependency to the automated suite; stdlib
``http.server`` is the CI-safe stand-in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING

import pytest
import zstandard
from typer.testing import CliRunner

from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import IndexManifest, VersionPointer
from edgeproc.bundles.signing import Ed25519Signer, generate_keypair
from edgeproc.cli import app
from tests.bundles import _producer

if TYPE_CHECKING:
    from collections.abc import Iterator

runner = CliRunner()

_FILES: dict[str, bytes] = {
    "norm.json": b'{"a":1,"b":2}',
    "small.bin": b"a small payload under the min chunk size",
    "index.faiss": (b"the quick brown fox jumps over the lazy dog. " * 12_000)[: 400 * 1024],
}


@dataclass(frozen=True)
class Origin:
    """A live HTTP origin under test: its URL, on-disk dir, signer, and pinned pubkey."""

    base_url: str
    dir: Path
    signer: Ed25519Signer
    pubkey: Path


def _build_origin(
    origin: Path, signer: Ed25519Signer, files: dict[str, bytes], version: str
) -> VersionPointer:
    return _producer.build_origin(
        files=files, origin=origin, chunker=GearCDC(), signer=signer, version=version
    )


def _serve(directory: Path) -> tuple[ThreadingHTTPServer, Thread, str]:
    handler = partial(SimpleHTTPRequestHandler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    return server, thread, f"http://{host}:{port}"


@pytest.fixture
def origin_server(tmp_path: Path) -> Iterator[Origin]:
    """Serve a produced origin over real HTTP, ephemeral port; tear the server down."""
    private, public = generate_keypair()
    signer = Ed25519Signer(private)
    origin = tmp_path / "origin"
    _build_origin(origin, signer, _FILES, "1.0.0")
    pubkey = tmp_path / "trust.pub"
    pubkey.write_bytes(public.public_bytes_raw())

    server, thread, base_url = _serve(origin)
    try:
        yield Origin(base_url=base_url, dir=origin, signer=signer, pubkey=pubkey)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _sync(base_url: str, cache: Path, *, key: Path | None) -> object:
    args = ["sync", "--http", "--base-url", base_url, "--cache-dir", str(cache)]
    if key is not None:
        args += ["--key", str(key)]
    return runner.invoke(app, args)


def _manifest_at(origin: Path, pointer: VersionPointer) -> IndexManifest:
    raw = (origin / "manifest" / pointer.manifest_hash).read_bytes()
    return IndexManifest.model_validate_json(raw)


def _read_pointer(origin: Path) -> VersionPointer:
    return VersionPointer.model_validate_json((origin / "latest").read_bytes())


def test_end_to_end_sync_over_http(origin_server: Origin, tmp_path: Path) -> None:
    cache = tmp_path / "cache"

    result = _sync(origin_server.base_url, cache, key=origin_server.pubkey)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["chunks_reused"] == 0
    assert payload["chunks_fetched"] > 0
    _assert_materializes(cache, origin_server.dir)


def _assert_materializes(cache: Path, origin: Path) -> None:
    from edgeproc.bundles.cas import FilesystemCacheStore  # noqa: PLC0415
    from edgeproc.bundles.sync import materialize_file  # noqa: PLC0415

    store = FilesystemCacheStore(cache)
    manifest = _manifest_at(origin, _read_pointer(origin))
    for path, original in _FILES.items():
        assert materialize_file(store, manifest, path) == original


def test_patch_over_http_fetches_only_changed_chunks(origin_server: Origin, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    first = _sync(origin_server.base_url, cache, key=origin_server.pubkey)
    assert first.exit_code == 0, first.stdout
    fetched_first = json.loads(first.stdout)["chunks_fetched"]

    patched = dict(_FILES)
    patched["index.faiss"] = _FILES["index.faiss"] + b"a localized few-byte edit at the tail"
    _build_origin(origin_server.dir, origin_server.signer, patched, "1.0.1")

    result = _sync(origin_server.base_url, cache, key=origin_server.pubkey)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert 1 <= payload["chunks_fetched"] < fetched_first  # only the changed tail chunk(s)
    assert payload["chunks_reused"] > payload["chunks_fetched"]


def test_fail_closed_on_tampered_chunk_over_http(origin_server: Origin, tmp_path: Path) -> None:
    manifest = _manifest_at(origin_server.dir, _read_pointer(origin_server.dir))
    victim = manifest.files[-1].chunks[0].hash
    (origin_server.dir / "chunk" / victim).write_bytes(zstandard.compress(b"corrupted bytes"))

    result = _sync(origin_server.base_url, tmp_path / "cache", key=origin_server.pubkey)

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "sync failed" in result.stderr


def test_missing_trust_key_fails_closed(
    origin_server: Origin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EDGEPROC_TRUST_ROOT_PUBKEY_PATH", raising=False)

    result = _sync(origin_server.base_url, tmp_path / "cache", key=None)

    assert result.exit_code == 1
    assert "trust root" in result.stderr.lower()


def test_sync_filesystem_origin_with_env_trust_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``--http`` (FilesystemAdapter), trust root from env, ``--pretty`` summary."""
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    _build_origin(origin, Ed25519Signer(private), _FILES, "1.0.0")
    pubkey = tmp_path / "trust.pub"
    pubkey.write_bytes(public.public_bytes_raw())
    monkeypatch.setenv("EDGEPROC_TRUST_ROOT_PUBKEY_PATH", str(pubkey))

    result = runner.invoke(
        app, ["sync", "--base-url", str(origin), "--cache-dir", str(tmp_path / "c"), "--pretty"]
    )

    assert result.exit_code == 0, result.stdout
    assert "1.0.0" in result.stdout
    assert "fetched" in result.stdout.lower()
