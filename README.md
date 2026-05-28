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

### …or from the CLI: `route`

Prefer the shell? Persist an index once, drop your `Task` in a JSON file, and
`route` it — the CLI loads the saved index into a `LocalVecRuntime`, registers it,
runs the task, and prints the `ResultEnvelope`. The exit code mirrors `success`
(`0` ok, `1` otherwise — including `no_runtime_accepted`), so scripts can branch on it.

First persist the catalog (a saved index is the FAISS file plus a small `state.json`):

```python
# save_index.py
import asyncio
from pathlib import Path

from shared_libs_python.vector_mgmt.core.types import IndexConfig, VectorEmbedding

from edgeproc.localvec.encoder import TextEncoder
from edgeproc.localvec.faiss_index import FaissVectorIndex

CATALOG = {
    "p1": "red running shoes",
    "p2": "waterproof hiking boots",
    "p3": "blue denim jacket",
    "p4": "trail running sneakers",
}


async def main() -> None:
    ids, texts = list(CATALOG), list(CATALOG.values())
    encoder = TextEncoder()
    index = FaissVectorIndex("catalog_idx", IndexConfig(dimension=encoder.dim))
    await index.insert(
        [
            VectorEmbedding(entity_id=i, embedding=v.tolist())
            for i, v in zip(ids, encoder.encode_texts(texts), strict=True)
        ]
    )
    index.save(Path("catalog_idx"))


asyncio.run(main())
```

Then route a task against it:

```bash
python save_index.py

cat > task.json <<'JSON'
{"kind": "search", "payload": {"query": "shoes for running", "k": 3}, "privacy_mode": "local_only"}
JSON

edgeproc route --index-dir catalog_idx --task task.json --pretty
```

```text
success=True runtime=localvec latency=82.7ms
  p1  0.219
  p4  0.246
  p2  0.556
```

Drop `--pretty` for the full `ResultEnvelope` as JSON. A missing index or an
unroutable task fails closed: non-zero exit and a message on stderr.

## Ship an index to the edge: `keygen` → `publish` → `sync`

So far the index lived on the same machine that routed against it. The `[bundles]`
substrate closes the loop: **build a signed bundle once on a publisher, then pull it
onto any number of consumers — over the filesystem or a CDN — with cryptographic proof
you got exactly what the publisher signed, and re-fetching only the chunks that changed.**

- **What it is.** Three CLI verbs over a content-addressed store. `keygen` mints an
  ed25519 keypair. `publish` chunks every file under a directory with content-defined
  chunking (GearCDC), writes each unique chunk once under its sha256, and signs a tiny
  `/latest` *version pointer* with your private key. `sync` pulls that pointer, **verifies
  the signature against a pinned trust-root pubkey before trusting anything**, diffs the
  manifest against the local cache, fetches only the missing chunks, re-checks every chunk
  against its content address, and atomically promotes the new version.
- **Why it works.** The pointer is the only signed thing, and it names the manifest by
  hash; the manifest names every chunk by hash. So a tampered chunk or a swapped manifest
  fails its content-address check, and a forged pointer fails its signature check — both
  exit non-zero with no traceback. Identical bytes across versions share chunks, so a
  one-line edit to a big index re-fetches one chunk, not the whole file.
- **Why it matters.** This is "edge as a CDN": the publisher is offline-relative to the
  consumer, the consumer trusts only a key it pinned out-of-band, and a `v1.0.0 → v1.0.1`
  push is a delta, not a full re-download.

> Needs `pip install edge-proc[bundles]`. The walkthrough uses the filesystem; add
> `--http` to `sync` (and serve `origin/` over any static HTTP/CDN) to go over the wire.

```bash
# 1. Mint a trust root. private.key signs on the publisher; public.key is the pin
#    a consumer trusts. Distribute public.key out-of-band; never ship private.key.
edgeproc keygen --out keys
#   wrote keys/private.key and keys/public.key

# 2. Stage the files to ship (here, the saved index dir from the `route` demo above),
#    then publish: chunk + sign them into a content-addressed origin dir. `publish`
#    records paths relative to --src, so keeping catalog_idx/ under src/ preserves it.
mkdir -p src && cp -r catalog_idx src/
edgeproc publish \
    --src src \
    --origin-dir origin \
    --key keys/private.key \
    --bundle-id catalog \
    --version 1.0.0 \
    --pretty
#   published v1.0.0 manifest=9f3a1c4e7b02
```

`origin/` now holds the full CDN contract — `latest` (the signed pointer), `manifest/<hash>`,
and `chunk/<hash>` (one zstd-compressed blob per unique chunk). Point a static server or
CDN at it as-is, or sync straight off the filesystem:

```bash
# 3. On the consumer: sync into a fresh cache, trusting ONLY the pinned pubkey.
#    Pass the key via --key, or set EDGEPROC_TRUST_ROOT_PUBKEY_PATH. With neither,
#    sync refuses to run — an unverifiable pull is rejected fail-closed.
edgeproc sync \
    --base-url origin \
    --cache-dir cache \
    --key keys/public.key \
    --pretty
#   synced v1.0.0 manifest=9f3a1c4e7b02 chunks_fetched=3 chunks_reused=0 bytes_fetched=4096
```

Drop `--pretty` for the full `SyncResult` as JSON (`version`, `manifest_hash`,
`chunks_fetched`, `chunks_reused`, `bytes_fetched`). Re-running `sync` against an
unchanged origin fetches nothing (`chunks_fetched=0`); publishing a `1.0.1` with a small
edit re-fetches only the chunks that actually changed (`chunks_reused` carries the rest).

The synced cache materializes the same files you published, so the consumer can `route`
against the freshly delivered index exactly as before:

```bash
edgeproc route --index-dir cache/catalog_idx --task task.json --pretty
#   success=True runtime=localvec latency=82.7ms
#     p1  0.219
#     p4  0.246
#     p2  0.556
```

Tamper with any chunk or signature in `origin/` and the next `sync` exits `1` with
`sync failed: …` on stderr — it never promotes an unverified version into `cache/`.

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
