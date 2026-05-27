# Phase A — Local-first sync substrate (design)

**Author:** Harish Seshadri
**Date:** 2026-05-27
**Status:** Architecture of record — Phase A of edge-proc local-first delivery. Test-first build, lands behind the `[bundles]` extra.
**Implements:** [Local-first delivery — north-star](../../../README.md) (and `~/dev/project-ideas/oss/edgeproc.md` §"Local-first delivery — north-star"), the native tier of the sync engine.

## TL;DR

> Phase A adds a **local-first, content-addressed, signed, incrementally-patched delivery engine** to
> `edge-proc` — "casync + TUF for AI constructs." The device pulls a tiny signed pointer, diffs a
> chunk manifest against what it already has, fetches **only the missing chunks** (zstd-compressed),
> verifies every chunk against its own hash and the manifest's signature, and **atomically swaps** the
> new bundle in. Heavy compressed first download → light patches when something changes. Pure-Python,
> native (filesystem) tier, TDD.

**In plain terms:** every local app re-solves the same boring delivery problem — get a big index/model
file onto the device, verify it wasn't tampered with, update it without ever leaving a half-written
file the app might read, and *not* re-download gigabytes when one byte changed. Phase A is that layer,
done once and correctly. A *construct* (an index, a normalization table, optionally model weights) is
split into variable-size **chunks** (a "chunk" is a content-determined slice of a file); each chunk is
named by the sha256 of its **plaintext** and stored zstd-compressed. A **manifest** is just the
ordered list of chunk hashes per file, plus a signature. To update, the device fetches `/latest` (a
few signed bytes), and if it changed, fetches the new manifest, subtracts the chunks it already holds,
and pulls the rest. The CDN only serves bytes — **compute never leaves the device; the edge is
delivery-only.** Trust is re-established offline on-device by two signature checks, so the origin is
untrusted transport.

### Why it works — worked example

| Situation | What the engine does | Why it wins |
|---|---|---|
| Steady state: nothing changed upstream | Fetch `/latest` (tiny signed pointer), compare to staged pointer, stop | **Zero chunk bytes** transferred — the common case is one small request |
| One file in a 2 GB index edited by a few KB | Manifest diff = chunk set-difference; only the chunks straddling the edit re-fetch (CDC keeps boundaries local) | Kilobytes move, not gigabytes — a one-byte edit re-chunks *locally*, not globally |
| A chunk is fetched but its bytes are corrupt/tampered | Decompress → sha256 → compare to the chunk's name → reject (fail-closed) | The content-address *is* the integrity check; a bad chunk never reaches the store |
| Origin/CDN is compromised and serves a forged manifest | Pointer signature and manifest signature are verified on-device against a **pinned** trust-root key → reject | TUF-style: one manifest signature transitively authenticates every chunk it lists |
| Process crashes mid-swap | Reader sees the **old** bundle, intact and readable; staging dir is discarded on next sync | `os.replace` of the pointer is atomic; a reader observes old-or-new, never hybrid |

### The honest boundary

Phase A is the **native filesystem tier only**, and it is delivery + verification + swap — **not
compute**. In scope: Gear-CDC chunking, a filesystem content-addressed store, ed25519 detached
signatures behind a Protocol, canonical manifest serialization, atomic pointer swap with mark-sweep
GC, and the `sync_index` engine wired behind a new CLI command. **Deferred** (named in full below):
the OPFS/browser `CacheStore` (Phase C), Sigstore keyless signing (behind the same `Signer`/`Verifier`
Protocols — zero consumer change later), edge-reco *consuming* this engine (Phase B), zstd
dictionaries, the Linux `renameat2` fast-path, packfiles / multi-level fan-out, and HTTP range-resume
*mid-chunk* (chunk-level granularity already gives resumability). No "production-ready" claim: this is
the first native tier, proven by the verification section at the end.

## Goal & invariant

Add a local-first, content-addressed, signed, incrementally-patched delivery engine. The core
invariant carried from the north-star spec: **compute stays on the device; the edge is delivery-only
(a CDN).** Data flows down; results never flow up. The operating principle is "heavy compressed first
download → light patches when something changes," and the trust boundary is the supply chain itself:
because there is no server-side gatekeeper between a tampered construct and on-device execution, the
on-device signature + hash checks *are* the gatekeeper, and they are mandatory and fail-closed.

This is the established casync/desync + TUF pattern (content-defined chunking + sign-the-manifest-not-
every-chunk), applied to AI constructs. It is adopted, not invented.

