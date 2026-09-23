"""The signed pointer's ``key_id`` + ``expires_at``, end to end through publish and sync.

Both fields are OPTIONAL and signed. The compatibility covenant is the same one
``bundle_id``/``channel``/``sequence`` hold: left unset, they appear in neither the
signing preimage nor the published ``latest`` bytes, so every existing publisher output
and every existing consumer is byte-for-byte unaffected. Stamping is opt-in, because an
older consumer's ``VersionPointer`` forbids unknown fields.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.cas import FilesystemCacheStore, IntegrityError, RollbackError
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.keyring import KeyringVerifier, keyring_of, revoke_key
from edgeproc.bundles.manifest import VersionPointer, is_expired, pointer_signing_bytes
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    KeyRevokedError,
    SignatureError,
    UnknownKeyError,
    Verifier,
)
from edgeproc.bundles.sync import PointerExpiredError, sync_index, verify_pointer

_HASH = "ab" * 32
_EXPIRES = 1_767_225_600


def _key(seed: int) -> tuple[Ed25519Signer, bytes]:
    private = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    return Ed25519Signer(private), private.public_key().public_bytes_raw()


_SIGNER_A, _PUB_A = _key(1)
_SIGNER_B, _PUB_B = _key(2)
_SIGNER_C, _PUB_C = _key(3)


# --- the model --------------------------------------------------------------------------


def test_new_fields_default_to_absent() -> None:
    pointer = VersionPointer(manifest_hash=_HASH, version="1", signature="s")
    assert pointer.key_id is None
    assert pointer.expires_at is None


@pytest.mark.parametrize("key_id", ["ABCDEF0123456789", "abc", "0" * 17, "g" * 16, ""])
def test_key_id_must_be_16_lowercase_hex(key_id: str) -> None:
    with pytest.raises(ValidationError):
        VersionPointer(manifest_hash=_HASH, version="1", key_id=key_id, signature="s")


def _with_expiry(raw: str) -> str:
    return f'{{"manifest_hash":"{_HASH}","version":"1","expires_at":{raw},"signature":"s"}}'


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "9007199254740992", "1.5", "1767225600.5", "true", "false", '"1767225600"'],
)
def test_expires_at_must_be_a_positive_safe_json_integer(raw: str) -> None:
    with pytest.raises(ValidationError):
        VersionPointer.model_validate_json(_with_expiry(raw))


@pytest.mark.parametrize("raw", ["1767225600.0", "1.7672256e9"])
def test_an_integral_json_number_is_the_same_integer_as_in_the_browser(raw: str) -> None:
    # JSON.parse cannot tell `1767225600.0` from `1767225600`, so the browser accepts both;
    # so does Python, as the integer. The preimage re-canonicalizes to the integer spelling,
    # so the signature is over exactly the bytes the publisher signed.
    pointer = VersionPointer.model_validate_json(_with_expiry(raw))
    assert pointer.expires_at == 1767225600
    assert type(pointer.expires_at) is int
    assert b'"expires_at":1767225600,' in pointer_signing_bytes(pointer)


def test_expires_at_accepts_the_largest_safe_integer() -> None:
    doc = f'{{"manifest_hash":"{_HASH}","version":"1","expires_at":{2**53 - 1},"signature":"s"}}'
    assert VersionPointer.model_validate_json(doc).expires_at == 2**53 - 1


def test_expires_at_rejects_python_bool_and_fractional_float_too() -> None:
    for value in (True, False, 1767225600.5, float("inf"), float("nan"), "5"):
        with pytest.raises(ValidationError):
            VersionPointer(manifest_hash=_HASH, version="1", expires_at=value, signature="s")


def test_is_expired_is_inclusive_at_the_expiry_second() -> None:
    pointer = VersionPointer(manifest_hash=_HASH, version="1", expires_at=100, signature="s")
    assert not is_expired(pointer, 99.999)
    assert is_expired(pointer, 100)
    assert is_expired(pointer, 101)
    assert not is_expired(VersionPointer(manifest_hash=_HASH, version="1", signature="s"), 1e18)


# --- the signing preimage and the wire bytes --------------------------------------------


def test_unset_fields_leave_the_preimage_and_wire_bytes_byte_identical() -> None:
    pointer = VersionPointer(
        manifest_hash=_HASH, version="1", bundle_id="b", channel="c", sequence=3, signature="s"
    )
    assert pointer_signing_bytes(pointer) == (
        b'{"bundle_id":"b","channel":"c","manifest_hash":"' + _HASH.encode() + b'","sequence":3,'
        b'"version":"1"}'
    )
    # The pre-keyring wire format: exactly these six keys, in this order, nulls included.
    assert list(json.loads(pointer.model_dump_json())) == [
        "manifest_hash",
        "version",
        "bundle_id",
        "channel",
        "sequence",
        "signature",
    ]
    legacy = VersionPointer(manifest_hash=_HASH, version="1", signature="s")
    assert legacy.model_dump_json() == (
        f'{{"manifest_hash":"{_HASH}","version":"1","bundle_id":null,'
        '"channel":null,"sequence":null,"signature":"s"}'
    )


def test_set_fields_fold_into_the_preimage_in_sorted_key_order() -> None:
    pointer = VersionPointer(
        manifest_hash=_HASH,
        version="1",
        sequence=4,
        key_id=_SIGNER_B.key_id,
        expires_at=_EXPIRES,
        signature="s",
    )
    assert pointer_signing_bytes(pointer) == (
        b'{"expires_at":1767225600,"key_id":"'
        + _SIGNER_B.key_id.encode()
        + b'","manifest_hash":"'
        + _HASH.encode()
        + b'","sequence":4,"version":"1"}'
    )
    wire = json.loads(pointer.model_dump_json())
    assert wire["key_id"] == _SIGNER_B.key_id
    assert wire["expires_at"] == _EXPIRES
    assert VersionPointer.model_validate_json(pointer.model_dump_json()) == pointer


def test_only_one_of_the_new_fields_may_be_set() -> None:
    pointer = VersionPointer(manifest_hash=_HASH, version="1", expires_at=5, signature="s")
    assert b"key_id" not in pointer_signing_bytes(pointer)
    assert "key_id" not in pointer.model_dump_json()
    assert b'"expires_at":5' in pointer_signing_bytes(pointer)


# --- publish ----------------------------------------------------------------------------


def _publish(origin: Path, signer: Ed25519Signer, *, sequence: int, **kw: object) -> VersionPointer:
    return build_bundle(
        files={"release.txt": f"release {sequence}".encode()},
        store=FilesystemCacheStore(origin),
        chunker=GearCDC(),
        signer=signer,
        bundle_id="rotating",
        version=f"{sequence}.0.0",
        channel="stable",
        sequence=sequence,
        bind_identity=True,
        **kw,  # type: ignore[arg-type]
    )


def test_default_publish_writes_the_pre_keyring_latest_bytes(tmp_path: Path) -> None:
    pointer = _publish(tmp_path / "o", _SIGNER_A, sequence=1)
    latest = (tmp_path / "o" / "latest").read_bytes()
    assert b"key_id" not in latest
    assert b"expires_at" not in latest
    assert list(json.loads(latest)) == [
        "manifest_hash",
        "version",
        "bundle_id",
        "channel",
        "sequence",
        "signature",
    ]
    Ed25519Verifier.from_public_bytes(_PUB_A).verify(
        pointer_signing_bytes(pointer), pointer.signature
    )


def test_stamped_publish_signs_key_id_and_expiry(tmp_path: Path) -> None:
    pointer = _publish(
        tmp_path / "o", _SIGNER_A, sequence=1, key_id=_SIGNER_A.key_id, expires_at=_EXPIRES
    )
    latest = VersionPointer.model_validate_json((tmp_path / "o" / "latest").read_bytes())
    assert latest == pointer
    assert latest.key_id == _SIGNER_A.key_id
    assert latest.expires_at == _EXPIRES
    Ed25519Verifier.from_public_bytes(_PUB_A).verify(
        pointer_signing_bytes(latest), latest.signature
    )


# --- sync: key selection, revocation, rotation ------------------------------------------


def _sync(origin: Path, cache: FilesystemCacheStore, verifier: Verifier, now: float = 0.0) -> str:
    return sync_index(
        base_url=str(origin),
        store=cache,
        adapter=FilesystemAdapter(),
        verifier=verifier,
        clock=lambda: now,
    ).version


def _ring(*keys: bytes, revoked: tuple[str, ...] = ()) -> KeyringVerifier:
    ring = keyring_of(keys)
    for key_id in revoked:
        ring = revoke_key(ring, key_id)
    return KeyringVerifier(ring)


def test_rotation_through_the_overlap_window(tmp_path: Path) -> None:
    """ring {A} → ring {A,B} → publisher switches to B → ring {B, revoked A}."""
    cache = FilesystemCacheStore(tmp_path / "cache")
    a1 = tmp_path / "a1"
    _publish(a1, _SIGNER_A, sequence=1, key_id=_SIGNER_A.key_id)
    b2 = tmp_path / "b2"
    _publish(b2, _SIGNER_B, sequence=2, key_id=_SIGNER_B.key_id)
    b3 = tmp_path / "b3"
    _publish(b3, _SIGNER_B, sequence=3, key_id=_SIGNER_B.key_id)

    # Consumer ships ring {A,B} while the publisher still signs with A.
    overlap = _ring(_PUB_A, _PUB_B)
    assert _sync(a1, cache, overlap) == "1.0.0"
    # Publisher switches to B: the same consumer accepts it with no consumer release.
    assert _sync(b2, cache, overlap) == "2.0.0"
    # Consumer then retires A.
    retired = _ring(_PUB_A, _PUB_B, revoked=(_SIGNER_A.key_id,))
    assert _sync(b3, cache, retired) == "3.0.0"


def test_old_a_signed_pointer_at_a_lower_sequence_is_refused_after_rotation(
    tmp_path: Path,
) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    overlap = _ring(_PUB_A, _PUB_B)
    old = tmp_path / "old"
    _publish(old, _SIGNER_A, sequence=1, key_id=_SIGNER_A.key_id)
    new = tmp_path / "new"
    _publish(new, _SIGNER_B, sequence=2, key_id=_SIGNER_B.key_id)
    _sync(new, cache, overlap)
    active = cache.read_active()

    with pytest.raises(RollbackError):
        _sync(old, cache, overlap)
    assert cache.read_active() == active


@pytest.mark.parametrize("stamp", [True, False])
def test_a_revoked_key_is_refused_even_at_a_higher_sequence(tmp_path: Path, stamp: bool) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    retired = _ring(_PUB_A, _PUB_B, revoked=(_SIGNER_A.key_id,))
    good = tmp_path / "good"
    _publish(good, _SIGNER_B, sequence=2, key_id=_SIGNER_B.key_id)
    _sync(good, cache, retired)
    active = cache.read_active()
    # The compromised key signs a "fresher" release — named or unnamed, it never verifies.
    attack = tmp_path / "attack"
    _publish(attack, _SIGNER_A, sequence=99, key_id=_SIGNER_A.key_id if stamp else None)

    with pytest.raises(KeyRevokedError):
        _sync(attack, cache, retired)
    assert cache.read_active() == active


def test_a_pointer_naming_a_key_outside_the_ring_fails_closed(tmp_path: Path) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_C, sequence=1, key_id=_SIGNER_C.key_id)
    with pytest.raises(UnknownKeyError):
        _sync(origin, cache, _ring(_PUB_A, _PUB_B))
    assert cache.read_active() is None


def test_a_forged_key_id_cannot_redirect_verification(tmp_path: Path) -> None:
    # C signs a pointer but claims A's key_id: A's key is selected and C's signature fails.
    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_C, sequence=1, key_id=_SIGNER_A.key_id)
    with pytest.raises(SignatureError):
        _sync(origin, cache, _ring(_PUB_A, _PUB_B))
    assert cache.read_active() is None


def test_a_single_pinned_key_still_syncs_legacy_and_self_named_pointers(tmp_path: Path) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    pinned = Ed25519Verifier.from_public_bytes(_PUB_A)
    legacy = tmp_path / "legacy"
    _publish(legacy, _SIGNER_A, sequence=1)
    named = tmp_path / "named"
    _publish(named, _SIGNER_A, sequence=2, key_id=_SIGNER_A.key_id)
    other = tmp_path / "other"
    _publish(other, _SIGNER_B, sequence=3, key_id=_SIGNER_B.key_id)

    assert _sync(legacy, cache, pinned) == "1.0.0"
    assert _sync(named, cache, pinned) == "2.0.0"
    with pytest.raises(UnknownKeyError):
        _sync(other, cache, pinned)


class _PlainVerifier:
    """A third-party ``Verifier`` that predates key selection (``verify`` only)."""

    def __init__(self, public_key: bytes) -> None:
        self._inner = Ed25519Verifier.from_public_bytes(public_key)
        self.calls = 0

    def verify(self, data: bytes, signature: str) -> None:
        self.calls += 1
        self._inner.verify(data, signature)


def test_a_verify_only_verifier_still_authenticates_named_pointers(tmp_path: Path) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    plain = _PlainVerifier(_PUB_A)
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_A, sequence=1, key_id=_SIGNER_A.key_id)
    assert _sync(origin, cache, plain) == "1.0.0"
    assert plain.calls == 1
    forged = tmp_path / "forged"
    _publish(forged, _SIGNER_B, sequence=2, key_id=_SIGNER_A.key_id)
    with pytest.raises(SignatureError):
        _sync(forged, cache, plain)


# --- sync: expiry ---------------------------------------------------------------------


def test_expired_pointer_is_refused_and_nothing_promotes(tmp_path: Path) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_A, sequence=1, expires_at=_EXPIRES)
    verifier = _ring(_PUB_A)

    with pytest.raises(PointerExpiredError):
        _sync(origin, cache, verifier, now=_EXPIRES)
    with pytest.raises(PointerExpiredError):
        _sync(origin, cache, verifier, now=_EXPIRES + 1)
    assert cache.read_active() is None
    assert _sync(origin, cache, verifier, now=_EXPIRES - 1) == "1.0.0"


def test_expiry_is_an_integrity_refusal() -> None:
    error = PointerExpiredError("x")
    assert isinstance(error, IntegrityError)
    assert error.code == "bundle.integrity_failed"


def test_expiry_is_checked_only_after_the_signature(tmp_path: Path) -> None:
    # An unsigned (tampered) expiry must never be what decides: the signature fails first.
    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_A, sequence=1, expires_at=_EXPIRES)
    latest = origin / "latest"
    doc = json.loads(latest.read_bytes())
    doc["expires_at"] = 1
    latest.write_text(json.dumps(doc))
    with pytest.raises(SignatureError):
        _sync(origin, cache, _ring(_PUB_A), now=_EXPIRES + 10)


def test_expiry_never_consults_the_clock_for_an_unstamped_pointer(tmp_path: Path) -> None:
    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_A, sequence=1)

    def _clock() -> float:
        raise AssertionError("clock read for a pointer with no expires_at")

    result = sync_index(
        base_url=str(origin),
        store=cache,
        adapter=FilesystemAdapter(),
        verifier=_ring(_PUB_A),
        clock=_clock,
    )
    assert result.version == "1.0.0"


def test_sync_defaults_to_the_wall_clock(tmp_path: Path) -> None:
    # A far-past expiry is refused with no clock injected: the default clock is real time.
    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    _publish(origin, _SIGNER_A, sequence=1, expires_at=1)
    with pytest.raises(PointerExpiredError):
        sync_index(
            base_url=str(origin), store=cache, adapter=FilesystemAdapter(), verifier=_ring(_PUB_A)
        )


# --- verify_pointer: the pointer-level stage of sync, on its own ------------------------


def _signed(signer: Ed25519Signer, **fields: object) -> VersionPointer:
    unsigned = VersionPointer(manifest_hash=_HASH, version="1", signature="", **fields)  # type: ignore[arg-type]
    return unsigned.model_copy(update={"signature": signer.sign(pointer_signing_bytes(unsigned))})


def test_verify_pointer_applies_key_selection_signature_and_expiry() -> None:
    ring = _ring(_PUB_A, _PUB_B, revoked=(_SIGNER_A.key_id,))
    fresh = _signed(_SIGNER_B, key_id=_SIGNER_B.key_id, expires_at=_EXPIRES)
    assert verify_pointer(fresh, ring, clock=lambda: _EXPIRES - 1) is None
    with pytest.raises(PointerExpiredError):
        verify_pointer(fresh, ring, clock=lambda: _EXPIRES)
    with pytest.raises(KeyRevokedError):
        verify_pointer(_signed(_SIGNER_A, key_id=_SIGNER_A.key_id), ring, clock=lambda: 0.0)
    with pytest.raises(UnknownKeyError):
        verify_pointer(_signed(_SIGNER_C, key_id=_SIGNER_C.key_id), ring, clock=lambda: 0.0)


def test_verify_pointer_defaults_to_the_wall_clock() -> None:
    with pytest.raises(PointerExpiredError):
        verify_pointer(_signed(_SIGNER_A, expires_at=1), _ring(_PUB_A))


def test_an_unstamped_pointer_takes_the_exact_pre_keyring_verify_call(tmp_path: Path) -> None:
    # A consumer's duck-typed verifier (a bare Mock has EVERY attribute, verify_pointer
    # included) must see the same `verify(data, signature)` call for a legacy pointer.
    from unittest.mock import Mock  # noqa: PLC0415

    cache = FilesystemCacheStore(tmp_path / "cache")
    origin = tmp_path / "o"
    pointer = _publish(origin, _SIGNER_A, sequence=1)
    verifier = Mock()
    sync_index(base_url=str(origin), store=cache, adapter=FilesystemAdapter(), verifier=verifier)
    verifier.verify.assert_called_once_with(pointer_signing_bytes(pointer), pointer.signature)
    verifier.verify_pointer.assert_not_called()
