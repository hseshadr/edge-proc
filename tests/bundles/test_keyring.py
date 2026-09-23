"""Trust-root keyring: ``key_id`` derivation, strict parsing, loading, and verification.

A consumer used to pin exactly ONE Ed25519 public key. The keyring lets it pin a small
set instead, name which one signed a pointer (``key_id``), and retire a key (``revoked``)
without waiting for every consumer to ship a new pin. The covenant these tests hold:

- A legacy raw 32-byte ``public.key`` (what ``edgeproc keygen`` writes) loads as a keyring
  of one and verifies EXACTLY as the single pinned key did.
- Parsing is strict: a keyring that could mean two things is refused, never guessed at.
- A signature by a revoked key never verifies, whether or not the pointer names its key.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from edgeproc.bundles.keyring import (
    KEYRING_SCHEMA,
    MAX_KEYRING_KEYS,
    MAX_REVOKED_KEY_IDS,
    MAX_TRUST_ROOT_BYTES,
    Keyring,
    KeyringVerifier,
    add_key,
    keyring_bytes,
    keyring_of,
    load_trust_root,
    parse_keyring,
    revoke_key,
)
from edgeproc.bundles.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    KeyRevokedError,
    PointerVerifier,
    SignatureError,
    UnknownKeyError,
    Verifier,
    key_id_for,
)

_DATA = b"pointer-preimage"


def _key(seed: int) -> tuple[Ed25519Signer, bytes]:
    private = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)
    return Ed25519Signer(private), private.public_key().public_bytes_raw()


_SIGNER_A, _PUB_A = _key(1)
_SIGNER_B, _PUB_B = _key(2)
_SIGNER_C, _PUB_C = _key(3)
_ID_A = key_id_for(_PUB_A)
_ID_B = key_id_for(_PUB_B)
_ID_C = key_id_for(_PUB_C)


def _doc(keys: list[bytes], revoked: list[str] | None = None) -> dict[str, object]:
    return {
        "schema": KEYRING_SCHEMA,
        "keys": [{"key_id": key_id_for(k), "public_key": k.hex()} for k in keys],
        "revoked": revoked or [],
    }


def _raw(doc: dict[str, object]) -> bytes:
    return json.dumps(doc).encode("utf-8")


# --- key_id -----------------------------------------------------------------------------


def test_key_id_is_the_first_16_hex_of_sha256_of_the_raw_public_key() -> None:
    assert key_id_for(_PUB_A) == hashlib.sha256(_PUB_A).hexdigest()[:16]
    assert len(_ID_A) == 16
    assert _ID_A != _ID_B


@pytest.mark.parametrize("length", [0, 31, 33, 64])
def test_key_id_refuses_anything_but_a_raw_32_byte_key(length: int) -> None:
    with pytest.raises(ValueError, match="32"):
        key_id_for(b"\x00" * length)


def test_signer_and_verifier_expose_the_same_key_id() -> None:
    assert _SIGNER_A.key_id == _ID_A
    assert Ed25519Verifier.from_public_bytes(_PUB_A).key_id == _ID_A


# --- strict parsing ---------------------------------------------------------------------


def test_parses_a_well_formed_keyring() -> None:
    ring = parse_keyring(_raw(_doc([_PUB_A, _PUB_B], [_ID_A])))
    assert [entry.key_id for entry in ring.keys] == [_ID_A, _ID_B]
    assert ring.revoked == [_ID_A]
    assert ring.is_revoked(_ID_A)
    assert not ring.is_revoked(_ID_B)
    assert ring.public_key(_ID_B) == _PUB_B
    assert ring.public_key(_ID_C) is None


def _refused(doc: object) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError IS a ValueError
        parse_keyring(json.dumps(doc).encode("utf-8"))


def test_refuses_an_unknown_top_level_field() -> None:
    doc = _doc([_PUB_A])
    doc["expires"] = 1
    _refused(doc)


def test_refuses_an_unknown_key_entry_field() -> None:
    doc = _doc([_PUB_A])
    doc["keys"] = [{"key_id": _ID_A, "public_key": _PUB_A.hex(), "note": "x"}]
    _refused(doc)


@pytest.mark.parametrize("schema", ["edgeproc.keyring/v2", "", None, "EDGEPROC.KEYRING/V1"])
def test_refuses_any_schema_but_the_v1_literal(schema: object) -> None:
    doc = _doc([_PUB_A])
    doc["schema"] = schema
    _refused(doc)


def test_refuses_the_python_attribute_name_in_place_of_schema() -> None:
    # `schema` is the one wire spelling; the model's internal field name is not an alias.
    doc = _doc([_PUB_A])
    doc["schema_id"] = doc.pop("schema")
    _refused(doc)


def test_refuses_a_missing_schema_or_revoked_list() -> None:
    for field in ("schema", "revoked", "keys"):
        doc = _doc([_PUB_A])
        del doc[field]
        _refused(doc)


def test_refuses_a_key_id_that_does_not_match_its_public_key() -> None:
    doc = _doc([_PUB_A])
    doc["keys"] = [{"key_id": _ID_B, "public_key": _PUB_A.hex()}]
    with pytest.raises(ValidationError, match="does not match"):
        parse_keyring(_raw(doc))


@pytest.mark.parametrize(
    "public_key",
    [
        _PUB_A.hex().upper(),  # uppercase hex is not the canonical form
        _PUB_A.hex()[:-2],  # 31 bytes
        _PUB_A.hex() + "00",  # 33 bytes
        "zz" * 32,  # not hex
    ],
)
def test_refuses_a_non_canonical_public_key(public_key: str) -> None:
    doc = _doc([_PUB_A])
    doc["keys"] = [{"key_id": _ID_A, "public_key": public_key}]
    _refused(doc)


@pytest.mark.parametrize("key_id", [_ID_A.upper(), _ID_A[:-1], _ID_A + "0", "g" * 16, 7])
def test_refuses_a_malformed_revoked_key_id(key_id: object) -> None:
    _refused(_doc([_PUB_A, _PUB_B], [key_id]))  # type: ignore[list-item]


def test_refuses_duplicate_key_ids() -> None:
    _refused(_doc([_PUB_A, _PUB_A]))


def test_refuses_duplicate_revoked_entries() -> None:
    _refused(_doc([_PUB_A, _PUB_B], [_ID_A, _ID_A]))


def test_revoked_may_name_a_key_the_ring_does_not_hold() -> None:
    ring = parse_keyring(_raw(_doc([_PUB_B], [_ID_A])))
    assert ring.is_revoked(_ID_A)
    assert ring.public_key(_ID_A) is None


def test_refuses_a_ring_whose_every_key_is_revoked() -> None:
    with pytest.raises(ValidationError, match="non-revoked"):
        parse_keyring(_raw(_doc([_PUB_A, _PUB_B], [_ID_A, _ID_B])))


def test_refuses_an_empty_key_list() -> None:
    _refused(_doc([]))


def _public_keys(count: int) -> list[bytes]:
    return [
        Ed25519PrivateKey.from_private_bytes(i.to_bytes(32, "big")).public_key().public_bytes_raw()
        for i in range(1, count + 1)
    ]


def test_limits_match_the_browser_runtime() -> None:
    assert (MAX_KEYRING_KEYS, MAX_REVOKED_KEY_IDS, MAX_TRUST_ROOT_BYTES) == (64, 1024, 65_536)


def test_accepts_exactly_the_maximum_number_of_keys_and_refuses_one_more() -> None:
    keys = _public_keys(MAX_KEYRING_KEYS + 1)
    assert len(parse_keyring(_raw(_doc(keys[:-1]))).keys) == MAX_KEYRING_KEYS
    _refused(_doc(keys))


def test_accepts_exactly_the_maximum_number_of_revocations_and_refuses_one_more() -> None:
    ids = [f"{i:016x}" for i in range(MAX_REVOKED_KEY_IDS + 1)]
    assert len(parse_keyring(_raw(_doc([_PUB_A], ids[:-1]))).revoked) == MAX_REVOKED_KEY_IDS
    _refused(_doc([_PUB_A], ids))


def test_loader_refuses_a_trust_root_over_the_byte_cap_before_parsing() -> None:
    padded = _raw(_doc([_PUB_A])) + b" " * MAX_TRUST_ROOT_BYTES
    assert parse_keyring(padded).keys  # whitespace alone is a valid document...
    with pytest.raises(ValueError, match="cap"):
        load_trust_root(padded)  # ...but the trust-root loader bounds its input first


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b'"x"', b"\xff\xfe" * 20])
def test_refuses_bytes_that_are_not_a_json_keyring_object(raw: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - every malformed shape is a ValueError
        parse_keyring(raw)


# --- the trust-root loader: legacy raw key OR JSON keyring ------------------------------


def test_loader_treats_a_raw_32_byte_public_key_as_a_keyring_of_one() -> None:
    ring = load_trust_root(_PUB_A)
    assert [entry.key_id for entry in ring.keys] == [_ID_A]
    assert ring.revoked == []


def test_loader_reads_a_json_keyring() -> None:
    ring = load_trust_root(_raw(_doc([_PUB_A, _PUB_B], [_ID_A])))
    assert ring.is_revoked(_ID_A)


@pytest.mark.parametrize("raw", [b"short", b"\x00" * 31, b"\x00" * 33])
def test_loader_refuses_neither_a_raw_key_nor_a_keyring(raw: bytes) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the CLI maps ValueError to config.invalid
        load_trust_root(raw)


# --- canonical serialization + edits ----------------------------------------------------


def test_keyring_bytes_is_canonical_regardless_of_input_order() -> None:
    forward = parse_keyring(_raw(_doc([_PUB_A, _PUB_B, _PUB_C], [_ID_C, _ID_A])))
    backward = parse_keyring(_raw(_doc([_PUB_C, _PUB_B, _PUB_A], [_ID_A, _ID_C])))
    assert keyring_bytes(forward) == keyring_bytes(backward)
    doc = json.loads(keyring_bytes(forward))
    assert [k["key_id"] for k in doc["keys"]] == sorted([_ID_A, _ID_B, _ID_C])
    assert doc["revoked"] == sorted([_ID_A, _ID_C])
    assert doc["schema"] == KEYRING_SCHEMA
    assert keyring_bytes(forward).endswith(b"}\n")
    assert parse_keyring(keyring_bytes(forward)) == parse_keyring(keyring_bytes(backward))


def test_keyring_of_builds_a_ring_from_raw_public_keys() -> None:
    ring = keyring_of([_PUB_B, _PUB_A])
    assert {entry.key_id for entry in ring.keys} == {_ID_A, _ID_B}


def test_keyring_of_refuses_duplicates_and_empty_input() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        keyring_of([_PUB_A, _PUB_A])
    with pytest.raises(ValueError):  # noqa: PT011
        keyring_of([])


def test_add_key_appends_a_new_key() -> None:
    ring = add_key(keyring_of([_PUB_A]), _PUB_B)
    assert ring.public_key(_ID_B) == _PUB_B


def test_add_key_refuses_a_key_already_in_the_ring() -> None:
    with pytest.raises(ValueError, match="already"):
        add_key(keyring_of([_PUB_A]), _PUB_A)


def test_add_key_refuses_to_re_trust_a_revoked_key() -> None:
    ring = parse_keyring(_raw(_doc([_PUB_B], [_ID_A])))
    with pytest.raises(ValueError, match="revoked"):
        add_key(ring, _PUB_A)


def test_revoke_key_marks_a_key_revoked_and_keeps_its_public_key() -> None:
    ring = revoke_key(keyring_of([_PUB_A, _PUB_B]), _ID_A)
    assert ring.is_revoked(_ID_A)
    assert ring.public_key(_ID_A) == _PUB_A


def test_revoke_key_is_idempotent() -> None:
    once = revoke_key(keyring_of([_PUB_A, _PUB_B]), _ID_A)
    assert revoke_key(once, _ID_A) == once


def test_revoke_key_may_name_an_id_the_ring_does_not_hold() -> None:
    assert revoke_key(keyring_of([_PUB_B]), _ID_A).is_revoked(_ID_A)


def test_revoke_key_refuses_a_malformed_key_id() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        revoke_key(keyring_of([_PUB_A, _PUB_B]), "not-a-key-id")


def test_revoke_key_refuses_to_revoke_the_last_active_key() -> None:
    with pytest.raises(ValueError, match="non-revoked"):
        revoke_key(keyring_of([_PUB_A]), _ID_A)


# --- verification -----------------------------------------------------------------------


def test_keyring_verifier_satisfies_both_verifier_protocols() -> None:
    verifier = KeyringVerifier(keyring_of([_PUB_A]))
    assert isinstance(verifier, Verifier)
    assert isinstance(verifier, PointerVerifier)
    assert isinstance(Ed25519Verifier.from_public_bytes(_PUB_A), PointerVerifier)


def test_a_one_key_ring_verifies_exactly_like_the_single_pinned_key() -> None:
    ring_verifier = KeyringVerifier(load_trust_root(_PUB_A))
    single = Ed25519Verifier.from_public_bytes(_PUB_A)
    good = _SIGNER_A.sign(_DATA)
    assert ring_verifier.verify(_DATA, good) is None
    assert single.verify(_DATA, good) is None
    for bad in (_SIGNER_B.sign(_DATA), "not base64!", good[:-4] + "AAA=", ""):
        with pytest.raises(SignatureError) as ring_exc:
            ring_verifier.verify(_DATA, bad)
        with pytest.raises(SignatureError) as single_exc:
            single.verify(_DATA, bad)
        assert type(ring_exc.value) is type(single_exc.value) is SignatureError


def test_unnamed_signature_verifies_under_any_non_revoked_key() -> None:
    verifier = KeyringVerifier(keyring_of([_PUB_A, _PUB_B]))
    verifier.verify(_DATA, _SIGNER_A.sign(_DATA))
    verifier.verify(_DATA, _SIGNER_B.sign(_DATA))
    with pytest.raises(SignatureError):
        verifier.verify(_DATA, _SIGNER_C.sign(_DATA))


def test_unnamed_signature_by_a_revoked_key_is_refused_as_revoked() -> None:
    verifier = KeyringVerifier(revoke_key(keyring_of([_PUB_A, _PUB_B]), _ID_A))
    with pytest.raises(KeyRevokedError):
        verifier.verify(_DATA, _SIGNER_A.sign(_DATA))
    verifier.verify(_DATA, _SIGNER_B.sign(_DATA))


def test_unnamed_signature_by_a_revoked_key_whose_public_half_is_gone_is_just_invalid() -> None:
    verifier = KeyringVerifier(parse_keyring(_raw(_doc([_PUB_B], [_ID_A]))))
    with pytest.raises(SignatureError) as exc:
        verifier.verify(_DATA, _SIGNER_A.sign(_DATA))
    assert type(exc.value) is SignatureError


def test_named_key_is_selected_and_must_verify() -> None:
    verifier = KeyringVerifier(keyring_of([_PUB_A, _PUB_B]))
    verifier.verify_pointer(_DATA, _SIGNER_B.sign(_DATA), _ID_B)
    with pytest.raises(SignatureError) as exc:
        # A's signature does not verify under the key the pointer NAMES, even though A is
        # in the ring: the named key is the only one tried.
        verifier.verify_pointer(_DATA, _SIGNER_A.sign(_DATA), _ID_B)
    assert type(exc.value) is SignatureError


def test_named_revoked_key_is_refused_before_any_signature_check() -> None:
    verifier = KeyringVerifier(revoke_key(keyring_of([_PUB_A, _PUB_B]), _ID_A))
    with pytest.raises(KeyRevokedError):
        verifier.verify_pointer(_DATA, _SIGNER_A.sign(_DATA), _ID_A)
    ring_without_a = KeyringVerifier(parse_keyring(_raw(_doc([_PUB_B], [_ID_A]))))
    with pytest.raises(KeyRevokedError):
        ring_without_a.verify_pointer(_DATA, _SIGNER_A.sign(_DATA), _ID_A)


def test_named_unknown_key_fails_closed() -> None:
    verifier = KeyringVerifier(keyring_of([_PUB_A]))
    with pytest.raises(UnknownKeyError):
        verifier.verify_pointer(_DATA, _SIGNER_C.sign(_DATA), _ID_C)


def test_verify_pointer_without_a_key_id_is_the_unnamed_path() -> None:
    verifier = KeyringVerifier(revoke_key(keyring_of([_PUB_A, _PUB_B]), _ID_A))
    verifier.verify_pointer(_DATA, _SIGNER_B.sign(_DATA), None)
    with pytest.raises(KeyRevokedError):
        verifier.verify_pointer(_DATA, _SIGNER_A.sign(_DATA), None)


def test_single_key_verifier_is_a_keyring_of_one_for_named_pointers() -> None:
    verifier = Ed25519Verifier.from_public_bytes(_PUB_A)
    verifier.verify_pointer(_DATA, _SIGNER_A.sign(_DATA), _ID_A)
    verifier.verify_pointer(_DATA, _SIGNER_A.sign(_DATA), None)
    with pytest.raises(UnknownKeyError):
        verifier.verify_pointer(_DATA, _SIGNER_B.sign(_DATA), _ID_B)
    with pytest.raises(SignatureError):
        verifier.verify_pointer(_DATA, _SIGNER_B.sign(_DATA), _ID_A)


def test_new_refusals_are_signature_errors_with_the_integrity_code() -> None:
    # Every existing `except SignatureError` handler (and the CLI) already stops on them.
    for error in (KeyRevokedError("x"), UnknownKeyError("x")):
        assert isinstance(error, SignatureError)
        assert error.code == "bundle.integrity_failed"


def test_keyring_model_rejects_direct_construction_of_an_invalid_ring() -> None:
    with pytest.raises(ValidationError):
        Keyring.model_validate({"schema": KEYRING_SCHEMA, "keys": [], "revoked": []})
