"""sync_index — diff sync engine: round-trip, idempotent, patch, fail-closed.

This is the integrator: it wires manifest-v2 + chunking + signing + cas + adapters
into the casync/TUF flow — pull a tiny signed pointer, diff the chunk manifest
against the local CAS, fetch only the missing zstd chunks, verify every chunk and
both signatures on-device, then atomically swap. The headline test is the PATCH
case: edit one file in a multi-file index and prove only its changed chunks move.

Fail-closed is pinned by two tests: a tampered pointer signature and a corrupted
chunk must both RAISE and must NOT promote the bad version (a reader still sees the
old/None active pointer).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import zstandard

from edgeproc.bundles.adapters import FilesystemAdapter, HttpAdapter
from edgeproc.bundles.cas import FilesystemCacheStore, IntegrityError
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import (
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    manifest_digest,
    pointer_signing_bytes,
)
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    SignatureError,
    generate_keypair,
)
from edgeproc.bundles.sync import materialize_file, sync_index
from tests.bundles import _producer

# One small file, one tiny file, one MULTI-chunk file (> MAX_SIZE 256 KiB → ≥2 chunks).
_BIG = (b"the quick brown fox jumps over the lazy dog. " * 12_000)[: 400 * 1024]
_FILES: dict[str, bytes] = {
    "norm.json": b'{"a":1,"b":2}',
    "small.bin": b"a small payload that is under the min chunk size",
    "index.faiss": _BIG,
}


def _verifier(pub_raw: bytes) -> Ed25519Verifier:
    return Ed25519Verifier.from_public_bytes(pub_raw)


def _setup(tmp_path: Path, files: dict[str, bytes]) -> tuple[Path, VersionPointer, Ed25519Verifier]:
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    pointer = _producer.build_origin(
        files=files,
        origin=origin,
        chunker=GearCDC(),
        signer=Ed25519Signer(private),
        version="1.0.0",
    )
    verifier = _verifier(public.public_bytes_raw())
    return origin, pointer, verifier


def _manifest_at(origin: Path, pointer: VersionPointer) -> IndexManifest:
    raw = (origin / "manifest" / pointer.manifest_hash).read_bytes()
    return IndexManifest.model_validate_json(raw)


def _total_chunks(manifest: IndexManifest) -> int:
    return len({ref.hash for entry in manifest.files for ref in entry.chunks})


def test_round_trip_into_fresh_store(tmp_path: Path) -> None:
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")

    result = sync_index(
        base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
    )

    assert result.chunks_reused == 0
    assert result.chunks_fetched == _total_chunks(_manifest_at(origin, pointer))
    assert store.read_active() == pointer
    manifest = _manifest_at(origin, pointer)
    for path, original in _FILES.items():
        assert materialize_file(store, manifest, path) == original
    # The multi-chunk file really did split (proves CDC over a >256 KiB payload).
    faiss = next(e for e in manifest.files if e.path == "index.faiss")
    assert len(faiss.chunks) >= 2


def test_idempotent_resync_fetches_nothing(tmp_path: Path) -> None:
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    first = sync_index(
        base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
    )

    again = sync_index(
        base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
    )

    assert again.chunks_fetched == 0
    assert again.chunks_reused == first.chunks_fetched
    assert again.bytes_fetched == 0
    assert store.read_active() == pointer


def test_patch_fetches_only_changed_chunks(tmp_path: Path) -> None:
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    _producer.build_origin(
        files=_FILES, origin=origin, chunker=GearCDC(), signer=Ed25519Signer(private)
    )
    verifier = _verifier(public.public_bytes_raw())
    store = FilesystemCacheStore(tmp_path / "cache")
    sync_index(base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier)

    # Edit ONE file (append a few bytes), re-produce origin with a new signed pointer.
    patched = dict(_FILES)
    patched["index.faiss"] = _BIG + b"a localized few-byte edit at the tail"
    pointer2 = _producer.build_origin(
        files=patched,
        origin=origin,
        chunker=GearCDC(),
        signer=Ed25519Signer(private),
        version="1.0.1",
    )

    result = sync_index(
        base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
    )

    total = _total_chunks(_manifest_at(origin, pointer2))
    assert result.chunks_fetched >= 1
    assert result.chunks_fetched < total - 1  # only the edited file's tail chunk(s)
    assert result.chunks_reused > result.chunks_fetched
    assert store.read_active() == pointer2
    assert (
        materialize_file(store, _manifest_at(origin, pointer2), "index.faiss")
        == patched["index.faiss"]
    )


def test_fail_closed_on_bad_pointer_signature(tmp_path: Path) -> None:
    origin, _pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    # Tamper the pointer's signature at the origin (valid base64, wrong sig bytes).
    tampered = VersionPointer.model_validate_json((origin / "latest").read_bytes())
    tampered = tampered.model_copy(update={"signature": "AAAA"})
    (origin / "latest").write_bytes(tampered.model_dump_json().encode("utf-8"))

    with pytest.raises(SignatureError):
        sync_index(
            base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
        )
    assert store.read_active() is None  # bad version was NOT promoted


def test_sync_rejects_pointer_bound_to_a_different_channel(tmp_path: Path) -> None:
    """A device pinned to channel "stable" must refuse a validly signed "beta" pointer.

    One signing key can publish a beta and a stable channel of the same bundle. A
    misrouted mirror (or an attacker) serving the beta pointer to a stable-pinned
    device must not have it accepted just because the signature checks out.
    """
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    build_bundle(
        files=_FILES,
        store=FilesystemCacheStore(origin),
        chunker=GearCDC(),
        signer=Ed25519Signer(private),
        bundle_id="b",
        version="1.0.0",
        channel="beta",
    )
    verifier = _verifier(public.public_bytes_raw())
    store = FilesystemCacheStore(tmp_path / "cache")

    with pytest.raises(IntegrityError):
        sync_index(
            base_url=str(origin),
            store=store,
            adapter=FilesystemAdapter(),
            verifier=verifier,
            expected_channel="stable",
        )
    assert store.read_active() is None  # the beta pointer was NOT promoted


def test_fail_closed_on_corrupted_manifest(tmp_path: Path) -> None:
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    # A corrupt or tampering origin serves manifest bytes that don't hash to the
    # pointer's manifest_hash; /latest itself is untouched and still verifies fine.
    manifest_path = origin / "manifest" / pointer.manifest_hash
    manifest_path.write_bytes(manifest_path.read_bytes() + b"corrupted")

    with pytest.raises(IntegrityError):
        sync_index(
            base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
        )
    assert store.read_active() is None  # bad version was NOT promoted


def test_fail_closed_on_manifest_lying_about_a_files_hash(tmp_path: Path) -> None:
    """A manifest whose declared file hash is wrong must be refused, even with real chunks.

    Every chunk here is genuine and content-addresses fine on its own — only the
    manifest's claim about what they reassemble to is a lie (a forged manifest signed
    under the same key). The per-chunk CAS check can't catch this; only the file-level
    reassembly check that runs during sync does.
    """
    private, public = generate_keypair()
    signer = Ed25519Signer(private)
    verifier = _verifier(public.public_bytes_raw())
    origin = tmp_path / "origin"
    _producer.build_origin(files=_FILES, origin=origin, chunker=GearCDC(), signer=signer)
    _forge_manifest_file_hash(origin, signer, path="norm.json")
    store = FilesystemCacheStore(tmp_path / "cache")

    with pytest.raises(IntegrityError):
        sync_index(
            base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
        )
    assert store.read_active() is None  # bad version was NOT promoted


def _forge_manifest_file_hash(origin: Path, signer: Ed25519Signer, *, path: str) -> None:
    """Rewrite the manifest so ONE file's declared hash is wrong, re-signing a matching pointer.

    The chunks referenced are untouched real chunks; only the manifest's claim about what
    they reassemble to changes — the same trust gap a compromised producer or a malicious
    mirror would exploit.
    """
    pointer = VersionPointer.model_validate_json((origin / "latest").read_bytes())
    manifest = IndexManifest.model_validate_json(
        (origin / "manifest" / pointer.manifest_hash).read_bytes()
    )
    entries = [
        entry.model_copy(update={"file_sha256": "00" * 32}) if entry.path == path else entry
        for entry in manifest.files
    ]
    forged_manifest = manifest.model_copy(update={"files": entries})
    forged_hash = manifest_digest(forged_manifest)
    (origin / "manifest" / forged_hash).write_bytes(canonical_bytes(forged_manifest))
    unsigned = pointer.model_copy(update={"manifest_hash": forged_hash, "signature": ""})
    signature = signer.sign(pointer_signing_bytes(unsigned))
    forged_pointer = unsigned.model_copy(update={"signature": signature})
    (origin / "latest").write_bytes(forged_pointer.model_dump_json().encode("utf-8"))


def test_fail_closed_on_corrupted_chunk(tmp_path: Path) -> None:
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    manifest = _manifest_at(origin, pointer)
    # Corrupt one chunk file at the origin (valid zstd of the WRONG plaintext).
    victim = manifest.files[-1].chunks[0].hash
    (origin / "chunk" / victim).write_bytes(zstandard.compress(b"corrupted bytes"))

    with pytest.raises(IntegrityError):
        sync_index(
            base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier
        )
    assert store.read_active() is None  # bad version was NOT promoted


def test_filesystem_adapter_fetch_bytes_reads_file(tmp_path: Path) -> None:
    target = tmp_path / "blob.bin"
    target.write_bytes(b"some bytes")
    assert FilesystemAdapter().fetch_bytes(str(target)) == b"some bytes"


def test_http_adapter_fetch_bytes_reuses_single_client() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=b"chunkbytes:" + request.url.path.encode())

    adapter = HttpAdapter(transport=httpx.MockTransport(handler))
    first = adapter.fetch_bytes("https://cdn.example/chunk/aaa")
    second = adapter.fetch_bytes("https://cdn.example/chunk/bbb")
    adapter.close()

    assert first == b"chunkbytes:/chunk/aaa"
    assert second == b"chunkbytes:/chunk/bbb"
    assert len(calls) == 2


def test_http_adapter_rejects_oversized_body() -> None:
    # A malicious/broken origin streaming a body past the cap must be refused fail-closed,
    # never buffered whole into memory (unbounded-read DoS defense).
    from edgeproc.bundles.adapters import ResponseTooLargeError  # noqa: PLC0415

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10_000)

    adapter = HttpAdapter(transport=httpx.MockTransport(handler), max_bytes=1024)
    with pytest.raises(ResponseTooLargeError):
        adapter.fetch_bytes("https://cdn.example/chunk/huge")
    adapter.close()


def test_http_adapter_context_manager_closes_client() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"ok")

    with HttpAdapter(transport=httpx.MockTransport(handler)) as adapter:
        assert adapter.fetch_bytes("https://cdn.example/latest") == b"ok"


def test_materialize_file_fail_closed_on_unknown_path(tmp_path: Path) -> None:
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    sync_index(base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier)
    with pytest.raises(KeyError):
        materialize_file(store, _manifest_at(origin, pointer), "nope.bin")


def test_materialize_file_refuses_files_over_memory_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    sync_index(base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier)
    monkeypatch.setenv("EDGEPROC_MAX_MATERIALIZE_BYTES", "1024")

    from edgeproc.bundles.sync import SyncCapError  # noqa: PLC0415

    with pytest.raises(SyncCapError, match="materialize"):
        materialize_file(store, _manifest_at(origin, pointer), "index.faiss")


def test_materialize_file_fail_closed_on_manifest_lying_about_hash(tmp_path: Path) -> None:
    """A manifest entry claiming the wrong sha256 for a file must refuse materialize_file.

    The chunks are real and correctly stored; only the manifest's claim about what they
    reassemble to is a lie — e.g. a hand-edited or corrupted local manifest copy.
    """
    origin, pointer, verifier = _setup(tmp_path, _FILES)
    store = FilesystemCacheStore(tmp_path / "cache")
    sync_index(base_url=str(origin), store=store, adapter=FilesystemAdapter(), verifier=verifier)
    manifest = _manifest_at(origin, pointer)
    entries = [
        entry.model_copy(update={"file_sha256": "ff" * 32}) if entry.path == "norm.json" else entry
        for entry in manifest.files
    ]
    lying_manifest = manifest.model_copy(update={"files": entries})

    with pytest.raises(IntegrityError):
        materialize_file(store, lying_manifest, "norm.json")
