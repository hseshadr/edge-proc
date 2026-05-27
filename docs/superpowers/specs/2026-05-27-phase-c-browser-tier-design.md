# Phase C — Browser tier (C1 → C2 → C3) (design)

**Author:** Harish Seshadri
**Date:** 2026-05-27
**Status:** Architecture of record — Phase C of edge-proc local-first delivery (the v0.5 tier). Built incrementally C1 → C2 → C3; the target is **C3 — full in-browser showcase**. Spike-in-demo first, then extract `@edgeproc/browser`.
**Implements:** the browser tier forward-declared in [Phase A — Local-first sync substrate](./2026-05-27-phase-a-sync-substrate-design.md) ("Phase C — browser / OPFS adapter") and `~/dev/project-ideas/oss/edgeproc.md` §4 ("Per-host adapters") + §"v0.5 deferred — Browser tier".

## TL;DR

> Phase C runs edge-proc's **already-shipped local-first sync engine IN THE BROWSER** — same signed
> bundles, same sync state machine, same trust model — only the storage primitive (**OPFS**) and the
> runtime (**a Web Worker**) differ. A TS engine ports `sync_index`: pull a tiny **ed25519-verified**
> pointer, diff a chunk manifest against an OPFS content-addressed store, fetch **only missing
> zstd chunks**, content-address-verify each, reassemble, atomically promote. Then it does
> **in-browser vector search** (cosine top-k over a synced float32 matrix + RRF rerank) and
> **in-browser query embedding** (transformers.js MiniLM), so the Nimbus demo runs **with no backend**
> — fully offline after first sync. It is **decoupled from the Wasm-3.0 gate**: the sync loop, JS/WASM
> vector search, and transformers.js need no Wasm 3.0; the deterministic Wasm KERNEL in-browser stays
> deferred.

**In plain terms:** the hard local-first plumbing — verify a signed pointer, fetch only the chunks
that changed, never trust the network, land files safely on disk — already exists and is tested in
Python (Phase A). The browser is just a different *disk* (OPFS instead of a filesystem) and a different
*sandbox* (a Web Worker instead of a process). edge-proc's `cas.py` already names the seam:
`CacheStore` is a Protocol, and its docstring says an `OPFSCacheStore` ("Phase C, browser") fills it
"with zero consumer change." So Phase C ports the *engine*, not the *architecture*: the manifest
format, the diff logic, the two signature checks, and the content-address-is-the-integrity-check rule
are all identical. We build it in three green checkpoints — sync first (C1), then search (C2), then
query-embedding + a backend-free demo (C3) — because each one de-risks the next, and each has its own
exit criteria you can prove in a real browser.

**The §4 "adapter, not architecture" framing (load-bearing):** edge-proc's north-star §4 says
browser-vs-native is *an adapter, not an architecture* — "the sync state machine, the manifest format,
the diff logic, and the trust model are identical across tiers; only the storage primitive and the
atomic-swap mechanism differ." Phase C is the literal proof of that claim. The Python `sync_index`
(`edgeproc/bundles/sync.py:124`) and the `CacheStore` Protocol (`edgeproc/bundles/cas.py:40-52`) are
the contract; the TS engine re-implements the *same* state machine against OPFS.

### Why it works — worked example

| Situation | What the browser engine does | Why it wins |
|---|---|---|
| First visit, empty OPFS | Sync the signed `examples/catalog` bundle over plain HTTP → verify pointer sig + manifest hash + every chunk → land `products.jsonl` + `vector/` + `embeddings.f32` in OPFS → promote `active` | A static-hostable tab becomes a fully-local search app — no server, no COOP/COEP |
| Producer ships a patched catalog | Fetch `/latest`, diff the new manifest's chunk set vs OPFS → fetch ONLY changed chunks (CDC keeps boundaries local) | A one-byte catalog edit moves kilobytes, in the browser, exactly as on native |
| A tampered `/latest` pointer is served | WebCrypto `crypto.subtle.verify` against the **pinned 32-byte pubkey** fails → reject, promote nothing | The content/supply chain is the trust boundary; a forged pointer never lands |
| User types a query, offline | transformers.js embeds the query in a Worker → cosine top-k over the synced `embeddings.f32` → RRF rerank → render `SearchResult[]` | Search/recommend/rerank all happen in-tab; the CDN is touched only to *fetch updates* |
| The dataset is ~98% one category (clothing) | The showcase is honest **intra-clothing semantic search**, not cross-domain magic | Scope is stated, not oversold (see honest boundary) |

