"""Content-defined chunking (FastCDC normalized) — ``GearCDC``.

A localized edit re-chunks only locally: boundaries are chosen by a rolling hash
of the content, so inserting/flipping bytes shifts only the chunks near the edit,
not every chunk after it. That property is what turns a one-byte change into a
kilobyte patch instead of a full re-download.

Algorithm: FastCDC normalized chunking (Xia et al., "FastCDC: a Fast and
Efficient Content-Defined Chunking Approach for Data Deduplication", USENIX ATC
2016). A 64-bit Gear rolling hash is masked against two thresholds — a stricter
mask below the average size and a looser mask above it — so chunk sizes
concentrate near ``AVG_SIZE``; a cut is forced at ``MAX_SIZE`` and suppressed
before ``MIN_SIZE``.

The gear table and the min/avg/max sizes are reproducibility-critical constants,
NOT configuration: two machines must split the same bytes identically or the diff
silently breaks. The table is generated once from a pinned seed (Python's
``random`` is stable across versions for a fixed seed) and frozen.

In-memory ``bytes`` input only for v0; streaming chunking is deferred (Phase A
operates on whole reassembled files, which fit in memory at this tier).
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from typing import Final

# PINNED SEED — MUST NEVER CHANGE. Chunk boundaries depend on the gear table this
# seed generates; altering it would re-chunk every existing construct and break
# cross-version sync. (Value = ASCII "edgeproc" as a 64-bit int.)
_GEAR_SEED: Final[int] = 0x6564676570726F63

# FROZEN on-wire contract: MIN/AVG/MAX define where boundaries land. Changing any of
# them re-chunks every existing bundle and breaks dedup/sync against all published
# constructs — never change without a migration (same rule as _GEAR_SEED).
MIN_SIZE: Final[int] = 16 * 1024  # 16 KiB — no cut before this (casync profile)
AVG_SIZE: Final[int] = 64 * 1024  # 64 KiB — target chunk size
MAX_SIZE: Final[int] = 256 * 1024  # 256 KiB — forced cut here

# Normalized-chunking masks: more 1-bits = harder to satisfy = bigger chunks.
# 13 bits below average (strict), 11 bits above (loose); centred on log2(AVG)=16.
# FROZEN reproducibility contract: the masks pick the same boundaries on every machine;
# altering them breaks dedup against all existing bundles — never change without a migration.
_MASK_S: Final[int] = 0x0003_5907_0353_0000  # 13 set bits
_MASK_L: Final[int] = 0x0000_D900_4353_0000  # 11 set bits
_MASK_64: Final[int] = (1 << 64) - 1


def _build_gear_table() -> tuple[int, ...]:
    rng = random.Random(_GEAR_SEED)  # noqa: S311 — deterministic table, not crypto
    return tuple(rng.getrandbits(64) for _ in range(256))


GEAR_TABLE: Final[tuple[int, ...]] = _build_gear_table()


def _cut_point(data: bytes, start: int, min_s: int, avg_s: int, max_s: int) -> int:
    """First content-defined boundary in ``data[start:]`` (offset, exclusive end)."""
    n = len(data)
    hard_end = min(start + max_s, n)
    norm_end = min(start + avg_s, hard_end)
    fp = 0
    i = start + min_s  # never cut before MIN_SIZE
    while i < norm_end:  # strict mask region: keep chunks from getting too small
        fp = ((fp << 1) + GEAR_TABLE[data[i]]) & _MASK_64
        if not fp & _MASK_S:
            return i + 1
        i += 1
    while i < hard_end:  # loose mask region: let chunks settle near AVG
        fp = ((fp << 1) + GEAR_TABLE[data[i]]) & _MASK_64
        if not fp & _MASK_L:
            return i + 1
        i += 1
    return hard_end  # forced cut at MAX_SIZE (or end of data)


class GearCDC:
    """FastCDC normalized chunker over in-memory ``bytes`` (streaming deferred)."""

    def __init__(
        self,
        min_size: int = MIN_SIZE,
        avg_size: int = AVG_SIZE,
        max_size: int = MAX_SIZE,
    ) -> None:
        self._min = min_size
        self._avg = avg_size
        self._max = max_size

    def chunk(self, data: bytes) -> Iterator[bytes]:
        """Yield content-defined chunk byte-slices in order; rejoins to ``data``."""
        start = 0
        n = len(data)
        while start < n:
            end = _cut_point(data, start, self._min, self._avg, self._max)
            yield data[start:end]
            start = end
