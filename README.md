# EdgeProc

**Run AI search and ranking right on your own device — no cloud, no per-search bill, your data never leaves the machine.**

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

## What you'd use it for

- **An offline-first assistant** that can still search your local notes and files when
  you've got no signal — no round-trip to a server required.
- **A privacy-first app** where the user's data is searched and ranked locally and simply
  never leaves their device.
- **An on-device recommender** that suggests the next item with zero per-search cloud bill,
  no matter how many users you have.
- **An edge or IoT device** that needs fast local search without standing up a big backend.

## Quickstart

Here's the whole thing working in a few lines. Needs `pip install edge-proc[localvec]`;
first run downloads `all-MiniLM-L6-v2` (~90 MB).

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
BM25 + vector fusion).

### …or from the CLI: `route`

Prefer the shell? Persist an index once, drop your `Task` in a JSON file, and `route` it —
the CLI loads the saved index, runs the task, and prints the result. The exit code mirrors
success (`0` ok, `1` otherwise), so scripts can branch on it.

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

Drop `--pretty` for the full result as JSON. A missing index or an unroutable task fails
closed: non-zero exit and a message on stderr.

---

## Under the hood (for developers)

Everything above is the friendly surface. Here's what actually happens, with the real
terms.

### Install

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
and a **budget** (max latency in ms, max memory in MB). Every run returns a typed
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
(`edgeproc/core/settings.py`), a Pydantic settings object that **rejects unknown fields
(fail closed)**. Env vars use the `EDGEPROC_` prefix (except the HF token, which uses the
ecosystem-standard `HF_TOKEN`):

| Setting | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `model_name` | `EDGEPROC_MODEL_NAME` | `sentence-transformers/all-MiniLM-L6-v2` | Embedding model. |
| `hf_token` | `HF_TOKEN` | `None` | Hugging Face auth token. |
| `default_k` | `EDGEPROC_DEFAULT_K` | `10` | Default top-k results. |
| `http_timeout` | `EDGEPROC_HTTP_TIMEOUT` | `30.0` | Bundle HTTP fetch timeout (s). |
| `task_budget_ms` | `EDGEPROC_TASK_BUDGET_MS` | `5000` | Default per-task latency budget. |
| `task_budget_memory_mb` | `EDGEPROC_TASK_BUDGET_MEMORY_MB` | `256` | Default per-task memory budget. |
| `rrf_k_window` | `EDGEPROC_RRF_K_WINDOW` | `60` | RRF rank-window constant for hybrid fusion. |
| `trust_root_pubkey_path` | `EDGEPROC_TRUST_ROOT_PUBKEY_PATH` | `None` | Pinned sync trust-root pubkey (no key ⇒ `sync` refused). |

For the full picture — system context, bundle lifecycle, the verification chain, and the
module map — see [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md) (with d2 diagrams).

## Status & roadmap

**Shipped (v0):** a deterministic non-AI router, a FAISS-backed local-vector runtime
(`EMBED` / `SEARCH` / `RANK`), and a content-addressed, signed-bundle sync substrate
(pinned ed25519 + content-defined chunking), all behind opt-in extras.

**Roadmap — not built yet** (kept as Protocol seams, not in v0):

- A Wasm-3.0 deterministic kernel for sandboxed, portable runtimes — *roadmap, not built yet.*
- Biscuit capability tokens for fine-grained, attenuable authorization — *roadmap, not built yet.*
- Sigstore keyless bundle signing as an alternative to pinned ed25519 keys — *roadmap, not built yet.*

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — system context, bundle lifecycle, CAS + manifest, module boundaries, seams.
- [docs/QUICKSTART.md](docs/QUICKSTART.md) — clone → gate → CLI walkthrough of the full `keygen → publish → sync → route` loop in five minutes.
- [docs/diagrams/](docs/diagrams/) — d2 sources + rendered SVGs.

## Develop

```bash
uv sync --all-extras   # core + extras + dev tooling
uv run poe gate        # lint + format-check + mypy strict + Radon Grade A + pytest (≥90% cov)
```

`poe gate` mirrors CI exactly — if it passes locally, CI passes.

## License

MIT