### The honest boundary

Phase C is the **browser delivery + search + query-embedding tier** built on JS/WASM primitives — it
is **NOT** the deterministic Wasm-3.0 KERNEL in the browser. **Decoupled from the Wasm-3.0 gate**
(stated up front because it is the single most important scoping fact): the sync loop needs no Wasm
3.0; JS/WASM vector search needs no Wasm 3.0; transformers.js query-embedding needs no Wasm 3.0. The
in-browser deterministic Wasm kernel — the property that waits on cross-browser Wasm 3.0 parity (still
fragmented in May 2026, see `edgeproc.md` §"v0.5 deferred") — **stays deferred**. Phase C ships the
useful 80% that does not depend on that gate.

In scope: a TS sync engine in a Web Worker (port of `sync_index`), an OPFS `CacheStore`, WebCrypto
ed25519 + sha256 + `@hpcc-js/wasm-zstd` decompression, in-browser cosine top-k + RRF, transformers.js
query embedding, and the Nimbus demo rewired to run backend-free behind the existing `types.ts`
contract. **Deferred / out of scope** (named in full below): the Wasm-3.0 deterministic kernel
in-browser; model-size first-load UX; WebGPU-availability variance; multi-threaded WASM / SAB
speedups (which is the *only* thing that would need COOP/COEP). No "production-ready" claim: this is
the first browser tier, proven by the Verification section.

## Framing — same bundles, same state machine, different disk + sandbox

The whole tier is one substitution, applied twice:

| Concern | Native tier (Phase A, shipped) | Browser tier (Phase C) | Identical across tiers? |
|---|---|---|---|
| Storage primitive | filesystem CAS (`FilesystemCacheStore`) | **OPFS** (`OPFSCacheStore`, sync access handles) | the `CacheStore` *Protocol* — yes |
| Atomic swap | `os.replace` of the `active` pointer | OPFS write of the `active` pointer file | swap *intent* — yes; mechanism differs |
| Runtime/sandbox | a Python process | a **Web Worker** (off the main thread) | — |
| Sync state machine | `sync_index` (`sync.py:124`) | a TS port of the same numbered flow | **yes** |
| Manifest format | `IndexManifest` / `VersionPointer` (`manifest.py`) | the same JSON, parsed in TS | **yes** |
| Diff logic | chunk set-difference (`_missing_chunks`, `sync.py:97`) | the same set-difference | **yes** |
| Trust model | ed25519 pointer + manifest hash + per-chunk sha256 | the same two checks + per-chunk sha256 | **yes** |
| Transport | `FetchAdapter` (`FilesystemAdapter` / `HttpAdapter`) | `fetch()` against the same origin | the *seam* — yes |

The `CacheStore` Protocol in `cas.py:40-52` names exactly this: `has_chunk`, `put_chunk_compressed`,
`get_chunk`, `put_manifest`, `get_manifest`, `read_active`, `promote`, `gc`. The TS `OPFSCacheStore`
implements that surface; nothing in the sync state machine changes shape. The cas.py module docstring
already pins the seam: *"The `CacheStore` Protocol is the seam an `OPFSCacheStore` (Phase C, browser)
fills with zero consumer change."*

## Incremental decomposition — C1 → C2 → C3 (target C3)

Each tier is a **green checkpoint** with its own exit criteria, proven in a real browser. We build to
C3 (the showcase), but C1 and C2 land and stay green first — each de-risks the seams the next depends
on.

### C1 — sync / delivery in-browser

**Goal:** the Phase A sync engine, running in a browser tab, landing the live signed bundle in OPFS.
**No producer change. No COOP/COEP. Static-hostable** (GitHub Pages works).

A TS engine in a **dedicated Web Worker** ports `sync_index` (`sync.py:124-140`), step for step:

1. `fetch("/latest")` → parse the `VersionPointer` JSON (`{manifest_hash, version, signature}`).
2. **Verify the pointer signature**: ed25519-verify the canonical pointer bytes (the pointer JSON with
   `signature` excluded — mirroring the Python `canonical_bytes(pointer, exclude={"signature"})` at
   `sync.py:82`) against the **pinned raw 32-byte pubkey** via WebCrypto
   `crypto.subtle.verify("Ed25519", key, sig, data)` (fallback `@noble/ed25519` where WebCrypto Ed25519
   is unavailable). **Fail-closed**: a bad signature promotes nothing.
