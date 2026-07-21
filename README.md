# EdgeProc

**Ship a big file to a lot of devices — and let every one of them prove it's the real file, unmodified, before using it. Then send only the parts that changed.**

[![CI](https://github.com/hseshadr/edge-proc/actions/workflows/ci.yml/badge.svg)](https://github.com/hseshadr/edge-proc/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue.svg)](https://www.python.org/downloads/)

## The problem, as a story

When a video game ships an update, your console doesn't re-download the whole 80 GB game. It
downloads a small patch. And before it installs anything, it checks a signature to confirm the
patch really came from the studio — not from someone who slipped a modified file onto a mirror.

Plenty of software that isn't a game needs exactly that and rarely gets it. A search index. A
machine-learning model. An offline catalog or price list. These files are big, they change
often, and they have to land on phones, browsers, and small boxes you don't own or control.

Which leaves two awkward questions:

1. **Is this actually my file?** It crossed someone else's network and sat on someone else's
   CDN. If it arrived corrupted — or quietly swapped — would you find out? Or would your app
   keep running and just start giving wrong answers?
2. **Do I have to send all of it again?** If one row of a 400 MB index changed, making every
   user re-download 400 MB is a real bandwidth bill and a bad experience.

**EdgeProc is a Python library that answers both.** You publish once; any number of devices
pull the update, verify it genuinely came from you and arrived intact, and fetch only the
bytes that actually changed.

## How it answers them

- **Every file is split into chunks, and each chunk is named by a fingerprint of its own
  contents.** That fingerprint (a SHA-256 hash) changes completely if even one byte changes —
  so a corrupted or tampered chunk no longer matches the name it was requested under, and is
  refused. The technical name for this is a *content-addressed store*, or CAS.
- **Exactly one small file is signed, and it vouches for everything else.** The publisher signs
  a *version pointer*: a few bytes saying "version 1.0.1 is live, and its file list is
  `<hash>`". That file list (the *manifest*) names every chunk by hash. So one signature check
  covers the entire release, and there's only one secret to protect.
- **Chunk boundaries follow the content, not fixed offsets.** Edit one line in the middle of a
  big file and only the chunk holding that line changes — everything after it keeps its old
  fingerprint instead of shifting. That's what makes the next update a small delta.
- **If verification fails, nothing is installed.** Not "installed with a warning." The sync
  exits non-zero and the previous good version stays live.

Once the data has landed, EdgeProc also runs the search and ranking **on the device** — no
embedding API, no vector database, no ranking server in the request path.

## See it work

Everything below was run to produce the output shown. About five minutes, most of it a
one-time model download.

```bash
git clone https://github.com/hseshadr/edge-proc.git
cd edge-proc
uv sync --all-extras   # first run also downloads a ~90 MB embedding model
```

### 1. Make something worth shipping

A small product catalog, turned into a searchable index on disk.

```bash
cat > save_index.py <<'PY'
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
PY

uv run python save_index.py
ls catalog_idx
```

```text
index.faiss
state.json
```

### 2. Mint a signing key

`private.key` stays on your build machine forever. `public.key` is what devices are given, and
it's the only thing they have to trust.

```bash
uv run edgeproc keygen --out keys
```

```text
wrote keys/private.key and keys/public.key
```

### 3. Publish a signed release

```bash
mkdir -p src && cp -r catalog_idx src/

uv run edgeproc publish \
    --src src --origin-dir origin \
    --key keys/private.key \
    --bundle-id catalog --version 1.0.0 --pretty
```

```text
published v1.0.0 manifest=c4c28ab05da5
```

`origin/` is now a plain directory of hash-named files. Put it behind any static web server or
CDN as-is — there is no application server to run.

### 4. Pull it onto a device

```bash
uv run edgeproc sync \
    --base-url origin --cache-dir cache \
    --key keys/public.key \
    --materialize-to materialized --pretty
```

```text
synced v1.0.0 manifest=c4c28ab05da5 chunks_fetched=2 chunks_reused=0 bytes_fetched=5903
```

### 5. Search the file that just arrived

```bash
cat > task.json <<'JSON'
{"kind": "search", "payload": {"query": "shoes for running", "k": 3}, "privacy_mode": "local_only"}
JSON

uv run edgeproc route --index-dir materialized/catalog_idx --task task.json --pretty
```

```text
success=True runtime=localvec latency=112.7ms
  p1  0.219
  p4  0.246
  p2  0.556
```

`p1` is "red running shoes" and `p4` is "trail running sneakers" — matched by meaning, on the
machine, with nothing in the request path but local code. The hashes and distances above
reproduce exactly; only `latency` varies by machine.

### 6. Now watch it refuse to be fooled

This is the part worth trying yourself, because it's the whole point.

**Nothing changed? Then nothing is downloaded.**

```bash
uv run edgeproc sync --base-url origin --cache-dir cache --key keys/public.key --pretty
```

```text
synced v1.0.0 manifest=c4c28ab05da5 chunks_fetched=0 chunks_reused=2 bytes_fetched=0
```

**A small edit ships as a small delta.** Publish `1.0.1` with one line appended, then re-sync:

```bash
echo "tiny edit" >> src/catalog_idx/state.json

uv run edgeproc publish --src src --origin-dir origin --key keys/private.key \
    --bundle-id catalog --version 1.0.1 --pretty
uv run edgeproc sync --base-url origin --cache-dir cache --key keys/public.key \
    --materialize-to materialized --pretty
```

```text
published v1.0.1 manifest=312b66ae9d63
synced v1.0.1 manifest=312b66ae9d63 chunks_fetched=1 chunks_reused=1 bytes_fetched=157
```

157 bytes instead of 5,903 — it re-fetched the one chunk that changed and reused the rest.

**No key means no sync.** There is no "just this once" mode:

```bash
uv run edgeproc sync --base-url origin --cache-dir cache2 --pretty; echo "exit=$?"
```

```text
no trust root: pass --key or set EDGEPROC_TRUST_ROOT_PUBKEY_PATH (refusing to sync)
exit=1
```

**Corrupt a chunk on the server and the device rejects the whole release.** Overwrite any file
under `origin/chunk/` and sync into a fresh cache:

```bash
printf 'corrupted' > "origin/chunk/$(ls origin/chunk | head -1)"
uv run edgeproc sync --base-url origin --cache-dir cache3 --key keys/public.key --pretty
echo "exit=$?"
ls cache3
```

```text
sync failed: stored chunk failed to decompress
exit=1
chunks
manifests
```

Compare that to the healthy `cache/`, which has an `active` directory. `cache3/` never got one:
the bad version was never promoted, and a device in this state keeps serving the last good
version instead of silently serving corrupted data.

### Prefer to stay in Python?

The same search, in-process, without the CLI:

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

```text
  p1  red running shoes        distance=0.219
  p3  trail sneakers           distance=0.374
  p2  waterproof hiking boots  distance=0.556
```

Swap `TaskKind.SEARCH` for `TaskKind.EMBED` (raw vectors) or `TaskKind.RANK` (keyword +
meaning-based ranking combined).

## Why you'd reach for this

Moving both the data and the compute to the device buys four things at once:

- **The Nth query costs nothing.** Per-request cloud bills for embeddings, vector search, and
  reranking collapse into a one-time bundle build.
- **Traffic spikes land on clients, not your servers.** A launch or a front-page link is
  absorbed by users' own devices. Nothing to autoscale, nothing to fall over.
- **It survives weak or absent connectivity.** After one sync the device needs no network at
  all to keep answering queries.
- **Tampering is caught, not tolerated.** Verification is fail-closed, and because the data is
  searched locally, it never leaves the device to begin with.

Typical shapes: an app that recommends the next item with no per-search cloud bill however many
users you have; a privacy-sensitive tool where user data must never leave the machine; an edge
or IoT box that needs fast local search without standing up a backend.

---

## Under the hood (for developers)

Everything above is the friendly surface. Here is what actually happens, in the real terms.

### Install

edge-proc isn't on PyPI yet, so the working install is the clone-and-go setup above — one
command, no sibling checkouts:

```bash
git clone https://github.com/hseshadr/edge-proc.git
cd edge-proc
uv sync --all-extras   # core + extras + dev tooling
```

That Just Works — `shared-libs-python` isn't on PyPI either, so `pyproject.toml` pins it to a
release tag from public GitHub (see `[tool.uv.sources]`); `uv sync` fetches it for you, nothing
else to clone. Co-developing `shared-libs-python` alongside EdgeProc? Clone it next to this repo
and swap the git source for the commented path source in `pyproject.toml`.

Once edge-proc is published to PyPI, the extras install directly:

```bash
pip install edge-proc                    # core + CLI (pure router, contracts)
pip install edge-proc[localvec]          # + FAISS vector runtime (EMBED / SEARCH / RANK)
pip install edge-proc[bundles]           # + manifest + checksum sync substrate
pip install edge-proc[localvec,bundles]  # full local substrate
```

EdgeProc is **purely a dependency** — a library an application embeds, not a service you sign
up for. The core is tiny; the heavy machinery (FAISS, sync) is opt-in behind extras. It builds
on [`shared-libs-python`](https://github.com/hseshadr/shared-libs-python): the FAISS index here
is a concrete implementation of that library's `VectorIndex` Protocol.

### The deterministic router

You hand EdgeProc a `Task` and a router picks which engine (a "runtime") serves it. **That
router is a plain rulebook, never an AI** — it asks each registered runtime "do you accept this
task?" and picks the first that says yes. Because it's a pure function, the same `Task` against
the same runtimes always routes the same way, so a trace is replayable and you can prove which
runtime touched a request.

### The typed result and the Task/budget model

A `Task` carries its `kind` (`EMBED` / `SEARCH` / `RANK`), a `payload`, a `privacy_mode`, and a
latency/memory **budget declaration**. `EdgeProc` admits work through a thread-safe
`MemoryManager`: the sum of declared in-flight reservations cannot exceed
`max_in_flight_memory_mb`, and every reservation releases in a `finally`-safe context. This is
deterministic admission control, **not** a native-RSS limit: the budget remains a declaration,
not an enforcement boundary for allocations inside FAISS, NumPy, or another native runtime. The
host or container owns RSS, CPU, and process-level termination. Share one `MemoryManager` across
facades when they share a process.

Every run returns a typed `ResultEnvelope` — a structured object with `success`, the serving
`runtime`, `latency`, and the `payload` — not a loose dict. Typed in, typed out.

### The verification chain, precisely

`sync` verifies the pointer signature against the pinned trust-root pubkey **before trusting
anything**, diffs the manifest against the local cache, fetches only missing chunks, re-checks
every chunk against its content address, and only then atomically promotes the new version. A
tampered chunk fails its content-address check; a forged pointer fails its signature check —
both exit non-zero with no traceback, and neither promotes into the cache.

Chunking is content-defined (GearCDC) and chunks are zstd-compressed. Add `--http` to `sync`,
serving `origin/` over any static HTTP server or CDN, to go over the wire instead of the
filesystem; the contract is identical and only the transport changes.

### Configuration: `EdgeProcSettings` + `EDGEPROC_`-prefixed env vars

Deploy-time config is read lazily from the environment / `.env` via `EdgeProcSettings`
(`edgeproc/core/settings.py`). It validates documented settings but
ignores unrelated host variables, so an embedded library coexists with the application's own
environment. Env vars use the `EDGEPROC_` prefix (except the HF token, which uses the
ecosystem-standard `HF_TOKEN`):

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

For the full picture — system context, bundle lifecycle, the verification chain, and the module
map — see [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) (with d2 diagrams).

### Measured numbers

```bash
uv sync --all-extras
uv run poe gate
uv run python benchmarks/benchmark.py
```

Both commands check themselves. The benchmark runs a fixed, offline fixture and prints JSON
with the latency it measured, peak memory, the machine it ran on, and pass/fail against each
budget — so you get *your* numbers rather than having to trust someone else's.

The figures measured here, and the hardware they were measured on, are recorded in
[**docs/OPERATIONS.md**](docs/OPERATIONS.md#measured-evidence). That is the one place they
live: this README deliberately does not restate them, because a number copied into two
documents is a number that will eventually disagree with itself.

## Status & roadmap

**Shipped (v0):** a deterministic non-AI router, a FAISS-backed local-vector runtime (`EMBED` /
`SEARCH` / `RANK`), and a content-addressed, signed-bundle sync substrate (pinned ed25519 +
content-defined chunking), all behind opt-in extras.

**Roadmap — not built yet** (kept as Protocol seams, not in v0):

- **First-party WASM kernel v0** — one deterministic hot path (chunk hash/verify, BM25, or
  rerank math), Rust→wasm32, running identically in the browser and in Python via wasmtime,
  filling the `CUSTOM_WASM` seam — *roadmap, not built yet; full definition of done in
  [ROADMAP.md](ROADMAP.md).*
- Biscuit capability tokens for fine-grained, attenuable authorization — *roadmap, not built yet.*
- Sigstore keyless bundle signing as an alternative to pinned ed25519 keys — *roadmap, not built yet.*

## Docs

- [docs/QUICKSTART.md](docs/QUICKSTART.md) — the `keygen → publish → sync → route` loop as a
  standalone five-minute walkthrough.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system context, bundle lifecycle, CAS +
  manifest, module boundaries, seams.
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — threat model, privacy flow, recovery/SLA ownership,
  resource ceilings, and the measured performance gate.
- [docs/diagrams/](docs/diagrams/) — d2 sources + rendered SVGs.

## Develop

```bash
uv sync --all-extras   # core + extras + dev tooling
uv run poe gate        # lint + format-check + mypy strict + Radon Grade A + pytest (≥90% cov)
```

`poe gate` mirrors CI exactly — if it passes locally, CI passes.

## About

**EdgeProc** — also written `edge-proc` and `edgeproc`; canonical repo
[`hseshadr/edge-proc`](https://github.com/hseshadr/edge-proc) — is the open-source, local-first
delivery-and-search substrate described above. It builds on
[**shared-libs-python**](https://github.com/hseshadr/shared-libs-python), the vector-partitioning
protocol its FAISS runtime implements. Canonical entity page:
[edge-reco.com/edgeproc](https://edge-reco.com/edgeproc), on a domain we control. It is **not
affiliated with any other product or company named "EdgeProc"**.

## License

MIT