## What's reused vs new

### REUSE — keep intact, do NOT break (pinned by tests)

The v1 file-level bundle path ships today and stays. Phase A adds the v2 chunked engine *alongside*
it; it does not rewrite it.

- **v1 manifest models + checksum** — `BundleManifest` / `BundleFile` / `parse_manifest`
  (`edgeproc/bundles/manifest.py:21-41`) and `validate_checksum` (`manifest.py:44-49`): whole-file
  sha256, **fail-closed on a missing `sha256:` prefix** (`manifest.py:46-47`). Untouched.
- **Fetch adapters** — the `FetchAdapter` Protocol (`edgeproc/bundles/adapters.py:23-29`),
  `FilesystemAdapter` (`adapters.py:32-41`), and `HttpAdapter` (httpx, streaming via `iter_bytes`,
  `adapters.py:44-69`) with its `transport=` test seam (`adapters.py:48-51`). Extended (new methods),
  not replaced.
- **v1 sync** — `sync_bundle` (`edgeproc/bundles/sync.py:22-37`): raises `ValueError` on a checksum
  mismatch (`sync.py:50`) and writes `manifest.json` **last** (`sync.py:35`). Stays as the v1 path.
- **Settings** — `EdgeProcSettings.http_timeout` (`edgeproc/core/settings.py:37`), read **lazily**,
  never at import (the lazy-construct rule, `settings.py:8-9`; live use at `adapters.py:50`). Extended
  with one new field (below).
- **CLI** — the existing `bundle-sync` command (`edgeproc/cli/app.py:48-71`) stays; v2 is a new
  command.

**Two pinned tests must stay green** (the v1 contract):
- `tests/bundles/test_sync.py::test_sync_downloads_verifies_and_caches` — `manifest.json` is cached
  after a successful sync.
- the `ValueError`-on-checksum-mismatch test in `tests/bundles/test_sync.py` — a mismatch raises rather
  than caching corrupt bytes.

Both are accounted for: the v2 engine is additive and shares neither code path nor module-level state
with v1, so neither test changes.

### NEW — the Phase A engine

All new code lands under `edgeproc/bundles/`, behind the `[bundles]` extra, test-first.

## Module layout (new files under `edgeproc/bundles/`)

### 1. `chunking.py` — content-defined chunking

- `Chunker` Protocol: `chunk(data: bytes) -> Iterator[bytes]`.
- `GearCDC` implementation: a ~50-line Gear-hash content-defined chunker. A **FIXED, committed
  256-entry Gear table** (a module-level `Final` tuple constant) makes boundaries **deterministic and
  reproducible across versions and machines** — the same bytes always split into the same chunks. This
  is why the table is a committed constant, never generated at runtime and never config.
- Sizes: **min 16 KiB / avg 64 KiB / max 256 KiB** (the casync reference profile). These are
  reproducibility-affecting module constants, **not** settings (see Config below).
- Small payloads (`< min`) yield a single chunk.
- Rationale (Decisions): own Gear-CDC impl rather than the stale/untyped `fastcdc` dependency —
  controllable, typed, and the fixed table guarantees reproducibility.

Glossary — *content-defined chunking (CDC)*: split a file at boundaries chosen by a rolling hash of the
content (not at fixed offsets), so inserting/removing bytes shifts only the chunks near the edit, not
every chunk after it.

### 2. `compression.py` — per-chunk zstd

- `zstandard` one-shot compress/decompress, **per chunk**.
- The decompressed-size hint comes from the **manifest** (`ChunkRef.size`, the uncompressed size), so
  the decompressor can size its buffer without trusting the stored bytes.
- May be folded into `cas.py` if it stays trivial; kept as a named seam here for clarity.

### 3. `cas.py` — content-addressed store

- `CacheStore` Protocol + `FilesystemCacheStore` implementation.
- **Layout** (2-char fan-out is mandatory to relieve filesystem inode/directory pressure):
  - `chunks/<aa>/<sha256>` — stores the **zstd-compressed** bytes, where `<aa>` is the first two hex
    chars of the hash.
  - `manifests/<sha256>` — the canonical manifest bytes.
  - `active` — the pointer file naming the currently-promoted manifest/bundle.
- **Hash-over-plaintext (pinned decision):** the content-address is `sha256(plaintext_chunk)`. The
  stored file holds `zstd(plaintext)`; the manifest records the **uncompressed** size. The read path is
  always: fetch → decompress → `sha256` → compare to the file's name → **fail-closed** on any mismatch.