3. `fetch("/manifest/<manifest_hash>")` → **sha256 the bytes** (`crypto.subtle.digest("SHA-256", ...)`)
   → compare to the pointer's `manifest_hash` (mirrors `sync.py:91`). Parse the `IndexManifest`.
4. **Diff**: the manifest's deduped chunk set MINUS the chunks already in the OPFS store = the missing
   set (mirrors `_missing_chunks`, `sync.py:97-101`).
5. For each missing `/chunk/<hash>`: `fetch` the verbatim zstd bytes → **zstd-decompress**
   (`@hpcc-js/wasm-zstd`) → **sha256 content-address verify** the plaintext against the chunk name
   (fail-closed, mirrors `put_chunk_compressed` + `_verify_or_remove`, `cas.py:100-119`) → land in OPFS.
6. **Reassembly check**: concatenate each file's chunks in order → sha256 → compare to
   `entry.file_sha256` (mirrors `_verify_reassembly`, `sync.py:116-121`).
7. **Promote**: write the verified pointer to OPFS `active` (mirrors `store.promote`, `sync.py:133`).

Storage: **OPFS** via `createSyncAccessHandle` (Worker-only — sync access handles are not available on
the main thread) for the chunk store + manifests + the `active` pointer; `navigator.storage.persist()`
to resist eviction, with **graceful degrade** when persistence is denied (re-sync on next load).
Main thread ↔ Worker communication is `postMessage` (typed message envelopes).

**Exit (provable in a real browser, Playwright):**
- Syncing the live `examples/catalog` bundle (`~/dev/oss/edge-reco/examples/catalog/`) over HTTP
  verifies + lands all files in OPFS and promotes `active`.
