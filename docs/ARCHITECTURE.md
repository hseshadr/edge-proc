# Architecture

EdgeProc is a library you `import`. It does three things, in order:

1. **Pick** a runtime for a task (deterministic router — never an LLM).
2. **Run** the task on that runtime (local vector index, by default).
3. **Sync** the index that powers it from a signed CDN-friendly origin, fail-closed.

The three live behind three small surfaces — `edgeproc.core`, `edgeproc.localvec`, `edgeproc.bundles` — wired together by `edgeproc.cli`. None of them depend on each other in a way that forces an opinion on the others: you can use `route` without `sync`, use `sync` to deliver any directory of files (not just an index), or register a different runtime entirely.

## System context

![system context](diagrams/system-context.svg)

Three parties:

- **Publisher** (build-side) signs a `/latest` pointer once per release and writes content-addressed chunks under `origin/`.
- **Origin** is any static HTTP server or CDN. It has no app logic — it serves files by hash.
- **Consumer** runs `edgeproc sync` to pull the pointer, verify it against a pinned public key, fetch only the missing chunks, and atomically promote the new version. Then the consumer's app calls `EdgeProc.run(Task(...))` and a deterministic router picks the runtime that owns that task.

The trust boundary is the pinned public key. Everything an attacker could swap (chunks, manifests, even the pointer) is recomputed and verified locally; the key is the only thing the consumer has to obtain out-of-band.

## Bundle lifecycle

![bundle lifecycle](diagrams/bundle-lifecycle.svg)

The four CLI verbs map one-to-one onto the four stages. `keygen` is one-time. `publish` runs on the build host whenever you cut a release. `sync` and `route` run on the device.

The invariants in the diagram are the security model in one screen. The only signed object is the version pointer; everything else is verified by content hash. A tampered chunk fails its hash, a swapped manifest fails its hash, a forged pointer fails its signature — all three exit non-zero with no traceback.

## Content-addressed store and manifest

![cas and manifest](diagrams/cas-and-manifest.svg)

Chunk-level deduplication is the reason `v1.0.0 → v1.0.1` is a delta, not a full re-download. Identical bytes across versions resolve to identical chunk hashes, so the manifest for v1.0.1 simply references the same chunks as v1.0.0 wherever the file content didn't change. A one-line edit to a 400 KB index re-fetches one chunk.

## Module boundaries

| Module | Lives under | Extras flag | Responsibility |
|---|---|---|---|
| `edgeproc.core` | `edgeproc/core/` | (default) | `Task`, `ResultEnvelope`, `RuntimeRegistry`, deterministic `Router`, `EdgeProcSettings` |
| `edgeproc.localvec` | `edgeproc/localvec/` | `[localvec]` | `TextEncoder`, `FaissVectorIndex`, `KeywordSearcher` (BM25), reciprocal-rank fusion, `LocalVecRuntime` |
| `edgeproc.bundles` | `edgeproc/bundles/` | `[bundles]` | content-defined chunking (GearCDC), zstd compression, ed25519 signing, manifest types, `sync_index`, `FetchAdapter` (HTTP + filesystem) |
| `edgeproc.cli` | `edgeproc/cli/` | (default) | Typer entrypoints: `keygen`, `publish`, `sync`, `route` |

Heavy dependencies are opt-in. Installing the core gives you `Task`, the router, and the CLI shell. `[localvec]` brings FAISS + sentence-transformers. `[bundles]` brings cryptography + zstandard.

## Where the seams are

Three protocol seams are kept in v0 so future runtimes drop in without breaking consumers:

- **`Runtime`** — anything that can `ACCEPT` a `Task` and produce a `ResultEnvelope`. The router picks the first registered runtime that accepts.
- **`Encoder`** — anything that turns `list[str]` into normalized float32 embeddings. `TextEncoder` is sentence-transformers; the seam lets a consumer plug in `onnx`, a remote service, or a fixture.
- **`FetchAdapter` / `CacheStore`** — `sync_index` doesn't know whether it's pulling over HTTP or off the filesystem. Both adapters ship; CDN-fronted edges, OPFS-backed browsers, and local-disk caches all reuse the same engine.

Roadmap seams not built in v0: a Wasmtime deterministic kernel, Biscuit capability tokens, and Sigstore-keyless bundles. The shipped path is pinned ed25519 over a content-addressed CAS, which is the production-real subset.

## Reading order

- New here? Start with [QUICKSTART.md](QUICKSTART.md), then come back.
- Want the security argument in detail? Re-read the cas-and-manifest diagram, then `edgeproc/bundles/sync.py` and `edgeproc/bundles/signing.py`.
- Adding a runtime? Read `edgeproc/core/router.py`, then `edgeproc/localvec/runtime.py` as the reference implementation.
