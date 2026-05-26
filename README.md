# EdgeProc

**AI-native local execution substrate.** The foundational local-execution Lego: hand it a *task*
("embed this", "search that") with a budget and a privacy mode; a plain, replayable, non-AI router
picks which runtime serves it. EdgeProc is a library an app embeds, not a cloud you sign up for.

## TL;DR

- **What it is.** A small shared layer that does the boring local-AI plumbing once, correctly: pick a
  runtime, run an embed/search/rank task, return a typed `ResultEnvelope`, and download-and-verify an
  index bundle. Built on [`shared-libs-python`](https://github.com/hseshadr/shared-libs-python) — the
  FAISS index here is a concrete implementation of its `VectorIndex` Protocol, so it drops straight
  into that library's `IndexManager`.
- **Why it works.** The router is a *pure deterministic function*, never an LLM. The same `Task`
  against the same registered runtimes always picks the same runtime — so a trace is replayable and a
  regulator (or you) can prove which runtime touched a request. Heavy deps are opt-in extras; the core
  is tiny.
- **Why it exists.** Every local-AI app re-solves "pick a runtime / run the task / verify the index"
  differently and usually badly. EdgeProc does it once so downstream projects (edge-reco, aml-filter,
  spookie, …) just `import edgeproc`.
- **Status.** v0, extract-first: the substrate is lifted from the tested `edge-reco` codebase. The
  Wasm-3.0 deterministic kernel, Biscuit capability tokens, and Sigstore-signed bundles are the
  **roadmap**, kept as Protocol seams — not built in v0. See `edgeproc.md` spec for the north star.

## Install

```bash
pip install edge-proc                    # core + CLI (pure router, contracts)
pip install edge-proc[localvec]          # + FAISS vector runtime (EMBED / SEARCH / RANK)
pip install edge-proc[bundles]           # + manifest + checksum sync substrate
pip install edge-proc[localvec,bundles]  # full local substrate
```

## Quickstart

In v0 you **register runtimes explicitly** — `EdgeProc.local_default()` gives you a
deterministic router over an *empty* registry (auto-probing installed kernels is
roadmap). So the real workflow is: build a `LocalVecRuntime`, register it, then
route a task. This semantic-search demo runs end to end:

> Needs `pip install edge-proc[localvec]`. First run downloads the
> `all-MiniLM-L6-v2` model (~90 MB); it is cached after that.

```python
import asyncio

from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding

from edgeproc import EdgeProc, PrivacyMode, Task, TaskKind
from edgeproc.core.registry import RuntimeRegistry
from edgeproc.localvec.encoder import TextEncoder
from edgeproc.localvec.faiss_index import FaissVectorIndex
from edgeproc.localvec.runtime import LocalVecRuntime
from edgeproc.localvec.searcher import KeywordSearcher

CATALOG = {
    "p1": "red running shoes",
    "p2": "waterproof hiking boots",
    "p3": "blue denim jacket",
    "p4": "trail running sneakers",
}


async def main() -> None:
    ids, texts = list(CATALOG), list(CATALOG.values())

    # 1. Build a local vector runtime: an encoder, a FAISS index, and a BM25 searcher.
    encoder = TextEncoder()
    index = FaissVectorIndex("catalog", IndexConfig(dimension=encoder.dim))
    await index.insert(
        [
            VectorEmbedding(entity_id=i, embedding=v.tolist())
            for i, v in zip(ids, encoder.encode_texts(texts), strict=True)
        ]
    )
    runtime = LocalVecRuntime(encoder, index, KeywordSearcher.from_texts(texts, ids))

    # 2. Register it, then hand EdgeProc a Task. The pure router picks the runtime.
    registry = RuntimeRegistry()
    registry.register(runtime)
    ep = EdgeProc(registry=registry)

    result = await ep.run(
        Task(
            kind=TaskKind.SEARCH,
            payload={"query": "shoes for running", "k": 3},
            privacy_mode=PrivacyMode.LOCAL_ONLY,
        )
    )
    print(result.success, result.runtime_used, f"{result.latency_ms:.1f}ms")
    for entity_id, distance in result.payload["results"]:
        print(f"  {entity_id}  {CATALOG[entity_id]:<24} distance={distance:.3f}")


asyncio.run(main())
```

```text
True localvec 31.2ms
  p1  red running shoes        distance=0.219
  p4  trail running sneakers   distance=0.246
  p2  waterproof hiking boots  distance=0.556
```

Swap `TaskKind.SEARCH` for `TaskKind.EMBED` with `payload={"texts": [...]}` to get
raw vectors back, or `TaskKind.RANK` for hybrid BM25 + vector fusion.

## Architecture (v0)

```
your app ── Task ──▶ EdgeProc.run()
                         │  pure deterministic Router picks the first runtime that ACCEPTs
                         ▼
   ┌─────────────────────────────────────────────┐
   │ edgeproc.core   contracts + router + registry │  (default)
   │ edgeproc.localvec  LocalVecRuntime + FAISS     │  [localvec]
   │ edgeproc.bundles   manifest + checksum + sync  │  [bundles]
   │ edgeproc.cli       Typer CLI                    │  (default)
   └─────────────────────────────────────────────┘
        seams (deferred): Wasmtime kernel · Biscuit caps · Sigstore bundles
```

## Develop

```bash
uv sync --all-extras   # core + extras + dev tooling
uv run poe gate        # lint + format-check + mypy strict + Radon Grade A + pytest (≥90% cov)
```

`poe gate` mirrors CI exactly — if it passes locally, CI passes.

## License

MIT