- A **re-sync after a patched bundle** fetches only the changed chunks (assert fetched-vs-reused
  counts, the same proof as Phase A's patch test).
- A **tampered pointer signature** is rejected fail-closed — nothing is promoted, OPFS unchanged.

### C2 — + in-browser vector search

**Goal:** answer search queries entirely in the browser over the synced index — same top results as
the Python backend.

**PRODUCER CHANGE (gating dependency — this is the one new edge-reco change Phase C requires):**
edge-reco's `publish_bundle` (`src/edgereco/catalog/publish.py`) must emit a **raw, browser-readable
embedding matrix** into the staging dir, because the FAISS index files are not browser-parseable
(faiss-WASM is not a viable in-browser path — see Decisions). Concretely:

- Emit `vector/embeddings.f32` — a **row-major, L2-normalized float32** matrix of shape
  `ntotal × 384`, written as `np.ascontiguousarray(embeddings.astype(np.float32)).tobytes()`. The rows
  are **already L2-normalized** (the encoder uses `normalize_embeddings=True`, see
  `edgeproc/localvec/encoder.py:63,67`), so **inner product == cosine; no renorm in the browser**.
- Emit an **id map** (the row-index → product-id ordering) so the browser can map a top-k row back to a
  product. Reuse the FAISS `state.json`/id-map the index already persists (`vector/`), or write an
  explicit `vector/ids.json`; the chosen file is read alongside `embeddings.f32`.
- Update `BUNDLE_FILES` (`publish.py:31`) so `embeddings.f32` (+ the id map, if separate) is included,
  and add `embedding_count` to `catalog_meta.json` (`CatalogMeta`, `publish.py:35-42`) so the browser
  can size the `Float32Array` without trusting raw bytes.

This change is **additive**: the existing FAISS `vector/` files still ship (native consumers
unaffected); `embeddings.f32` is an extra file edge-proc chunks/signs like any other.

The browser engine then:
- Reads the id map + `embeddings.f32` from OPFS as a `Float32Array` (`ntotal × 384`).
- Does **cosine top-k**: dot the (L2-normalized) query vector against each row; since rows are
  L2-normalized, inner product == cosine — **no renorm**. (A WASM dot-product kernel is an optional
  speedup; a plain typed-array loop is the baseline and needs no Wasm 3.0.)
- Ports **RRF rerank** from `edgeproc.localvec.fusion.reciprocal_rank_fusion` (the shared lego
  edge-reco re-exports at `src/edgereco/search/hybrid.py`) to fuse vector ranks (and any keyword ranks)
  — same fusion the Python backend runs.
- Produces results in the **exact `SearchResult` / `RecommendResponse` shapes** from the demo's
  `types.ts` (`demo/frontend/src/api/types.ts:27-42`): `{ product, score, score_components }`.

**Exit:**
- In-browser search over the synced index returns the **same top results as the Python backend** for a
  set of fixture queries (within rerank tolerance — RRF rank order, not float-exact scores).
- **No COOP/COEP needed** (single-threaded typed-array math).

### C3 — + in-browser query embedding + demo goes backend-free (THE SHOWCASE)

**Goal:** the Nimbus demo runs with **no backend at all** — first load syncs the signed bundle + the
embedding model, then search / recommend / rerank all happen in-tab. Fully offline after first sync.

- **Query embedding**: transformers.js v3 (`Xenova/all-MiniLM-L6-v2`, **WASM-q8 default**, WebGPU
  optional) embeds the QUERY string in a Worker.
- **Wire the demo behind the EXISTING contract**: replace `demo/frontend/src/api/client.ts`'s `fetch`
  calls with calls into the browser engine, keeping the `types.ts` interfaces unchanged
  (`search`/`recommend`/`browse` signatures and return types stay). Events
  (`sendEvent`/`InteractionEvent`) become **client-only / no-op** (no server to POST to; the
  click→re-rank loop runs against the in-tab session state). `browse`/`catalogInfo` read the synced
  `products.jsonl` (+ `catalog_meta.json`) from OPFS.

**THE HARD PART — embedding parity (called out prominently, this is the load-bearing C3 risk):**
transformers.js MiniLM must reproduce sentence-transformers' **mean-pooling + L2-normalization**
*exactly*, or the query vector and the catalog vectors live in subtly different spaces and **relevance
silently degrades** — no error, just worse results. The Python catalog vectors come from
`SentenceTransformer.encode(..., normalize_embeddings=True)` (mean-pool + L2-norm,
`edgeproc/localvec/encoder.py:62-68`); the browser query vector must match that pipeline bit-closely
enough that cosine stays meaningful.

> **REQUIRED — embedding-parity test (the load-bearing C3 test):** embed the same fixed set of strings
> with the Python `ProductEncoder` (committed as fixture vectors) and with transformers.js in the
> browser, and **assert cosine ≥ 0.99** per string. This is the gate that proves the two pipelines
> agree; if it fails, C3 relevance is unreliable and the showcase is not shippable.

**Also flag (UX / variance, not blockers):**
- **Model first-load**: `Xenova/all-MiniLM-L6-v2` is **~23–90 MB** depending on quant (q8 vs fp32),
  downloaded once then cached (IndexedDB / Cache API). First load is the cost; subsequent loads are
  instant. CDC helps *bundle updates*, not the *first* model download (the same first-load caveat as
  the native tier).
- **WebGPU availability variance**: WebGPU is optional and not uniformly available; the **WASM-q8 path
  is the default** so the showcase works everywhere, with WebGPU as an opt-in speedup.

**Exit:**
- The demo runs with **NO backend** — first load syncs the signed bundle + model, then
  search/recommend/rerank all happen in-tab; fully offline after first sync.
- The **embedding-parity test passes** (cosine ≥ 0.99).
- **Playwright drives a real search → click → recommend loop** entirely client-side.

## TS package placement — lean / rule-of-three

**Spike inside the demo first, extract later.** Build the engine at `demo/frontend/src/engine/` to
de-risk the seams (OPFS sync access handles, the WebCrypto Ed25519 fallback, the zstd-WASM init, the
parity gap) against the real demo. **Once C1 and C2 prove the shape**, extract a standalone
`@edgeproc/browser` TS package **in the edge-proc repo** (the TS sibling of the Python `edgeproc`
package). **Do not create the standalone package prematurely** — a Protocol/package boundary is
justified only by a proven shape and a second consumer (rule of three); crystallizing
`@edgeproc/browser` before C1/C2 land would abstract an unproven engine.

## Primitives / libraries

One browser mechanism per native primitive; all chosen for the **no-COOP/COEP, static-hostable** path.

| Primitive | In-browser mechanism | Library (license) |
|---|---|---|
| Ed25519 verify | `crypto.subtle.verify("Ed25519", key, sig, data)` against a **pinned raw 32-byte pubkey** | WebCrypto (built-in); fallback **`@noble/ed25519`** (MIT) where WebCrypto Ed25519 is unavailable |
| sha256 (content-address) | `crypto.subtle.digest("SHA-256", bytes)` | WebCrypto (built-in) |
| zstd decompress | one-shot decompress of verbatim chunk bytes | **`@hpcc-js/wasm-zstd`** (Apache-2.0) |
| Content-addressed store | OPFS chunk store + manifests + `active` pointer | `navigator.storage.getDirectory()` + **`createSyncAccessHandle`** (Worker-only) + `navigator.storage.persist()` |
| Vector search | cosine top-k over a `Float32Array` (inner product == cosine; rows L2-normalized) | typed arrays (built-in); optional WASM dot-product later |
| RRF rerank | port of `reciprocal_rank_fusion` | (own TS, ported from `edgeproc.localvec.fusion`) |
| Query embedding | MiniLM mean-pool + L2-norm in a Worker | **transformers.js v3** (`Xenova/all-MiniLM-L6-v2`), Apache-2.0 |

**COOP/COEP / SharedArrayBuffer is required ONLY for multi-threaded WASM, SAB, or full WebGPU
multi-threading** — and **none of those are on Phase C's chosen path**. C1, C2, and single-threaded C3
need **no cross-origin isolation headers**, so the demo **deploys on plain static hosting (including
GitHub Pages)**. This is the deliberate reason single-threaded WASM-q8 is the default.

## Testing strategy

The browser tier's quality gate is the demo frontend's existing **`frontend-quality`** bar — Biome,
`tsc --strict`, Vitest, Playwright; **no `any`, no default exports, interface-over-type, hook hygiene,
a11y baseline**. New TS code clears that bar.

- **Engine unit tests (Vitest)** — the sync loop, ed25519/sha256 verify **fail-closed**, zstd
  decompress, cosine top-k, RRF. Run logic in-Worker or against an **OPFS test shim**; for pure-logic
  tests, fall back to a **thin in-memory `CacheStore`** (the same Protocol surface, backed by a `Map`)
  so the state machine is tested without OPFS.
- **Embedding-parity test (the load-bearing C3 test)** — a **cross-language fixture**: Python
  `ProductEncoder` vectors (committed) vs transformers.js embeddings, **assert cosine ≥ 0.99** per
  string. This is the gate that proves the query and catalog vector spaces agree.
- **Playwright e2e** against a served origin (the edge-reco **Caddy stack** at
  `deploy/docker-compose.yml`, or a plain static server of `examples/catalog`):
  - **C1**: sync-lands-in-OPFS + a patch re-sync fetches only changed chunks + a tampered-pointer
    reject (nothing promoted).
  - **C3**: the full **backend-free search → click → recommend** loop, in-tab.
- **Producer change (C2) — Python tests in edge-reco**, under its existing gate: `embeddings.f32` is
  emitted; it is present in the published bundle; it has the correct shape (`embedding_count × 384`,
  row-major float32, L2-normalized rows). Additive — the existing FAISS-bundle tests stay green.
- **Both Python suites stay green**: the edge-proc Python suite (the producer change is in edge-reco,
  not edge-proc) and the edge-reco Python suite (the producer change is additive — FAISS files still
  ship).

## Honest boundary / deferred (named)

- **Wasm-3.0 deterministic KERNEL in-browser — DEFERRED.** Phase C uses JS/WASM (typed-array math,
  `@hpcc-js/wasm-zstd`, transformers.js), **not** the deterministic Wasm kernel. The in-browser
  deterministic kernel waits on cross-browser Wasm 3.0 parity (fragmented in May 2026) and is
  explicitly out of Phase C. Phase C is **decoupled from that gate** — it ships the useful tier that
  does not need it.
- **Model-size first-load UX** — the ~23–90 MB MiniLM download on first visit (cached after). A product
  concern (quant choice, a tiny cold-start model streaming behind the full one), not sync-engine work.
- **WebGPU availability variance** — WebGPU is optional and uneven; WASM-q8 is the default so the
  showcase works everywhere.
- **Dataset is ~98% single-category (clothing)** — so the C3 showcase is honest **intra-clothing
  semantic search**, not cross-domain retrieval. Stated, not oversold.
- **Multi-threaded WASM / SAB speedups (COOP/COEP)** — out of scope; the single-threaded path is
  deliberately chosen so the demo static-hosts with no isolation headers.

## Decisions pinned

- **Incremental C1 → C2 → C3** — three green checkpoints to the showcase; sync, then search, then
  query-embedding + backend-free demo. Each de-risks the next; each has its own exit criteria.
- **OPFS CAS in a Worker** — the chunk store + manifests + `active` pointer live in OPFS via
  `createSyncAccessHandle` (Worker-only); `navigator.storage.persist()` + graceful degrade on eviction.
- **WebCrypto Ed25519 + `@hpcc-js/wasm-zstd` + `crypto.subtle` sha256** — pointer signature, chunk
  decompression, and content-address checks; `@noble/ed25519` is the fallback where WebCrypto Ed25519
  is unavailable. The pinned trust root is the **raw 32-byte pubkey** (mirrors `public_bytes_raw()`).
- **Producer adds raw `embeddings.f32`** — edge-reco emits a row-major L2-normalized float32
  `ntotal × 384` matrix (+ id map, + `embedding_count` in `catalog_meta.json`) so the browser can do
  cosine top-k directly. **faiss-WASM is not a viable in-browser path**, so the index is delivered as a
  raw matrix the browser can read. Additive — FAISS `vector/` files still ship.
- **transformers.js query-embed with a MANDATORY parity test** — `Xenova/all-MiniLM-L6-v2` (WASM-q8
  default / WebGPU optional); the embedding-parity test (cosine ≥ 0.99 vs the Python encoder) is the
  load-bearing C3 gate, because mean-pool + L2-norm must match or relevance silently degrades.
- **Spike-in-demo then extract `@edgeproc/browser`** — build the engine in `demo/frontend/src/engine/`
  to prove the shape; extract the standalone TS package (in the edge-proc repo) only after C1/C2 land
  (rule of three). No premature package.
- **Decoupled from the Wasm-3.0 gate** — the sync loop, JS/WASM vector search, and transformers.js need
  no Wasm 3.0; the in-browser deterministic Wasm kernel stays deferred.
- **No COOP/COEP needed for the chosen path** — single-threaded WASM-q8 + typed-array math; the demo
  static-hosts (incl. GitHub Pages). COOP/COEP is only for multi-threaded WASM / SAB / full WebGPU,
  which are out of scope.

## Verification

How to prove each tier — every claim has a runnable proof.

1. **C1 — sync lands in OPFS (Playwright, real browser).** Serve `examples/catalog` (the edge-reco
   Caddy stack or a static server); load the demo; assert the Worker engine syncs, every chunk
   content-address-verifies, and `products.jsonl` + `vector/` + `embeddings.f32` land in OPFS with
   `active` promoted.
2. **C1 — patch fetches only changed chunks.** Publish a patched bundle; re-sync; assert the
   fetched-vs-reused chunk counts show only the changed file's chunks were fetched (the Phase A patch
   proof, in-browser).
