"""Detached ed25519 signing behind Signer/Verifier Protocols (Phase A wave 3).

Trust is re-established offline on-device by a signature check, so verification is
**fail-closed by construction**: a valid signature returns ``None``, and anything
invalid — tampered data, mangled signature, wrong key, or a malformed signature
string — RAISES ``SignatureError`` rather than passing or crashing with a stray
``binascii``/``InvalidSignature`` type. These tests pin that contract.
"""

from __future__ import annotations

import base64

import pytest

from edgeproc.bundles.signing import (
    Ed25519Signer,
    Ed25519Verifier,
    SignatureError,
    Signer,
    Verifier,
    generate_keypair,
)

_DATA = b"canonical-manifest-bytes-to-be-signed"


def _signer_verifier() -> tuple[Ed25519Signer, Ed25519Verifier]:
    private, public = generate_keypair()
    return Ed25519Signer(private), Ed25519Verifier(public)


def test_round_trip_valid_signature_accepted() -> None:
    signer, verifier = _signer_verifier()
    sig = signer.sign(_DATA)
    assert verifier.verify(_DATA, sig) is None  # valid -> returns None, no raise


def test_tampered_data_rejected() -> None:
    signer, verifier = _signer_verifier()
    sig = signer.sign(_DATA)
    with pytest.raises(SignatureError):
        verifier.verify(_DATA + b"!", sig)


def test_tampered_signature_rejected() -> None:
    signer, verifier = _signer_verifier()
    sig = signer.sign(_DATA)
    raw = bytearray(base64.b64decode(sig))
    raw[0] ^= 0xFF  # flip a byte: still valid base64, wrong signature
    mangled = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(SignatureError):
        verifier.verify(_DATA, mangled)


def test_wrong_key_rejected() -> None:
    signer, _ = _signer_verifier()
    _, other_public = generate_keypair()
    other_verifier = Ed25519Verifier(other_public)
    sig = signer.sign(_DATA)
    with pytest.raises(SignatureError):
        other_verifier.verify(_DATA, sig)


def test_malformed_signature_string_fails_closed() -> None:
    _, verifier = _signer_verifier()
    with pytest.raises(SignatureError):
        verifier.verify(_DATA, "!!!")  # not valid base64 -> SignatureError, not binascii.Error


def test_raw_key_round_trip() -> None:
    private, public = generate_keypair()
    signer = Ed25519Signer.from_private_bytes(
        private.private_bytes_raw()  # type: ignore[attr-defined]
    )
    verifier = Ed25519Verifier.from_public_bytes(public.public_bytes_raw())
    sig = signer.sign(_DATA)
    assert verifier.verify(_DATA, sig) is None


def test_protocol_conformance() -> None:
    signer, verifier = _signer_verifier()
    assert isinstance(signer, Signer)
    assert isinstance(verifier, Verifier)
