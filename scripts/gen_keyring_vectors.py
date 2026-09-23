#!/usr/bin/env python3
"""Generate the keyring's cross-runtime fixtures from FIXED seeds (A = 32 x 0x01, B = 32 x 0x02).

Two files, both deterministic (Ed25519 signatures are deterministic):

``tests/fixtures/keyring_vectors.json`` — the SHARED contract with ``@edgeproc/browser``.
    Byte-identical to the browser runtime's copy: compact, key-sorted JSON with no trailing
    newline. It pins the ``key_id`` derivation, the signing preimage with ``key_id`` /
    ``expires_at`` folded in, the exact signatures, and the verdicts under ring
    ``b_revoked_a`` either side of pointer ``d``'s expiry. ``manifest_hash`` is 64 ``0``:
    these vectors exercise the pointer stage only.

``tests/fixtures/keyring_scenarios.json`` — Python's wider matrix over a REAL manifest, so
    each case runs through a complete ``sync_index``: rotation overlap, rollback floors, a
    single legacy pin, and check precedence.

Key ids, preimages, signatures, and wire JSON are derived through the shipped library. The
verdicts are DECLARED here as data and never computed: the test suite drives each one
through the real verifier / sync, so a verdict can never agree with the library merely
because the library produced it.

Run it:
    uv run python scripts/gen_keyring_vectors.py            # (re)write both fixtures
    uv run python scripts/gen_keyring_vectors.py --check    # exit 1 if either is stale
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from edgeproc.bundles.keyring import Keyring, keyring_bytes, keyring_of, revoke_key
from edgeproc.bundles.manifest import (
    ChunkRef,
    FileEntry,
    IndexManifest,
    VersionPointer,
    canonical_bytes,
    manifest_digest,
    pointer_signing_bytes,
)
from edgeproc.bundles.signing import Ed25519Signer, key_id_for

FIXTURES: Final = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
SHARED_NAME: Final = "keyring_vectors.json"
SCENARIOS_NAME: Final = "keyring_scenarios.json"

SEEDS: Final = {"A": b"\x01" * 32, "B": b"\x02" * 32}
BUNDLE_ID: Final = "vectors"
CHANNEL: Final = "stable"
VERSION: Final = "2026-01-01"
EXPIRES_AT: Final = 1767225600  # 2026-01-01T00:00:00Z
BEFORE: Final = EXPIRES_AT - 1
AT: Final = EXPIRES_AT
SHARED_MANIFEST_HASH: Final = "0" * 64
# One file below the chunker's minimum size, so it is exactly one chunk on every runtime.
PAYLOAD: Final = {"hello.txt": "edgeproc keyring vectors\n"}

type PointerSpec = tuple[str, str, int, bool, int | None]

#: (name, signer, sequence, stamp key_id?, expires_at)
POINTERS: Final[tuple[PointerSpec, ...]] = (
    ("a", "A", 1, False, None),
    ("b", "A", 2, True, None),
    ("c", "B", 3, True, None),
    ("d", "B", 4, True, EXPIRES_AT),
)

#: Shared verdicts under ring ``b_revoked_a`` (keys [B], revoked [A]). Declared, not computed.
SHARED_OUTCOMES: Final = {
    "b_revoked_a": {
        str(BEFORE): {"a": "signature_error", "b": "key_revoked", "c": "ok", "d": "ok"},
        str(AT): {"a": "signature_error", "b": "key_revoked", "c": "ok", "d": "expired"},
    }
}

#: Scenario verdicts: (ring, pointer, now, floor pointer promoted first, verdict). Declared.
SCENARIOS: Final = (
    # Ring {B, revoked A} with A's public key GONE (the shared ring): A is simply invalid.
    ("b_revoked_a", "a", BEFORE, None, "signature_error"),
    ("b_revoked_a", "b", BEFORE, None, "key_revoked"),
    ("b_revoked_a", "c", AT, None, "ok"),
    ("b_revoked_a", "d", BEFORE, None, "ok"),
    ("b_revoked_a", "d", AT, None, "expired"),
    # The same revocation with A's public key still LISTED: an unnamed A signature is
    # recognised and refused by name. A key in both lists is revoked.
    ("b_revoked_a_listed", "a", BEFORE, None, "key_revoked"),
    ("b_revoked_a_listed", "b", AT, None, "key_revoked"),
    ("b_revoked_a_listed", "c", BEFORE, None, "ok"),
    ("b_revoked_a_listed", "d", AT, None, "expired"),
    # Rollback floor: once d (seq 4) is active, c (seq 3) is refused; the reverse promotes.
    ("b_revoked_a", "c", BEFORE, "d", "rollback"),
    ("b_revoked_a", "d", BEFORE, "c", "ok"),
    # Overlap window {A, B}: every pointer verifies; the floor still orders them across keys.
    ("ab", "a", BEFORE, None, "ok"),
    ("ab", "b", BEFORE, None, "ok"),
    ("ab", "c", BEFORE, None, "ok"),
    ("ab", "d", BEFORE, None, "ok"),
    ("ab", "d", AT, None, "expired"),
    ("ab", "a", BEFORE, "c", "rollback"),
    ("ab", "c", BEFORE, "a", "ok"),
    # A legacy single pin (keyring of one, A): a B-named pointer is unknown, expired or not.
    ("a_only", "a", BEFORE, None, "ok"),
    ("a_only", "b", BEFORE, None, "ok"),
    ("a_only", "c", BEFORE, None, "unknown_key"),
    ("a_only", "d", AT, None, "unknown_key"),
    # A ring that never held A.
    ("b_only", "a", BEFORE, None, "signature_error"),
    ("b_only", "b", BEFORE, None, "unknown_key"),
    ("b_only", "c", BEFORE, None, "ok"),
)

CHECK_ORDER: Final = (
    "parse the pointer strictly (key_id 16 lowercase hex; expires_at an integer 0 < v < 2^53)",
    "key selection: named key revoked -> key_revoked; named key not in the ring -> unknown_key",
    "signature: the named key only, else any non-revoked key; an unnamed signature verifying "
    "only under a revoked key still listed in keys -> key_revoked; otherwise signature_error",
    "expiry, only after the signature verified: now >= expires_at -> expired",
    "identity pins, manifest hash, chunks, reassembly",
    "promote: sequence strictly greater than the active floor, else rollback",
)


def _public(name: str) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(SEEDS[name]).public_key().public_bytes_raw()


def _signer(name: str) -> Ed25519Signer:
    return Ed25519Signer(Ed25519PrivateKey.from_private_bytes(SEEDS[name]))


def _id(name: str) -> str:
    return key_id_for(_public(name))


def _ring_doc(ring: Keyring) -> object:
    return json.loads(keyring_bytes(ring))


def _sign(manifest_hash: str, spec: PointerSpec) -> VersionPointer:
    _, signer_name, sequence, stamp, expires_at = spec
    signer = _signer(signer_name)
    unsigned = VersionPointer(
        manifest_hash=manifest_hash,
        version=VERSION,
        bundle_id=BUNDLE_ID,
        channel=CHANNEL,
        sequence=sequence,
        key_id=signer.key_id if stamp else None,
        expires_at=expires_at,
        signature="",
    )
    signature = signer.sign(pointer_signing_bytes(unsigned))
    return unsigned.model_copy(update={"signature": signature})


def _signature_hex(pointer: VersionPointer) -> str:
    return base64.b64decode(pointer.signature).hex()


def _shared_pointer(spec: PointerSpec) -> dict[str, object]:
    pointer = _sign(SHARED_MANIFEST_HASH, spec)
    wire = {k: v for k, v in json.loads(pointer.model_dump_json()).items() if v is not None}
    return {
        "pointer": wire,
        "preimage_hex": pointer_signing_bytes(pointer).hex(),
        "signature_hex": _signature_hex(pointer),
    }


def build_shared_vectors() -> dict[str, object]:
    """The shared contract, shaped exactly as ``@edgeproc/browser`` commits it."""
    a, b = _public("A"), _public("B")
    return {
        "key_ids": {name: _id(name) for name in SEEDS},
        "keyrings": {
            "ab": _ring_doc(keyring_of([a, b])),
            "b_revoked_a": _ring_doc(revoke_key(keyring_of([b]), _id("A"))),
        },
        "outcomes": SHARED_OUTCOMES,
        "pointers": {spec[0]: _shared_pointer(spec) for spec in POINTERS},
    }


def render_shared(vectors: dict[str, object]) -> str:
    """Compact, key-sorted, no trailing newline — the byte form both runtimes commit."""
    return json.dumps(vectors, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _file_entry(path: str, text: str) -> FileEntry:
    """One single-chunk file: the chunk IS the file, so both hash to the same digest."""
    data = text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    chunk = ChunkRef(hash=digest, size=len(data))
    return FileEntry(path=path, size=len(data), file_sha256=digest, chunks=[chunk])


def _manifest() -> IndexManifest:
    files = [_file_entry(path, text) for path, text in PAYLOAD.items()]
    return IndexManifest(bundle_id=BUNDLE_ID, version=VERSION, files=files)


def _scenario_rings() -> dict[str, Keyring]:
    a, b = _public("A"), _public("B")
    both = keyring_of([a, b])
    return {
        "ab": both,
        "b_revoked_a": revoke_key(keyring_of([b]), _id("A")),
        "b_revoked_a_listed": revoke_key(both, _id("A")),
        "a_only": keyring_of([a]),
        "b_only": keyring_of([b]),
    }


def _scenario_pointer(manifest_hash: str, spec: PointerSpec) -> dict[str, object]:
    pointer = _sign(manifest_hash, spec)
    return {
        "signer": spec[1],
        "wire_json": pointer.model_dump_json(),
        "preimage_hex": pointer_signing_bytes(pointer).hex(),
        "signature_hex": _signature_hex(pointer),
    }


def _key_doc(name: str) -> dict[str, str]:
    return {"seed_hex": SEEDS[name].hex(), "public_key": _public(name).hex(), "key_id": _id(name)}


def build_scenarios() -> dict[str, object]:
    """Python's wider matrix over a real manifest, for complete ``sync_index`` runs."""
    manifest = _manifest()
    manifest_hash = manifest_digest(manifest)
    return {
        "schema": "edgeproc.keyring-scenarios/v1",
        "generator": "scripts/gen_keyring_vectors.py",
        "keys": {name: _key_doc(name) for name in SEEDS},
        "keyrings": {name: _ring_doc(ring) for name, ring in _scenario_rings().items()},
        "payload": {"bundle_id": BUNDLE_ID, "version": VERSION, "files": dict(PAYLOAD)},
        "manifest_canonical": canonical_bytes(manifest).decode("utf-8"),
        "manifest_hash": manifest_hash,
        "pointers": {spec[0]: _scenario_pointer(manifest_hash, spec) for spec in POINTERS},
        "check_order": list(CHECK_ORDER),
        "cases": [
            {"ring": ring, "pointer": pointer, "now": now, "floor": floor, "expected": verdict}
            for ring, pointer, now, floor, verdict in SCENARIOS
        ],
    }


def render_scenarios(scenarios: dict[str, object]) -> str:
    return json.dumps(scenarios, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def rendered() -> dict[str, str]:
    """File name -> exact committed text, for every fixture this script owns."""
    return {
        SHARED_NAME: render_shared(build_shared_vectors()),
        SCENARIOS_NAME: render_scenarios(build_scenarios()),
    }


def _read(path: Path) -> str | None:
    return path.read_bytes().decode("utf-8") if path.exists() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the keyring cross-runtime fixtures.")
    parser.add_argument("--out-dir", type=Path, default=FIXTURES)
    parser.add_argument("--check", action="store_true", help="fail if a fixture is stale")
    args = parser.parse_args(argv)
    out_dir: Path = args.out_dir
    texts = rendered()
    if args.check:
        stale = [name for name, text in texts.items() if _read(out_dir / name) != text]
        for name in stale:
            print(f"{out_dir / name} is stale; re-run the generator", file=sys.stderr)
        return 1 if stale else 0
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in texts.items():
        (out_dir / name).write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
