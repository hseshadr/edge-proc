"""Signed-pointer hardening: identity binding + a monotonic freshness sequence.

Two trust-boundary gaps this pins closed (both additive, backward-compatible):

- **Identity binding.** The signed :class:`VersionPointer` now optionally carries a
  ``bundle_id``/``channel``; a consumer can pin an expected identity so a pointer minted
  for another bundle/channel — a cross-bundle replay under a *shared* signing key +
  transport compromise — is refused before promote. Left unset, the pointer's signed
  bytes are byte-identical to the legacy ``{manifest_hash, version}`` preimage, so every
  already-signed pointer still verifies unchanged.
- **Monotonic sequence.** An optional integer ``sequence`` gives a downstream a cheap
  freshness/anti-replay signal (strictly-greater = fresh). A provably-lower sequence is
  refused at ``promote`` alongside the existing PEP 440 anti-rollback guard.

The covenant: none of these fields is required, all default ``None``, and a legacy
pointer (carrying none of them) verifies, materializes, and promotes exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.cas import FilesystemCacheStore, IntegrityError, RollbackError
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.manifest import (
    ChunkRef,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    is_fresh_sequence,
    pointer_signing_bytes,
)
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from edgeproc.bundles.sync import sync_index

_FILES: dict[str, bytes] = {"norm.json": b'{"a":1}', "small.bin": b"a small payload"}


def _publish(
    origin: Path, signer: Ed25519Signer, *, bundle_id: str, **kw: object
) -> VersionPointer:
    return build_bundle(
        files=_FILES,
        store=FilesystemCacheStore(origin),
        chunker=GearCDC(),
        signer=signer,
        bundle_id=bundle_id,
        version="1.0.0",
        **kw,  # type: ignore[arg-type]
    )


# --- pointer_signing_bytes: the backward-compat + binding contract ---------------------


def _legacy_preimage(manifest_hash: str, version: str) -> bytes:
    """The exact pre-hardening signed bytes: a 2-field, sorted, compact JSON object."""
    return json.dumps(
        {"manifest_hash": manifest_hash, "version": version},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def test_signing_bytes_identical_to_legacy_when_identity_unset() -> None:
    # A pointer carrying none of the new fields must hash to the EXACT legacy preimage
    # (no `null` keys leaking in), so every already-signed pointer verifies byte-for-byte.
    legacy = VersionPointer(manifest_hash="cd" * 32, version="2.0.0", signature="sig")
    assert pointer_signing_bytes(legacy) == _legacy_preimage("cd" * 32, "2.0.0")
    assert b"null" not in pointer_signing_bytes(legacy)
    # The naive `exclude={"signature"}` now leaks null identity fields — the exact trap
    # `pointer_signing_bytes` exists to avoid.
    assert canonical_bytes(legacy, exclude={"signature"}) != pointer_signing_bytes(legacy)


def test_signing_bytes_bind_identity_when_set() -> None:
    # Setting bundle_id/channel/sequence folds them into the signed bytes, so the
    # signature is bound to that identity and can't be cross-applied.
    plain = VersionPointer(manifest_hash="cd" * 32, version="2.0.0", signature="s")
    bound = VersionPointer(
        manifest_hash="cd" * 32,
        version="2.0.0",
        bundle_id="A",
        channel="stable",
        sequence=7,
        signature="s",
    )
    assert pointer_signing_bytes(bound) != pointer_signing_bytes(plain)
    assert b"bundle_id" in pointer_signing_bytes(bound)


def test_legacy_signed_pointer_still_verifies_under_new_code() -> None:
    # The covenant: a signature made over the OLD 2-field preimage still verifies under the
    # new code path (pointer_signing_bytes), proving already-signed pointers are unaffected.
    private, public = generate_keypair()
    signature = Ed25519Signer(private).sign(_legacy_preimage("ab" * 32, "1.0.0"))
    signed = VersionPointer(manifest_hash="ab" * 32, version="1.0.0", signature=signature)
    Ed25519Verifier.from_public_bytes(public.public_bytes_raw()).verify(
        pointer_signing_bytes(signed), signed.signature
    )


# --- monotonic sequence freshness ------------------------------------------------------


@pytest.mark.parametrize(
    ("incoming", "active", "fresh"),
    [(5, 3, True), (3, 5, False), (5, 5, False), (None, 3, True), (5, None, True)],
)
def test_is_fresh_sequence_flags_lower_and_equal_as_stale(
    incoming: int | None, active: int | None, fresh: bool
) -> None:
    a = VersionPointer(manifest_hash="ab" * 32, version="1.0.0", sequence=incoming, signature="s")
    b = VersionPointer(manifest_hash="cd" * 32, version="1.0.0", sequence=active, signature="s")
    assert is_fresh_sequence(a, b) is fresh


def test_promote_refuses_lower_sequence(tmp_path: Path) -> None:
    # A provably-lower monotonic sequence is a rollback even if the version is not older.
    store = FilesystemCacheStore(tmp_path)
    active = _make_pointer(store, b"new", version="1.0.0", sequence=5)
    stale = _make_pointer(store, b"old", version="1.0.0", sequence=3)
    store.promote(active)
    with pytest.raises(RollbackError):
        store.promote(stale)
    assert store.read_active() == active


def test_promote_allows_equal_sequence_idempotent(tmp_path: Path) -> None:
    # Equal sequence is NOT a downgrade — an idempotent re-sync of the same pointer promotes.
    store = FilesystemCacheStore(tmp_path)
    pointer = _make_pointer(store, b"same", version="1.0.0", sequence=5)
    store.promote(pointer)
    store.promote(pointer)  # equal sequence → allowed
    assert store.read_active() == pointer


def test_promote_refuses_equal_sequence_for_different_pointer(tmp_path: Path) -> None:
    # Given
    store = FilesystemCacheStore(tmp_path)
    active = _make_pointer(store, b"active", version="1.0.0", sequence=5)
    equivocation = _make_pointer(store, b"other", version="1.0.0", sequence=5)
    store.promote(active)

    # When / Then
    with pytest.raises(RollbackError):
        store.promote(equivocation)
    assert store.read_active() == active


def _make_pointer(
    store: FilesystemCacheStore, payload: bytes, *, version: str, sequence: int
) -> VersionPointer:
    ref = ChunkRef(hash=store.put_chunk(payload), size=len(payload))
    entry = FileEntry(
        path="f",
        size=len(payload),
        file_sha256=hashlib.sha256(payload).hexdigest(),
        chunks=[ref],
    )
    manifest = IndexManifest(bundle_id="b", version=version, files=[entry])
    digest = store.put_manifest(canonical_bytes(manifest))
    return VersionPointer(manifest_hash=digest, version=version, sequence=sequence, signature="s")


# --- identity binding end-to-end: cross-bundle replay is refused -----------------------


def test_sync_accepts_matching_expected_bundle_id(tmp_path: Path) -> None:
    private, public = generate_keypair()
    origin = tmp_path / "A"
    _publish(origin, Ed25519Signer(private), bundle_id="A", bind_identity=True, channel="stable")
    verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())

    result = sync_index(
        base_url=str(origin),
        store=FilesystemCacheStore(tmp_path / "cache"),
        adapter=FilesystemAdapter(),
        verifier=verifier,
        expected_bundle_id="A",
        expected_channel="stable",
    )
    assert result.version == "1.0.0"


def test_sync_rejects_cross_bundle_replay(tmp_path: Path) -> None:
    # ONE key signs bundle A and bundle B. An attacker serves B's validly-signed pointer
    # to a device pinned to A. Identity binding refuses it BEFORE promote.
    private, public = generate_keypair()
    signer, verifier = (
        Ed25519Signer(private),
        Ed25519Verifier.from_public_bytes(public.public_bytes_raw()),
    )
    origin_b = tmp_path / "B"
    _publish(origin_b, signer, bundle_id="B", bind_identity=True)

    with pytest.raises(IntegrityError):
        sync_index(
            base_url=str(origin_b),
            store=FilesystemCacheStore(tmp_path / "cache"),
            adapter=FilesystemAdapter(),
            verifier=verifier,
            expected_bundle_id="A",
        )


def test_sync_without_expectation_is_unchanged(tmp_path: Path) -> None:
    # Backward-compat: a consumer that pins nothing gets exactly today's behavior.
    private, public = generate_keypair()
    origin = tmp_path / "A"
    _publish(origin, Ed25519Signer(private), bundle_id="A")  # no bind_identity → legacy pointer
    verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())

    result = sync_index(
        base_url=str(origin),
        store=FilesystemCacheStore(tmp_path / "cache"),
        adapter=FilesystemAdapter(),
        verifier=verifier,
    )
    assert result.version == "1.0.0"


def test_sync_rejects_pointer_identity_mismatching_manifest(tmp_path: Path) -> None:
    # A pointer that BINDS bundle_id="A" but names a manifest declaring bundle_id="B"
    # (a forged pointer under a shared key) is refused: the binding must be sound.
    private, public = generate_keypair()
    origin = tmp_path / "origin"
    _publish(origin, Ed25519Signer(private), bundle_id="realB", bind_identity=True)
    verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())
    _forge_pointer_bundle_id(origin, Ed25519Signer(private), forged="lieA")

    with pytest.raises(IntegrityError):
        sync_index(
            base_url=str(origin),
            store=FilesystemCacheStore(tmp_path / "cache"),
            adapter=FilesystemAdapter(),
            verifier=verifier,
            expected_bundle_id="lieA",
        )


def _forge_pointer_bundle_id(origin: Path, signer: Ed25519Signer, *, forged: str) -> None:
    """Rewrite /latest so the pointer claims a DIFFERENT bundle_id than its manifest, re-signed."""
    pointer = VersionPointer.model_validate_json((origin / "latest").read_bytes())
    forged_unsigned = pointer.model_copy(update={"bundle_id": forged, "signature": ""})
    signature = signer.sign(pointer_signing_bytes(forged_unsigned))
    forged_pointer = forged_unsigned.model_copy(update={"signature": signature})
    (origin / "latest").write_bytes(forged_pointer.model_dump_json().encode("utf-8"))
