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

```python
import asyncio
from edgeproc import EdgeProc, Task, TaskKind, PrivacyMode

async def main() -> None:
    ep = EdgeProc.local_default()                 # registers whichever runtimes are installed
    task = Task(
        kind=TaskKind.EMBED,
        payload={"texts": ["red running shoes", "waterproof hiking boots"]},
        privacy_mode=PrivacyMode.LOCAL_ONLY,
    )
    result = await ep.run(task)
    print(result.success, result.runtime_used, result.latency_ms)

asyncio.run(main())
```

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
