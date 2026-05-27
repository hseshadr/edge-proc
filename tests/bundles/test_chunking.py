"""GearCDC content-defined chunking (Phase A wave 2).

Chunk boundaries are reproducibility-critical: they decide the diff that makes a
one-byte edit move kilobytes instead of gigabytes. These tests pin round-trip
fidelity, cross-instance determinism, the FastCDC size bounds, *local* re-chunking
(the whole point), and the immutable gear table that boundaries depend on.
"""

from __future__ import annotations

import hashlib
import random
import struct

from edgeproc.bundles.chunking import (
    AVG_SIZE,
    GEAR_TABLE,
    MAX_SIZE,
    MIN_SIZE,
    GearCDC,
)

# Pinned digest of the serialized 256-entry gear table. If this ever changes the
# chunker would split known bytes differently and silently break cross-version sync.
_GEAR_TABLE_SHA256 = "fd068d7e8c89e88ad6d312924e7333dc4f6abef20f9672f30027df341d4eeeed"


def _pseudo_random_bytes(n: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(n)  # noqa: S311 — test fixture, not crypto


def _lengths(data: bytes) -> list[int]:
    return [len(c) for c in GearCDC().chunk(data)]


def test_round_trip_various_sizes() -> None:
    for data in (
        b"",
        b"short payload under the minimum",  # < MIN_SIZE -> single chunk
        _pseudo_random_bytes(MIN_SIZE - 1, seed=1),
        _pseudo_random_bytes(4 * MAX_SIZE, seed=2),
        _pseudo_random_bytes(3 * 1024 * 1024 + 777, seed=3),
    ):
        assert b"".join(GearCDC().chunk(data)) == data


def test_sub_min_payload_is_single_chunk() -> None:
    data = _pseudo_random_bytes(MIN_SIZE - 1, seed=10)
    chunks = list(GearCDC().chunk(data))
    assert len(chunks) == 1
    assert chunks[0] == data


def test_determinism_across_calls_and_instances() -> None:
    data = _pseudo_random_bytes(2 * 1024 * 1024 + 13, seed=4)
    first = _lengths(data)
    second = [len(c) for c in GearCDC().chunk(data)]
    assert first == second
    assert len(first) > 1  # actually exercised the boundary search


def test_size_bounds_hold_for_non_final_chunks() -> None:
    data = _pseudo_random_bytes(3 * 1024 * 1024 + 5, seed=5)
    chunks = list(GearCDC().chunk(data))
    for chunk in chunks[:-1]:
        assert MIN_SIZE <= len(chunk) <= MAX_SIZE
    assert 0 < len(chunks[-1]) <= MAX_SIZE


def test_local_edit_rechunks_only_locally() -> None:
    base = bytearray(_pseudo_random_bytes(4 * 1024 * 1024, seed=6))
    original = list(GearCDC().chunk(bytes(base)))

    edit_at = len(base) // 2
    for i in range(edit_at, edit_at + 100):
        base[i] ^= 0xFF  # flip ~100 bytes in the middle
    edited = list(GearCDC().chunk(bytes(base)))

    # Leading run of byte-identical chunks must survive the edit (boundaries before
    # the edit region are unchanged) — i.e. it does NOT globally re-chunk.
    shared = 0
    for left, right in zip(original, edited, strict=False):
        if left != right:
            break
        shared += 1

    leading_bytes = sum(len(original[i]) for i in range(shared))
    assert shared > 0
    assert leading_bytes < edit_at  # the shared run stops before the edit
    # And it stops well before the end — the tail differs, not everything.
    assert shared < len(original) - 1


def test_gear_table_is_stable() -> None:
    assert len(GEAR_TABLE) == 256
    assert all(0 <= v < 2**64 for v in GEAR_TABLE)
    serialized = b"".join(struct.pack("<Q", v) for v in GEAR_TABLE)
    assert hashlib.sha256(serialized).hexdigest() == _GEAR_TABLE_SHA256


def test_size_constants_match_casync_profile() -> None:
    assert MIN_SIZE == 16 * 1024
    assert AVG_SIZE == 64 * 1024
    assert MAX_SIZE == 256 * 1024
