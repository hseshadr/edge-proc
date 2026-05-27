"""Detached ed25519 signatures behind ``Signer`` / ``Verifier`` Protocols.

The origin/CDN is untrusted transport; trust is re-established offline on-device by
a signature check against a pinned root-of-trust public key. So verification is
**fail-closed by construction**: ``verify`` returns ``None`` on a valid signature
and RAISES :class:`SignatureError` on anything else — tampered data, a wrong key, a
mangled signature, or a malformed (non-base64) signature string. A malformed string
must never escape as a stray ``binascii.Error``; it is normalized to
``SignatureError`` so callers have exactly one failure type to handle.

Signatures are detached and serialized as standard base64 ``str``. Keys are raw
32-byte ed25519 (the leanest form — no PEM): pinned trust-root keys are
``public_key.public_bytes_raw()``. Sigstore keyless signing is deferred behind these
same Protocols — a future implementer slots in with zero consumer change.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class SignatureError(Exception):
    """Raised when a signature is absent, malformed, or does not verify."""


@runtime_checkable
class Signer(Protocol):
    def sign(self, data: bytes) -> str: ...


@runtime_checkable
class Verifier(Protocol):
    def verify(self, data: bytes, signature: str) -> None: ...


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Fresh ed25519 keypair; the public half is the pinnable root of trust."""
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


class Ed25519Signer:
    """Produce detached base64 ed25519 signatures over canonical bytes."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key

    @classmethod
    def from_private_bytes(cls, raw: bytes) -> Ed25519Signer:
        return cls(Ed25519PrivateKey.from_private_bytes(raw))

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._key.sign(data)).decode("ascii")


class Ed25519Verifier:
    """Fail-closed verifier: returns ``None`` on a valid signature, else raises."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._key = public_key

    @classmethod
    def from_public_bytes(cls, raw: bytes) -> Ed25519Verifier:
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    def verify(self, data: bytes, signature: str) -> None:
        try:
            self._key.verify(base64.b64decode(signature, validate=True), data)
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise SignatureError("signature verification failed") from exc
