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

A key is named by its ``key_id`` (:func:`key_id_for`): the first 16 lowercase hex chars of
the SHA-256 of its raw 32 public bytes. A pointer that names its signing key is verified
through :class:`PointerVerifier`, which selects that key and refuses an unknown one
(:class:`UnknownKeyError`) or a revoked one (:class:`KeyRevokedError`). Both subclass
:class:`SignatureError`, so every existing handler already stops on them.
"""

from __future__ import annotations

import base64
import hashlib
from typing import ClassVar, Final, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from edgeproc.errors import BUNDLE_INTEGRITY_FAILED

#: Raw Ed25519 public-key length; ``key_id`` is defined over exactly these bytes.
ED25519_PUBLIC_KEY_BYTES: Final = 32
_KEY_ID_HEX_CHARS: Final = 16


def key_id_for(public_key: bytes) -> str:
    """The ``key_id`` of a raw 32-byte Ed25519 public key: ``sha256(key)`` hex, first 16.

    Shared with ``@edgeproc/browser`` — both runtimes must derive the same id from the same
    bytes, so this is defined over the RAW key only (never PEM/DER/hex text).
    """
    if len(public_key) != ED25519_PUBLIC_KEY_BYTES:
        raise ValueError(f"a key_id is defined over a raw {ED25519_PUBLIC_KEY_BYTES}-byte key")
    return hashlib.sha256(public_key).hexdigest()[:_KEY_ID_HEX_CHARS]


class SignatureError(Exception):
    """Raised when a signature is absent, malformed, or does not verify.

    Carries the canonical ``bundle.integrity_failed`` code: a signature that does not
    verify is the same trust-boundary refusal as a failed content-address check, and an
    operator should see one code for "this bundle could not be trusted". Metadata only —
    the type and message every existing handler depends on are unchanged.
    """

    code: ClassVar[str] = BUNDLE_INTEGRITY_FAILED


class UnknownKeyError(SignatureError):
    """The pointer names a ``key_id`` the pinned trust root does not hold (fail-closed)."""


class KeyRevokedError(SignatureError):
    """The pointer was signed by — or names — a key the trust root has revoked.

    A revoked key's signature never verifies, whatever the pointer's ``sequence``.
    """


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


@runtime_checkable
class PointerVerifier(Protocol):
    """A verifier that can select the signing key a pointer names by ``key_id``.

    ``sync`` prefers this over :meth:`Verifier.verify` whenever the verifier offers it, so
    a named key is selected (and refused when unknown or revoked) instead of every trusted
    key being tried. ``key_id=None`` is the legacy path: any trusted, non-revoked key.
    """

    def verify_pointer(self, data: bytes, signature: str, key_id: str | None) -> None:
        """Return ``None`` iff the (named) trusted key authenticates ``data``; else raise."""
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

    @property
    def key_id(self) -> str:
        """The ``key_id`` a consumer's keyring knows this signer's public half by."""
        return key_id_for(self._key.public_key().public_bytes_raw())

    def sign(self, data: bytes) -> str:
        return base64.b64encode(self._key.sign(data)).decode("ascii")


class Ed25519Verifier:
    """Fail-closed verifier: returns ``None`` on a valid signature, else raises."""

    def __init__(self, public_key: Ed25519PublicKey) -> None:
        self._key = public_key

    @classmethod
    def from_public_bytes(cls, raw: bytes) -> Ed25519Verifier:
        return cls(Ed25519PublicKey.from_public_bytes(raw))

    @property
    def key_id(self) -> str:
        """The ``key_id`` of the single pinned key."""
        return key_id_for(self._key.public_bytes_raw())

    def verify_pointer(self, data: bytes, signature: str, key_id: str | None) -> None:
        """A single pinned key is a keyring of one: a pointer naming another key is unknown."""
        if key_id is not None and key_id != self.key_id:
            raise UnknownKeyError(f"pointer names key_id {key_id}, not the pinned key")
        self.verify(data, signature)

    def verify(self, data: bytes, signature: str) -> None:
        try:
            self._key.verify(base64.b64decode(signature, validate=True), data)
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise SignatureError("signature verification failed") from exc
