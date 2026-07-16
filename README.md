# EdgeProc

**Run AI search and ranking right on your own device — no cloud, no per-search bill, your data never leaves the machine.**

[![CI](https://github.com/hseshadr/edge-proc/actions/workflows/ci.yml/badge.svg)](https://github.com/hseshadr/edge-proc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

## What is this? (plain version)

Most apps that "search" or "recommend" things quietly ship your data off to a company's
servers, run the search there, and send the answer back. That's slow, it costs money on
every single request, and it means your private notes, messages, and files leave your
device to do it.

EdgeProc is a Python library that does that search and ranking **on the device itself** —
your phone, your laptop, or a small box at the edge of a network. Nothing has to go to the
cloud, so it's fast, free per request, and private by default.

It also handles keeping that on-device search **up to date**: it can safely download new
index files over the internet and check, with cryptography, that nobody tampered with them
along the way. Think of it like an app store that verifies the signature on an update
before installing it — except for your search index.

## Northstar status (verified 2026-07-16)

**EdgeProc now has an explicit memory-admission boundary for concurrent work.**
`MemoryManager` reserves each task's declared memory budget before dispatch, rejects
work that would exceed `max_in_flight_memory_mb`, and releases reservations on every
success and exception path. The change is merged as `f3e3ca9` and the hosted main CI
run passed.

Run the same proof locally:

```bash
uv sync --all-extras
uv run poe gate
uv run python benchmarks/northstar.py
```

The current gate is 255 tests with 98.72% coverage; the benchmark reports sub-0.1 ms
search p95, 47.8 ms cold-sync p95, 15.6 ms warm-sync p95, and 114.2 MiB peak RSS
against a 512 MiB admission budget. That budget is deterministic admission control,
not a promise to cap native FAISS/NumPy allocations; the host or container owns RSS,
CPU, and process-level termination.

## Where it fits

EdgeProc is the reusable **substrate** that makes on-device compute possible: it ships your
search index as a signed, content-addressed bundle any CDN can serve, verifies it fail-closed,
and runs the inference locally — the generic Lego other products build on.
[**edge-reco**](https://github.com/hseshadr/edge-reco) is the reference product built on it — a
full search + recommendation engine running entirely in the browser ([live demo](https://edge-reco.com)).
EdgeProc itself builds on [**shared-libs-python**](https://github.com/hseshadr/shared-libs-python),
the vector-partitioning protocol its FAISS runtime implements.

## What you'd use it for

Moving compute to the edge buys you four things at once. The index + data ship as a static,
signed, content-addressed bundle any CDN serves; inference runs on the device — so there's
no embedding API, vector DB, or ranking server in the request path:

- **Zero-marginal-cost compute.** Every per-request cloud bill (embeddings, vector search,
  reranking) becomes a one-time bundle build. The Nth query costs you nothing.
- **Scales on the clients, not your servers.** A traffic spike — Black Friday, a launch, a
  Hacker News hug — is absorbed by users' own devices. No autoscaling bill, nothing to fall
  over.
- **Resilient on weak, flaky, or dropped connections.** After a one-time sync the consumer
  needs no network. An offline-first assistant still searches your local notes with no
  signal.
- **Trustworthy by construction.** Fail-closed Ed25519 + SHA-256 means an unverifiable or
  tampered pull is *rejected, not silently served* — like an app store checking an update's
  signature before installing it, but for your search index. And because the data is searched
  and ranked locally, it never leaves the device.

Concretely: an on-device recommender that suggests the next item with no per-search cloud
bill no matter how many users you have; a privacy-first app where user data never leaves the
machine; an edge or IoT box that needs fast local search without standing up a backend.
[edge-reco](https://github.com/hseshadr/edge-reco) puts all four together — a browser-native
storefront with search + recommendations and zero backend calls after sync.

## Quickstart

Here's the whole thing working in a few lines. Grab the deps with the clone-and-go
[setup below](#install) (`git clone … && uv sync --all-extras`) — first run downloads
`all-MiniLM-L6-v2` (~90 MB). (Once edge-proc is on PyPI: `pip install edge-proc[localvec]`.)

```python
import asyncio

from edgeproc import EdgeProc, PrivacyMode, RuntimeRegistry, Task, TaskKind
from edgeproc.localvec.encoder import TextEncoder
from edgeproc.localvec.runtime import LocalVecRuntime

CATALOG = {"p1": "red running shoes", "p2": "waterproof hiking boots", "p3": "trail sneakers"}


async def main() -> None:
    runtime = await LocalVecRuntime.from_texts(CATALOG, encoder=TextEncoder())
    registry = RuntimeRegistry(); registry.register(runtime)
    result = await EdgeProc(registry=registry).run(
        Task(kind=TaskKind.SEARCH, payload={"query": "shoes for running"}, privacy_mode=PrivacyMode.LOCAL_ONLY)
    )
    for entity_id, distance in result.payload["results"]:
        print(f"  {entity_id}  {CATALOG[entity_id]:<24} distance={distance:.3f}")


asyncio.run(main())
```

Swap `TaskKind.SEARCH` for `TaskKind.EMBED` (raw vectors) or `TaskKind.RANK` (hybrid
keyword + meaning-based ranking).

### …or from the CLI: `route`

Prefer the shell? Persist an index once, drop your `Task` in a JSON file, and `route` it —
the CLI loads the saved index, runs the task, and prints the result. The exit code mirrors
success (`0` ok, `1` otherwise), so scripts can branch on it.

First persist the catalog (a saved index is the search-index file plus a small `state.json`):

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

Drop `--pretty` for the full result as JSON. A missing index or an unroutable task fails
closed: non-zero exit and a message on stderr.

---

## Under the hood (for developers)

Everything above is the friendly surface. Here's what actually happens, with the real
terms.

### Install

edge-proc isn't on PyPI yet, so the working install is the clone-and-go setup below — one
command, no sibling checkouts:

```bash
git clone https://github.com/hseshadr/edge-proc.git
cd edge-proc
uv sync --all-extras   # core + extras + dev tooling
```

That Just Works — `shared-libs-python` isn't on PyPI either, so `pyproject.toml` pins it to a
release tag from public GitHub (see `[tool.uv.sources]`); `uv sync` fetches it for you,
nothing else to clone. Co-developing `shared-libs-python` alongside EdgeProc? Clone it next
to this repo and swap the git source for the commented path source in `pyproject.toml`.

Once edge-proc is published to PyPI, the extras install directly:

```bash
pip install edge-proc                    # core + CLI (pure router, contracts)
pip install edge-proc[localvec]          # + FAISS vector runtime (EMBED / SEARCH / RANK)
pip install edge-proc[bundles]           # + manifest + checksum sync substrate
pip install edge-proc[localvec,bundles]  # full local substrate
```

EdgeProc is **purely a dependency** — a library an app embeds, not a service you sign up
for. The core is tiny; the heavy machinery (FAISS, sync) is opt-in behind extras. It builds
on [`shared-libs-python`](https://github.com/hseshadr/shared-libs-python): the FAISS index
here is a concrete implementation of that library's `VectorIndex` Protocol.

### The deterministic router

You hand EdgeProc a `Task` and a router picks which engine (a "runtime") serves it. **That
router is a plain rulebook, never an AI** — it asks each registered runtime "do you accept
this task?" and picks the first that says yes. Because it's a pure function, the same `Task`
against the same runtimes always routes the same way, so a trace is replayable and you can
prove which runtime touched a request. (Useful when a regulator, or future you, asks.)

### The typed result and the Task/budget model

A `Task` carries its `kind` (`EMBED` / `SEARCH` / `RANK`), a `payload`, a `privacy_mode`,
and a latency/memory **budget declaration**. `EdgeProc` admits work through a
thread-safe `MemoryManager`: the sum of declared in-flight reservations cannot exceed
`max_in_flight_memory_mb`, and every reservation releases in a `finally`-safe context.
This is deterministic admission control, not a native-RSS limit: the budget remains a
declaration, not an enforcement boundary for allocations inside FAISS, NumPy, or another
native runtime. Share one `MemoryManager` across facades when they share a process. Every run returns a typed
`ResultEnvelope` — a structured object with `success`, the serving `runtime`, `latency`,
and the `payload` — not a loose dict. Typed in, typed out.

### keygen → publish → sync: shipping an index to the edge

So far the index lived on the same machine that routed against it. The `[bundles]` substrate
closes the loop: build a **signed bundle** once on a publisher, then pull it onto any number
of consumers — over the filesystem or a CDN — with cryptographic proof you got exactly what
the publisher signed, re-fetching only the chunks that changed.

Two ideas do the heavy lifting:

- **Content-addressed + signed bundles.** Every file is split into chunks, and each chunk is
  named by its own sha256 hash — so any tampering changes the name and is caught immediately.
  A single small **signed version pointer** (`/latest`) says which version is live; it's the
  only thing signed, and it names the manifest by hash, which names every chunk by hash.
- **Content-defined chunking (GearCDC).** Files are split by their *content*, not at fixed
  offsets, so a one-line edit to a big index only changes one chunk — an update downloads
  only the pieces that actually changed, not the whole file.

```bash
# 1. Mint a trust root. private.key signs on the publisher; public.key is the pin
#    a consumer trusts. Distribute public.key out-of-band; never ship private.key.
edgeproc keygen --out keys

# 2. Stage the files to ship, then publish: chunk + sign them into a content-addressed
#    origin dir. `publish` records paths relative to --src.
mkdir -p src && cp -r catalog_idx src/
edgeproc publish \
    --src src \
    --origin-dir origin \
    --key keys/private.key \
    --bundle-id catalog \
    --version 1.0.0 \
    --pretty

# 3. On the consumer: sync into a fresh cache, trusting ONLY the pinned pubkey.
#    Pass --key, or set EDGEPROC_TRUST_ROOT_PUBKEY_PATH. With neither, sync refuses to
#    run — an unverifiable pull is rejected fail-closed. --materialize-to reassembles
#    every synced file into a plain directory a follow-on `route` can read.
edgeproc sync \
    --base-url origin \
    --cache-dir cache \
    --key keys/public.key \
    --materialize-to materialized \
    --pretty
```

`sync` verifies the pointer signature against the pinned trust-root pubkey **before trusting
anything**, diffs the manifest against the local cache, fetches only missing chunks,
re-checks every chunk against its content address, and atomically promotes the new version.
A tampered chunk fails its content-address check; a forged pointer fails its signature
check — both exit non-zero with no traceback, and never promote into `cache/`. Re-syncing an
unchanged origin fetches nothing; a `1.0.0 → 1.0.1` push is a delta, not a full
re-download. Add `--http` to `sync` (serving `origin/` over any static HTTP/CDN) to go over
the wire instead of the filesystem. This is "edge as a CDN".

The materialized directory holds the exact files you published, so the consumer can `route`
against the freshly delivered index directly:

```bash
edgeproc route --index-dir materialized/catalog_idx --task task.json --pretty
```

### Configuration: `EdgeProcSettings` + `EDGEPROC_`-prefixed env vars

Deploy-time config is read lazily from the environment / `.env` via `EdgeProcSettings`
(`edgeproc/core/settings.py`). It validates documented settings but ignores unrelated host variables,
so an embedded library coexists with the application's own environment. Env vars use the
`EDGEPROC_` prefix (except the HF token, which uses the ecosystem-standard `HF_TOKEN`):

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `model_name` | `EDGEPROC_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model. |
| `hf_token` | `HF_TOKEN` | `None` | Hugging Face auth token. |
| `default_k` | `EDGEPROC_DEFAULT_K` | `10` | Default top-k results. |
| `http_timeout` | `EDGEPROC_HTTP_TIMEOUT` | `30.0` | Bundle HTTP fetch timeout (s). |
| `mutation_lock_timeout` | `EDGEPROC_MUTATION_LOCK_TIMEOUT` | `30.0` | Bounded cross-process publish/sync/promote/GC lock wait (s). |
| `task_budget_ms` | `EDGEPROC_TASK_BUDGET_MS` | `5000` | Default per-task latency budget. |
| `task_budget_memory_mb` | `EDGEPROC_TASK_BUDGET_MEMORY_MB` | `256` | Default per-task memory budget. |
| `max_in_flight_memory_mb` | `EDGEPROC_MAX_IN_FLIGHT_MEMORY_MB` | `512` | Sum of declared task reservations admitted concurrently by one `EdgeProc` instance. |
| `max_materialize_bytes` | `EDGEPROC_MAX_MATERIALIZE_BYTES` | `256 MiB` | Maximum one file materialized into a returned `bytes` value. |
| `rrf_k_window` | `EDGEPROC_RRF_K_WINDOW` | `60` | RRF rank-window constant for hybrid fusion. |
| `trust_root_pubkey_path` | `EDGEPROC_TRUST_ROOT_PUBKEY_PATH` | `None` | Pinned sync trust-root pubkey (no key ⇒ `sync` refused). |

For the full picture — system context, bundle lifecycle, the verification chain, and the
module map — see [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) (with d2 diagrams).

## Status & roadmap

**Shipped (v0):** a deterministic non-AI router, a FAISS-backed local-vector runtime
(`EMBED` / `SEARCH` / `RANK`), and a content-addressed, signed-bundle sync substrate
(pinned ed25519 + content-defined chunking), all behind opt-in extras.

**Roadmap — not built yet** (kept as Protocol seams, not in v0):

- **First-party WASM kernel v0** — one deterministic hot path (chunk hash/verify, BM25, or
  rerank math), Rust→wasm32, running identically in the browser and in Python via wasmtime,
  filling the `CUSTOM_WASM` seam — *roadmap, not built yet; full definition of done in
  [ROADMAP.md](ROADMAP.md).*
- Biscuit capability tokens for fine-grained, attenuable authorization — *roadmap, not built yet.*
- Sigstore keyless bundle signing as an alternative to pinned ed25519 keys — *roadmap, not built yet.*

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system context, bundle lifecycle, CAS + manifest, module boundaries, seams.
- [docs/QUICKSTART.md](docs/QUICKSTART.md) — clone → gate → CLI walkthrough of the full `keygen → publish → sync → route` loop in five minutes.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — threat model, privacy flow, recovery/SLA ownership, resource ceilings, and measured performance gate.
- [docs/diagrams/](docs/diagrams/) — d2 sources + rendered SVGs.

## The stack

EdgeProc is the middle layer of a three-repo system, all MIT-licensed:

- [**edge-reco**](https://github.com/hseshadr/edge-reco) — the reference product built on
  this substrate: a browser-native storefront with hybrid search + session-aware
  recommendations, zero backend calls after sync ([live demo](https://edge-reco.com)).
- **edge-proc** (this repo) — the reusable local-compute substrate: signed bundle sync, a
  CAS cache, fail-closed verification, and hybrid retrieval primitives.
- [**shared-libs-python**](https://github.com/hseshadr/shared-libs-python) — the
  vector-partitioning protocol EdgeProc's FAISS/localvec runtime builds on.

## Develop

```bash
uv sync --all-extras   # core + extras + dev tooling
uv run poe gate        # lint + format-check + mypy strict + Radon Grade A + pytest (≥90% cov)
```

`poe gate` mirrors CI exactly — if it passes locally, CI passes.

## About

**EdgeProc** — also written `edge-proc` and `edgeproc`; canonical repo
[`hseshadr/edge-proc`](https://github.com/hseshadr/edge-proc) — is the open-source,
local-first search/ranking substrate described above. Canonical entity page:
[edge-reco.com/edgeproc](https://edge-reco.com/edgeproc) — EdgeProc on the domain we
control. It is **not affiliated with any other product or company named "EdgeProc"**.

## License

MIT