- Methods (illustrative): `has(hash) -> bool`, `put_chunk(plaintext: bytes) -> str` (returns the
  hash), `get_chunk(hash) -> bytes` (decompress + verify), `put_manifest(...)`, `read_active()`,
  `write_active(...)`, plus the chunk-set enumeration needed for diff + GC.

### 4. `signing.py` — detached signatures, TUF-style root of trust

- `Signer` / `Verifier` Protocols + `Ed25519Signer` / `Ed25519Verifier` via `cryptography`
  (`cryptography.hazmat.primitives.asymmetric.ed25519`).
- Sign / verify the **canonical manifest bytes** (from §5) and the **version pointer**.
- The public verify key is **pinned as a TUF-style root of trust**, loaded from config
  (`trust_root_pubkey_path`, see Config). Fail-closed: no signature, unknown key, or mismatch → reject.
- **Sigstore keyless is DEFERRED behind these same Protocols** — a future `SigstoreVerifier` slots in
  with zero consumer change, because consumers only ever see `Verifier`.

### 5. `manifest.py` — v2 models (extend; keep v1 intact)

The v1 models above are untouched. New v2 Pydantic models are added in the same module:

- `ChunkRef(hash: str, size: int)` — `size` is the **uncompressed** chunk size.
- `FileEntry(path: str, file_type: str | None, size: int, file_sha256: str, chunks: list[ChunkRef])`.
- `IndexManifest(schema_version: int, bundle_id: str, version: str, files: list[FileEntry], metadata)`
  — `metadata` uses a **`Scalar` union** (`str | int | float | bool | None`), mirroring shared-libs'
  `Scalar` / `Metadata` convention (`shared_libs_python.vector_mgmt.core.types`). **Never**
  `Dict[str, Any]` / `TypedDict`.
- `VersionPointer(manifest_hash: str, version: str, signature: str)`.
- **Canonical serialization function** (pinned decision): sorted keys, UTF-8, fixed separators, no
  whitespace ambiguity — the exact bytes that get signed. This is pinned because **signatures will not
  reproduce** across machines/versions otherwise. Both signing and verification go through this one
  function; there is no other path to manifest bytes.

### 6. `swap.py` — atomic promote + GC (or folded into `sync.py`)

- **Atomic promote:** stage the reassembled bundle + manifest **fully** on the **same filesystem**,
  `fsync`, write a **new pointer file**, then `os.replace(new_pointer, active_pointer)`.
  - `os.replace` is atomic on POSIX **and** Windows. Pinned decision: **not** `renameat2`
    (Linux-only fast-path, deferred) and **not** symlinks (the Windows symlink-privilege trap).
  - A concurrent reader sees **old-or-new, never hybrid**.
  - Caveats captured: the staged content **must be on the same mount** as the active pointer (cross-
    device `os.replace` is not atomic), and **fsync-before-publish** is required or a crash can publish
    a torn bundle.
- **GC:** mark-sweep orphaned chunks against the **active manifest's chunk set** — anything in
  `chunks/` not referenced by the active manifest is sweepable.

### 7. `sync.py` — v2 engine (extend; add alongside v1 `sync_bundle`)

`sync_index(...)` runs alongside `sync_bundle`. The numbered flow:

1. Fetch `/latest` version pointer via the adapter → **verify the pointer signature** (fail-closed).
2. Fetch the manifest by `manifest_hash` → verify it **hashes to** the pointer's `manifest_hash` →
   **verify the manifest signature** (TUF transitive trust: the manifest hashes then authenticate every
   chunk it lists).
3. **Diff:** `manifest chunk-set` MINUS `chunks already in local CAS` = the **missing set**.
4. Fetch each missing `/chunk/<hash>` → on arrival, **decompress + sha256 + compare to the name**
   (fail-closed) → `put` into the CAS. Chunks are independent and immutable, so **resume is free** —
   a failed/aborted chunk just re-fetches; nothing else is touched.
5. **Reassemble** each file: concatenate its chunks in order → verify `file_sha256` → write into the
   **staged** bundle directory.
6. **Atomic-swap** the active pointer (§6).
7. **GC** orphans (§6). Return a result carrying the manifest + stats: **bytes fetched, chunks reused
   vs fetched**.

### 8. `adapters.py` — chunk/bytes fetching (extend)