3. **C1 — tamper is rejected fail-closed.** Serve a `/latest` whose signature does not verify against
   the pinned pubkey; assert the engine rejects it and promotes nothing (OPFS unchanged).
4. **C2 — search parity with the Python backend.** For a set of fixture queries, assert the in-browser
   top results match the Python backend's top results within rerank tolerance (RRF rank order).
5. **C3 — embedding-parity test passes.** Run the cross-language fixture (Python `ProductEncoder`
   vectors vs transformers.js); assert cosine ≥ 0.99 per string.
6. **C3 — backend-free demo run.** Start the demo with no backend process; first load syncs the signed
   bundle + model; then drive a **search → click → recommend** loop entirely client-side (Playwright);
   confirm it works **offline after the first sync** (no further network calls answer queries).
7. **Gates green.** The demo frontend's `frontend-quality` gate (Biome, `tsc --strict`, Vitest,
   Playwright) passes; the edge-proc Python suite and the edge-reco Python suite (incl. the new
   `embeddings.f32` producer tests) stay green.

<!--
Spec voice: matches ~/dev/project-ideas/oss/edgeproc.md and the Phase A spec — 4-part TL;DR
(quote + plain-terms + worked example + honest boundary), Karpathy clarity (why before how,
plain-language glosses), a Decisions-pinned mini-log, and a runnable Verification section. No
"production-ready" claim.
-->
