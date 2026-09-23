"""The trust-root keyring: several pinned Ed25519 keys, named by ``key_id``, some revoked.

A consumer used to pin exactly one public key, so a rotation needed a coordinated cutover
and a compromised key stayed trusted until every consumer shipped a new pin. A keyring lets
a consumer pin ``{old, new}`` through an overlap window and then ``{new, revoked old}``.

The on-disk form is a strict JSON document (``edgeproc.keyring/v1``)::

    {"schema": "edgeproc.keyring/v1",
     "keys": [{"key_id": "<16 hex>", "public_key": "<64 hex: raw 32 bytes>"}],
     "revoked": ["<16 hex>", ...]}

Parsing refuses anything ambiguous: an unknown field, another schema, a ``key_id`` that is
not the derived id of its ``public_key``, a duplicate id, or a ring with no key left
unrevoked. ``revoked`` may name ids the ring does not hold (a key retired before this
consumer ever pinned it), and a key listed in both is revoked. A ring holds 1-64 keys and at
most 1024 revocations, and a trust-root file is at most 64 KiB. The same JSON parses
identically in ``@edgeproc/browser``.

:func:`load_trust_root` is the one loader behind ``EDGEPROC_TRUST_ROOT_PUBKEY_PATH`` and
``--key``: a raw 32-byte ``public.key`` — what ``edgeproc keygen`` writes — is a keyring of
one, which verifies exactly as the single pinned key always did.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from edgeproc.bundles.manifest import KeyId, validate_key_id
from edgeproc.bundles.signing import (
    ED25519_PUBLIC_KEY_BYTES,
    Ed25519Verifier,
    KeyRevokedError,
    SignatureError,
    UnknownKeyError,
    key_id_for,
)

KEYRING_SCHEMA: Final = "edgeproc.keyring/v1"
# Protocol limits shared with `@edgeproc/browser`, so both runtimes accept and refuse the
# same documents. They bound the work an oversized trust root can cause; they are part of
# the format, not deployment tunables.
MAX_KEYRING_KEYS: Final = 64
MAX_REVOKED_KEY_IDS: Final = 1024
MAX_TRUST_ROOT_BYTES: Final = 64 * 1024
_PUBLIC_KEY_HEX: Final = re.compile(r"[0-9a-f]{64}")


def _validate_public_key_hex(value: str) -> str:
    if _PUBLIC_KEY_HEX.fullmatch(value) is None:
        raise ValueError("invalid public_key: expected 64 lowercase hex chars (raw 32 bytes)")
    Ed25519Verifier.from_public_bytes(bytes.fromhex(value))  # refuses a non-key up front
    return value


type PublicKeyHex = Annotated[str, AfterValidator(_validate_public_key_hex)]


class KeyringEntry(BaseModel):
    """One trusted key: its raw public bytes (hex) and the ``key_id`` derived from them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key_id: KeyId
    public_key: PublicKeyHex

    @model_validator(mode="after")
    def _key_id_is_derived(self) -> Self:
        if key_id_for(bytes.fromhex(self.public_key)) != self.key_id:
            raise ValueError(f"key_id {self.key_id} does not match its public_key")
        return self

    @property
    def public_bytes(self) -> bytes:
        return bytes.fromhex(self.public_key)