- Add a chunk/bytes fetch to the `FetchAdapter` Protocol — e.g. `fetch_chunk(base, hash, local_path)`
  or a generic `fetch_bytes(...)`. Implement in **both** `FilesystemAdapter` and `HttpAdapter`,
  keeping the streaming pattern and the `transport=` test seam (`adapters.py:48-51`).
- **PERF improvement (pinned as required):** `HttpAdapter` currently opens a **new `httpx.Client` per
  call** (`adapters.py:54`, `adapters.py:63`). A many-chunk sync must **reuse ONE client for the
  duration of a sync** (connection-pooling / keep-alive); refactor the adapter to accept/own a client
  for the sync's lifetime while preserving the `transport=` seam.

### 9. `cli` — v2 sync command (extend; keep v1)

- Add: `edgeproc sync --pointer-url <url> --cache-dir <dir> [--http] [--key <pinned-pubkey>]`.
- Keep the v1 `bundle-sync` command (`edgeproc/cli/app.py:48-71`) exactly as-is.
- Mirror v1's lazy import of the `[bundles]` extra and its `Exit(code=1)` "install edge-proc[bundles]"
  message (`app.py:56-62`). Exit code mirrors success (`0` ok, `1` otherwise), matching `route`'s
  convention (`app.py:93`).

## Config (`EdgeProcSettings`, extend, lazy)

Add **one** field to `edgeproc/core/settings.py`:

- `trust_root_pubkey_path: Path | None = None` — the pinned TUF-style trust-root public key, read
  **lazily** (constructed where a default is needed, never at import — the rule at `settings.py:8-9`).
  Env: `EDGEPROC_TRUST_ROOT_PUBKEY_PATH` (the `EDGEPROC_` prefix, `settings.py:28`).

**Do NOT** make CDC chunk sizes env-tunable. They affect chunk-boundary reproducibility, so the
min/avg/max and the Gear table are **module constants in `chunking.py`**, never settings. Making them
configurable would let two devices produce different chunkings of the same bytes and silently defeat
the diff.

## Origin (Caddy) HTTP contract

The origin/CDN is **untrusted transport** — trust is re-established offline on-device by the two
signature checks (pointer + manifest). The contract:

| Path | Mutability | Cache-Control | Body |
|---|---|---|---|
| `GET /chunk/<sha256>` | immutable | `public, immutable, max-age=31536000` | zstd-compressed chunk bytes |
| `GET /manifest/<sha256>` | immutable | `public, immutable, max-age=31536000` | canonical manifest bytes |
| `GET /latest` | the **only** mutable object | `max-age=5..30, must-revalidate` (short TTL) | the signed `VersionPointer` |

Chunks and manifests are content-addressed, therefore immutable and infinitely cacheable. `/latest` is
the single mutable, short-TTL object — the pull-based change signal.

## Testing strategy (TDD — this is a test-first build)

Red → green → refactor, per module. Tests describe behavior, not implementation. Existing style:
function-style pytest, `tmp_path`, `monkeypatch`, `FilesystemAdapter` as the real-IO fake, and
**`httpx.MockTransport` injected via `transport=`** for HTTP unit tests (**NOT** respx) — matching the
shipped `tests/bundles/` style.

Per-module unit tests:

- **`chunking.py`** — determinism + boundary reproducibility: same bytes → identical chunk sequence; a
  **1-byte edit re-chunks locally, not globally** (chunks before/after the edit region are unchanged);
  sub-min payload → one chunk.
- **`cas.py`** — `put`/`get`/verify round-trip; **tamper rejection** (corrupt a stored chunk → `get`
  fails closed); 2-char fan-out layout asserted.
- **`signing.py`** — sign→verify round-trip; **reject a tampered manifest**; **reject an unknown key**
  (fail-closed); no-signature → reject.
- **`manifest.py`** — canonical-serialization **stability**: re-serialize an equal model → byte-
  identical output → identical signature.
- **`swap.py`** — swap **atomicity**, including a **mid-failure leaving the OLD bundle intact and
  readable**; GC removes only true orphans (never a chunk the active manifest references).
- **`sync.py`** — diff fetches **ONLY missing chunks** (assert reused-vs-fetched counts); a **"patch"
  scenario**: change one file → only its changed chunks fetch; pointer/manifest signature failure →
  fail-closed, no swap.

Integration test:

