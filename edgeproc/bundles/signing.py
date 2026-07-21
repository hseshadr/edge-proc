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
from typing import ClassVar, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from edgeproc.errors import BUNDLE_INTEGRITY_FAILED


class SignatureError(Exception):
    """Raised when a signature is absent, malformed, or does not verify.

    Carries the canonical ``bundle.integrity_failed`` code: a signature that does not
    verify is the same trust-boundary refusal as a failed content-address check, and an
    operator should see one code for "this bundle could not be trusted". Metadata only —
    the type and message every existing handler depends on are unchanged.
    """

    code: ClassVar[str] = BUNDLE_INTEGRITY_FAILED


# FUTURE: a Sigstore keyless verifier slots in behind these same Protocols (roadmap)
# — a future implementer adds it with zero consumer change.
@runtime_checkable
class Signer(Protocol):
    """Produces a detached signature over canonical bytes."""

    def sign(self, data: bytes) -> str:
        """Sign ``data`` and return the detached signature as a base64 ``str``."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Fail-closed verifier over a detached, base64-encoded signature."""

    def verify(self, data: bytes, signature: str) -> None:
        """Return ``None`` iff ``signature`` (base64) authenticates ``data``.

        RAISES :class:`SignatureError` on anything else — a bad/forged signature, a
        wrong key, or a malformed (non-base64) string. It never returns a bool, so a
        caller cannot accidentally treat a falsy non-None as "verified".
        """
        ...


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