class Keyring(BaseModel):
    """A strict, validated trust root: at least one key that is not revoked."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["edgeproc.keyring/v1"] = Field(alias="schema")
    keys: list[KeyringEntry] = Field(min_length=1, max_length=MAX_KEYRING_KEYS)
    revoked: list[KeyId] = Field(max_length=MAX_REVOKED_KEY_IDS)

    @model_validator(mode="after")
    def _is_unambiguous(self) -> Self:
        ids = [entry.key_id for entry in self.keys]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate key_id in keys")
        if len(set(self.revoked)) != len(self.revoked):
            raise ValueError("duplicate key_id in revoked")
        if not self.active_keys():
            raise ValueError("a keyring needs at least one non-revoked key")
        return self

    def is_revoked(self, key_id: str) -> bool:
        return key_id in self.revoked

    def public_key(self, key_id: str) -> bytes | None:
        """The raw public key held under ``key_id`` (revoked or not), else ``None``."""
        return next((e.public_bytes for e in self.keys if e.key_id == key_id), None)

    def active_keys(self) -> list[KeyringEntry]:
        return [entry for entry in self.keys if entry.key_id not in self.revoked]

    def revoked_keys(self) -> list[KeyringEntry]:
        return [entry for entry in self.keys if entry.key_id in self.revoked]


def _entry(public_key: bytes) -> KeyringEntry:
    return KeyringEntry(key_id=key_id_for(public_key), public_key=public_key.hex())


def _ring(keys: Iterable[KeyringEntry], revoked: Iterable[str]) -> Keyring:
    """Build a validated ring in canonical order (keys and revoked sorted by id)."""
    return Keyring(
        schema=KEYRING_SCHEMA,
        keys=sorted(keys, key=lambda e: e.key_id),
        revoked=sorted(revoked),
    )


def keyring_of(public_keys: Iterable[bytes]) -> Keyring:
    """A ring trusting each raw 32-byte public key, none revoked (``ValueError`` if invalid)."""
    return _ring((_entry(key) for key in public_keys), ())


def parse_keyring(raw: bytes) -> Keyring:
    """Strictly parse a JSON keyring document; any malformation is a ``ValueError``."""
    return Keyring.model_validate_json(raw)


def load_trust_root(raw: bytes) -> Keyring:
    """Load a pinned trust root: a legacy raw 32-byte key (ring of one) or a JSON keyring.

    The two are unambiguous by length — no valid keyring document is 32 bytes long. Input
    over :data:`MAX_TRUST_ROOT_BYTES` is refused before any parse.
    """
    if len(raw) > MAX_TRUST_ROOT_BYTES:
        raise ValueError(f"trust root exceeds the {MAX_TRUST_ROOT_BYTES}-byte cap")
    if len(raw) == ED25519_PUBLIC_KEY_BYTES:
        return keyring_of([raw])
    return parse_keyring(raw)


def add_key(ring: Keyring, public_key: bytes) -> Keyring:
    """Return ``ring`` plus ``public_key``; refuse a duplicate or a revoked key."""
    key_id = key_id_for(public_key)
    if ring.is_revoked(key_id):
        raise ValueError(f"key_id {key_id} is revoked; refusing to trust it again")
    if ring.public_key(key_id) is not None:
        raise ValueError(f"key_id {key_id} is already in the keyring")
    return _ring([*ring.keys, _entry(public_key)], ring.revoked)


def revoke_key(ring: Keyring, key_id: str) -> Keyring:
    """Return ``ring`` with ``key_id`` revoked (idempotent; its public key is kept).

    Keeping the revoked key's public half lets a consumer report an unnamed pointer signed
    by it as revoked rather than merely invalid. Revoking the last active key is refused.
    """
    validate_key_id(key_id)
    if ring.is_revoked(key_id):
        return ring
    return _ring(ring.keys, [*ring.revoked, key_id])


def keyring_bytes(ring: Keyring) -> bytes:
    """The canonical file form: sorted, 2-space indented JSON with a trailing newline."""
    doc = {
        "schema": ring.schema_id,
        "keys": [e.model_dump() for e in sorted(ring.keys, key=lambda e: e.key_id)],
        "revoked": sorted(ring.revoked),
    }
    return (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")


class KeyringVerifier:
    """Fail-closed verifier over a :class:`Keyring` (a ``Verifier`` and a ``PointerVerifier``).

    - A pointer that NAMES its key: a revoked id raises :class:`KeyRevokedError`, an id the
      ring does not hold raises :class:`UnknownKeyError`, and otherwise ONLY that key is
      tried — a failure is a plain :class:`SignatureError`.
    - A pointer that names no key (legacy): the signature must verify under at least one
      NON-revoked key. If it verifies only under a revoked key it is refused as
      :class:`KeyRevokedError`; otherwise as :class:`SignatureError`.

    With a ring of one unrevoked key both paths are exactly the single-pinned-key check.
    """

    def __init__(self, ring: Keyring) -> None:
        self._ring = ring

    def verify(self, data: bytes, signature: str) -> None:
        if _any_verifies(self._ring.active_keys(), data, signature):
            return
        if _any_verifies(self._ring.revoked_keys(), data, signature):
            raise KeyRevokedError("pointer is signed by a revoked key")
        raise SignatureError("signature verification failed")

    def verify_pointer(self, data: bytes, signature: str, key_id: str | None) -> None:
        if key_id is None:
            self.verify(data, signature)
            return
        if self._ring.is_revoked(key_id):
            raise KeyRevokedError(f"pointer names revoked key_id {key_id}")
        public_key = self._ring.public_key(key_id)
        if public_key is None:
            raise UnknownKeyError(f"pointer names key_id {key_id}, which the keyring lacks")
        Ed25519Verifier.from_public_bytes(public_key).verify(data, signature)


def _any_verifies(entries: Iterable[KeyringEntry], data: bytes, signature: str) -> bool:
    return any(_verifies(entry, data, signature) for entry in entries)


def _verifies(entry: KeyringEntry, data: bytes, signature: str) -> bool:
    try:
        Ed25519Verifier.from_public_bytes(entry.public_bytes).verify(data, signature)
    except SignatureError:
        return False
    return True