- **One full integration test** running the complete `sync_index` + a patch against a **REAL local
  HTTP origin** serving a CAS directory — `python -m http.server`, or the edge-reco Caddy stack pattern
  at `deploy/docker-compose.yml`. Note: edge-reco's Caddy is **TTL-cached today**; the
  immutable-chunk + short-TTL-`/latest` policy above is the **evolution** of that stack, reusable as-is
  for wiring and protocol tests.

Keep the **two pinned v1 sync/manifest tests green** (the v1 contract above).

## Quality bar (must pass `uv run poe gate`)

`poe gate` is lint + fmt-check + typecheck + complexity + test (`pyproject.toml:131`). The bar:

- **≤15-line functions**; **xenon Grade A** (`--max-absolute A --max-modules A --max-average A`,
  `pyproject.toml:128`) ⇒ mccabe ≤5 (`pyproject.toml:81`).
- **mypy --strict** (`pyproject.toml:85`, `typecheck` task `pyproject.toml:136-137`).
- **No `Dict[str, Any]` / `TypedDict`** — Pydantic models + the **`Scalar` union** for `metadata`,
  mirroring shared-libs' `Scalar` / `Metadata` convention.
- **Fail-closed**; **raise, don't swallow**.
- **≥90% coverage** in the same commits as the code (`--cov-fail-under=90`, `pyproject.toml:103`). No
  "tests later."

New dependencies go in the **`[bundles]` extra** (`pyproject.toml:36-39`, beside `httpx` + `structlog`):

- `zstandard>=0.25`
- `cryptography>=44`

Both ship type stubs (typed), so no `[[tool.mypy.overrides]]` should be needed; add one **only** if a
dep turns out to lack stubs.

## Explicitly OUT of scope / deferred

- **OPFS / browser `CacheStore`** — Phase C (a different storage primitive behind the same
  `CacheStore` Protocol).
- **Sigstore keyless signing** — behind the `Signer` / `Verifier` Protocols; ed25519 is Phase A.
- **edge-reco consuming this engine** — Phase B (the dogfood/contract proof).
- **zstd dictionaries** — shared-dictionary first-load optimization; a product choice, not sync work.
- **Linux `renameat2` fast-path** — `os.replace` is the portable Phase A swap.
- **Packfiles / multi-level fan-out** — single 2-char fan-out is enough for Phase A scale.
- **HTTP range-resume mid-chunk** — chunk-level granularity already gives resumability (a failed chunk
  re-fetches whole; chunks are ≤256 KiB).

## Decisions pinned

- **Hash-over-plaintext** — content-address is `sha256(plaintext)`; the store holds `zstd(plaintext)`;
  the manifest records the uncompressed size.
- **Gear-CDC own impl** — not the stale/untyped `fastcdc`; a fixed committed 256-entry table +
  16/64/256 KiB profile for reproducible boundaries.
- **ed25519 detached, behind a Protocol** — Sigstore keyless deferred behind the same `Signer` /
  `Verifier` seam (zero consumer change later).
- **`os.replace` pointer swap** — not `renameat2`, not symlinks (Windows symlink-privilege trap);
  same-mount + fsync-before-publish required.
- **Full substrate now** — the user chose building the full chunked substrate now over a staged
  partial.
- **Keep the v1 path intact** — the chunked v2 engine is additive; `sync_bundle` + the v1 manifest
  models stay, and the two pinned tests stay green.

## Verification

How to prove Phase A end-to-end:

1. **`uv run poe gate` is green** — lint, fmt-check, mypy --strict, xenon Grade A, and pytest at
   ≥90% coverage all pass (`pyproject.toml:131`).
2. **Patch integration test shows only-changed-chunks fetched** — the full `sync_index` + patch run
   against a real local HTTP origin asserts the reused-vs-fetched counts: editing one file fetches only
   that file's changed chunks.
3. **Tamper is rejected fail-closed** — a corrupted chunk (hash ≠ name) and a forged/tampered manifest
   (signature/hash mismatch) are both rejected; nothing is staged or swapped.
4. **`edgeproc sync` round-trips against a local origin** — the new CLI command syncs a bundle from a
   real `python -m http.server` / Caddy origin into a local CAS, atomically promotes it, and exits `0`;
   the v1 `bundle-sync` command still works unchanged.

<!--
Spec voice: matches ~/dev/project-ideas/oss/edgeproc.md — TL;DR/why before how, plain-language
glosses, honest deferred-list, no "production-ready" claim.
-->
