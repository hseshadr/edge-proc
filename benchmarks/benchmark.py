"""Repeatable offline latency and memory gate for EdgeProc-owned hot paths."""

from __future__ import annotations

import asyncio
import json
import math
import platform
import random
import resource
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

import numpy as np
from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding

from edgeproc.bundles.adapters import FilesystemAdapter
from edgeproc.bundles.cas import FilesystemCacheStore
from edgeproc.bundles.chunking import GearCDC
from edgeproc.bundles.publish import build_bundle
from edgeproc.bundles.signing import Ed25519Signer, Ed25519Verifier, generate_keypair
from edgeproc.bundles.sync import sync_index
from edgeproc.localvec.faiss_index import FaissVectorIndex

VECTOR_COUNT = 10_000
VECTOR_DIM = 32
SEARCH_RUNS = 30
SYNC_BYTES = 4 * 1024 * 1024
COLD_RUNS = 7
WARM_RUNS = 20
BUDGETS = {"search_p95_ms": 100.0, "cold_p95_ms": 750.0, "warm_p95_ms": 250.0}
RSS_BUDGET_MIB = 512.0


def _milliseconds(start: float) -> float:
    return (perf_counter() - start) * 1000.0


def _summary(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    rank = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "p50_ms": ordered[len(ordered) // 2],
        "p95_ms": ordered[rank],
        "max_ms": ordered[-1],
    }


def _max_rss_mib() -> float:
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


async def _search_samples() -> list[float]:
    rng = np.random.default_rng(20260715)
    rows = rng.standard_normal((VECTOR_COUNT, VECTOR_DIM)).astype(np.float32)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    index = FaissVectorIndex("benchmark", IndexConfig(dimension=VECTOR_DIM))
    await index.insert(_embeddings(rows))
    query = rows[0].tolist()
    for _ in range(3):
        await index.search(query, 10)
    return [await _timed_search(index, query) for _ in range(SEARCH_RUNS)]


def _embeddings(rows: np.ndarray) -> list[VectorEmbedding]:
    return [
        VectorEmbedding(entity_id=f"entity-{idx}", embedding=row.tolist())
        for idx, row in enumerate(rows)
    ]


async def _timed_search(index: FaissVectorIndex, query: list[float]) -> float:
    start = perf_counter()
    await index.search(query, 10)
    return _milliseconds(start)


def _origin(root: Path) -> tuple[Path, Ed25519Verifier]:
    private, public = generate_keypair()
    origin = root / "origin"
    payload = random.Random(20260715).randbytes(SYNC_BYTES)  # noqa: S311 - fixed fixture
    build_bundle(
        files={"catalog.bin": payload},
        store=FilesystemCacheStore(origin),
        chunker=GearCDC(),
        signer=Ed25519Signer(private),
        bundle_id="benchmark",
        version="1.0.0",
        channel="stable",
        sequence=1,
        bind_identity=True,
    )
    return origin, Ed25519Verifier.from_public_bytes(public.public_bytes_raw())


def _sync_once(origin: Path, cache: Path, verifier: Ed25519Verifier) -> float:
    start = perf_counter()
    sync_index(
        base_url=str(origin),
        store=FilesystemCacheStore(cache),
        adapter=FilesystemAdapter(),
        verifier=verifier,
        expected_bundle_id="benchmark",
        expected_channel="stable",
    )
    return _milliseconds(start)


def _cold_samples(root: Path, origin: Path, verifier: Ed25519Verifier) -> list[float]:
    return [_sync_once(origin, root / f"cold-{idx}", verifier) for idx in range(COLD_RUNS)]


def _warm_samples(root: Path, origin: Path, verifier: Ed25519Verifier) -> list[float]:
    cache = root / "warm"
    _sync_once(origin, cache, verifier)
    return [_sync_once(origin, cache, verifier) for _ in range(WARM_RUNS)]


def _checks(
    search: dict[str, float], cold: dict[str, float], warm: dict[str, float]
) -> dict[str, bool]:
    return {
        "search_p95": search["p95_ms"] <= BUDGETS["search_p95_ms"],
        "cold_p95": cold["p95_ms"] <= BUDGETS["cold_p95_ms"],
        "warm_p95": warm["p95_ms"] <= BUDGETS["warm_p95_ms"],
        "max_rss": _max_rss_mib() <= RSS_BUDGET_MIB,
    }


async def _run(root: Path) -> dict[str, object]:
    origin, verifier = _origin(root)
    search = _summary(await _search_samples())
    cold = _summary(_cold_samples(root, origin, verifier))
    warm = _summary(_warm_samples(root, origin, verifier))
    checks = _checks(search, cold, warm)
    return {
        "fixture": {"vectors": VECTOR_COUNT, "dimensions": VECTOR_DIM, "sync_bytes": SYNC_BYTES},
        "results": {"search": search, "cold_sync": cold, "warm_sync": warm},
        "resources": {"max_rss_mib": _max_rss_mib(), "budget_mib": RSS_BUDGET_MIB},
        "budgets_ms": BUDGETS,
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    with TemporaryDirectory(prefix="edgeproc-benchmark-") as directory:
        result = asyncio.run(_run(Path(directory)))
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
