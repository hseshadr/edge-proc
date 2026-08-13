"""Aggregate sync caps: a fail-closed ceiling on TOTAL bytes + file count per sync.

The substrate already bounds a SINGLE fetch (``max_fetch_bytes``) and a single chunk's
decompression (``max_decompressed_bytes``). Neither bounds the AGGREGATE: ``sync_index``
would fetch every chunk a manifest enumerates, so a hostile or runaway manifest could
name unbounded chunks/files and exhaust disk. These pin an aggregate ceiling —
``max_files`` (checked before any fetch) and ``max_total_bytes`` (a running ceiling that
aborts mid-fetch before writing the chunk that would cross it) — both fail-closed.

The defaults are generous (never trip a legitimate bundle) and configurable via
``EdgeProcSettings``; the tests inject tiny caps to exercise the guard directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.cas import FilesystemCacheStore
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from edgeproc.bundles.sync import SyncCapError, sync_index
from tests.bundles import _producer

# A >256 KiB file splits into several chunks; three files exercise the file-count cap.
_BIG = (b"the quick brown fox jumps over the lazy dog. " * 12_000)[: 400 * 1024]
_FILES: dict[str, bytes] = {"a.json": b'{"a":1}', "b.bin": b"payload b", "index.faiss": _BIG}


def _origin(tmp_path: Path) -> tuple[str, Ed25519Verifier]:
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    _producer.build_origin(
        files=_FILES, origin=origin, chunker=GearCDC(), signer=Ed25519Signer(private)
    )
    return str(origin), Ed25519Verifier.from_public_bytes(public.public_bytes_raw())


def test_sync_rejects_over_file_count_cap(tmp_path: Path) -> None:
    base_url, verifier = _origin(tmp_path)
    with pytest.raises(SyncCapError):
        sync_index(
            base_url=base_url,
            store=FilesystemCacheStore(tmp_path / "cache"),
            adapter=FilesystemAdapter(),
            verifier=verifier,
            max_files=2,  # boundary: manifest has exactly one file over the cap
        )


def test_sync_running_byte_cap_trips_mid_fetch(tmp_path: Path) -> None:
    base_url, verifier = _origin(tmp_path)
    with pytest.raises(SyncCapError):
        sync_index(
            base_url=base_url,
            store=FilesystemCacheStore(tmp_path / "cache"),
            adapter=FilesystemAdapter(),
            verifier=verifier,
            max_total_bytes=100,  # the multi-chunk bundle blows past 100 bytes of fetch
        )


def test_sync_over_cap_writes_nothing_over_the_ceiling(tmp_path: Path) -> None:
    # Fail-closed: the chunk that crosses the cap is refused BEFORE it reaches the store.
    base_url, verifier = _origin(tmp_path)
    store = FilesystemCacheStore(tmp_path / "cache")
    with pytest.raises(SyncCapError):
        sync_index(
            base_url=base_url,
            store=store,
            adapter=FilesystemAdapter(),
            verifier=verifier,
            max_total_bytes=1,
        )
    assert list((tmp_path / "cache" / "chunks").glob("*/*")) == []
    assert store.read_active() is None


def test_sync_under_generous_caps_succeeds(tmp_path: Path) -> None:
    # The whole point of a generous default: a legitimate bundle syncs unaffected.
    base_url, verifier = _origin(tmp_path)
    result = sync_index(
        base_url=base_url,
        store=FilesystemCacheStore(tmp_path / "cache"),
        adapter=FilesystemAdapter(),
        verifier=verifier,
        max_files=10,
        max_total_bytes=10 * 1024 * 1024,
    )
    assert result.version == "1.0.0"
    assert result.bytes_fetched > 0


def test_sync_refuses_a_file_over_the_materialize_cap_during_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file bigger than the materialize memory cap must be refused DURING sync.

    The reassembly check that guards this cap runs BEFORE promote, so a manifest
    declaring a file over the cap (a runaway or malicious producer) never becomes the
    active pointer — the refusal fires at verify time, not later at materialize_file.
    """
    base_url, verifier = _origin(tmp_path)
    monkeypatch.setenv("EDGEPROC_MAX_MATERIALIZE_BYTES", "1024")
    store = FilesystemCacheStore(tmp_path / "cache")

    with pytest.raises(SyncCapError, match="materialize"):
        sync_index(base_url=base_url, store=store, adapter=FilesystemAdapter(), verifier=verifier)
    assert store.read_active() is None
