# Changelog

All notable changes to **edge-proc**. Newest first; we follow [SemVer](https://semver.org).

## [Unreleased]

### Added
- **Canonical, portable integrity failures.** Bundle-integrity exceptions now
  carry the shared `bundle.integrity_failed` code and can be rendered as RFC
  9457 Problem Details without changing their existing Python type or message.
- **Evidence-backed operating contract and benchmark.** A single operations guide now
  defines threat/privacy boundaries, recovery ownership, fixed resource limits, and a
  repeatable offline p50/p95/RSS gate for vector search and signed bundle sync.

### Changed
- **The shared error dependency now resolves from its released v0.2.0 tag.**
  Fresh clones and CI consume the same immutable public release instead of the
  temporary pre-release commit pin.
- **Filesystem mutations are cross-process serialized and bounded.** Publish, sync,
  rollback-check/promote, GC, and CLI materialization share one mutation lock, closing
  stale-last-writer and sync-vs-GC races. Lock waits fail retryably after 30 seconds.
- **Task budgets are documented truthfully.** The v0 fields are runtime declarations,
  not facade-level preemption or whole-process memory enforcement.
- **Workflow actions are immutable.** CI, gitleaks, and scheduled dependency-audit
  actions are pinned to full commit SHAs, with a regression test that rejects moving tags.
- **Security lock refresh.** `setuptools` is locked at 83.0.0, clearing
  PYSEC-2026-3447 reported by the exact-branch dependency audit.

## [0.1.4] — 2026-07-13

- **`__version__` re-synced to the released version and single-sourced.**
  `edgeproc.__version__` — and with it the `edgeproc version` CLI output and the
  `runtime_version` stamped into every `ResultEnvelope`'s provenance — had been stuck at
  `0.1.1` while the package shipped `0.1.3`. The version now lives only in
  `edgeproc/_version.py`; hatchling reads it at build time (`dynamic = ["version"]`), so the
  installed metadata and `__version__` are one value by construction. A regression test pins
  `importlib.metadata.version("edge-proc") == edgeproc.__version__`.
- **Single-point trust-boundary hardening.** Bundle models now reject non-canonical SHA-256
  values for chunk, file, and manifest digests; direct CAS calls validate digests and resolve
  every object path inside the store root, including symlinked storage directories. Monotonic
  sequences must be non-negative, and reusing an active sequence for different content is
  rejected while exact idempotent replay and legacy signed-pointer bytes remain unchanged.
  `keygen` creates or tightens its output directory to owner-only mode `0700`.

## [0.1.3] — 2026-07-12

Security hardening pass (#11) — **additive runtime safety only**. No persisted or signed
manifest/pointer format changed; `canonical_bytes`, signing, and verification are untouched,
so every already-signed bundle still verifies and materializes unchanged.

- **Local-FS hardening: `O_NOFOLLOW` on key writes + aggregate sync caps.** `keygen` now
  writes `private.key`/`public.key` with `O_NOFOLLOW` (portable via `getattr`), so a symlink
  pre-planted at a key path is refused (ELOOP → fail-closed) instead of redirecting the write
  onto a victim file; materialization was already symlink-safe via the §3.1 containment gate, so
  it is unchanged. `sync_index` gains a fail-closed aggregate ceiling — `max_files` (refused
  before any fetch) and `max_total_bytes` (a running ceiling that aborts before writing the chunk
  that would cross it) — so a hostile or runaway manifest can't enumerate unbounded chunks/files
  to exhaust disk. Defaults are generous (4 GiB / 100k files) and configurable via
  `EdgeProcSettings`; `sync` behavior on a legitimate bundle is unchanged.
- **Signed-pointer identity binding + monotonic sequence (opt-in, backward-compatible).**
  `VersionPointer` gains three optional fields — `bundle_id`, `channel`, `sequence` — folded
  into the signed bytes only when set (`pointer_signing_bytes`), so a pointer that binds none
  of them hashes to the exact legacy `{manifest_hash, version}` preimage and every
  already-signed pointer verifies byte-for-byte. `publish --bind-identity`/`--channel`/
  `--sequence` stamp them; `sync --expected-bundle-id`/`--expected-channel` pin the consumer
  so a validly-signed pointer minted for another bundle/channel (a cross-bundle replay under a
  shared key + transport compromise) is refused before promote, and a pointer whose bound
  `bundle_id` disagrees with its manifest is rejected. A provably-lower `sequence` is refused at
  `promote` alongside the PEP 440 anti-rollback guard, and `is_fresh_sequence` gives a downstream
  a strict-monotonic freshness/anti-replay predicate. All identity/freshness inputs are opt-in;
  the default `publish`/`sync` behavior and the persisted pointer format are unchanged.
- **Trust-boundary path containment (§3.1 trust gate).** New `bundles/containment.py`
  chokepoint refuses traversal (`../`), backslash, and absolute paths. A `FileEntry.path`
  `field_validator` rejects an unsafe path at parse time, and materialization re-checks the
  fully-resolved target still lies inside the output root (catches symlink/zip-slip escapes).
- **Private key written 0600.** `keygen` now writes `private.key` with owner-only
  permissions instead of the umask default (world-readable 0644).
- **Decompression-bomb + oversized-body caps.** CAS decompression streams at most
  `max_decompressed_bytes` (default 64 MiB) rather than trusting the zstd frame's
  content-size header, and the HTTP adapter refuses a response body past `max_fetch_bytes`
  (default 256 MiB) — both fail-closed and configurable via `EdgeProcSettings`.
- **Anti-rollback on promote.** `promote()` refuses a signed pointer whose version is
  provably older (PEP 440) than the active one, so a replayed stale `/latest` cannot
  downgrade a client. Equal/forward versions, first promote, and unparseable versions are
  still allowed — a valid signed bundle is never rejected.
- **FAISS stale-row purge.** Deleting an id then re-inserting it no longer leaves the old
  physical row addressable; search never returns the duplicated/stale-scored entity, and
  `get_stats` counts the superseded row so a rebuild compacts it.
- **CVE lock bumps (#9).** `torch` 2.12.0→2.13.0 (CVE-2025-3000), `cryptography`
  48.0.0→49.0.0 (GHSA-537c-gmf6-5ccf), `pydantic-settings` 2.14.1→2.14.2
  (GHSA-4xgf-cpjx-pc3j) — all in-range lock bumps, no `pyproject` floor changes.

## [0.1.2] — 2026-07-11

Propagation-chain release: re-pins the upstream Lego so downstream consumers can bump
in one hop (`shared-libs-python v0.1.3 → edge-proc v0.1.2 → edge-reco`). No library
code changes.

- **Deps.** `shared-libs-python` git-tag pin bumped v0.1.2 → **v0.1.3** (upstream
  release is gate/CI/docs-only — zero runtime change).

Also ships the house engineering-standard alignment — CI and docs only:

- **CI.** The workflow now literally runs `uv run poe gate`, so the local gate and CI
  can never drift one-sidedly; minimal token permissions; `uv sync --frozen`;
  `astral-sh/setup-uv` full-pinned to v8.3.2 (no floating major tag exists); new
  full-history gitleaks secret-scan job.
- **Security.** Weekly `security-audit.yml` (pip-audit over the exported lock) and
  `dependabot.yml` (weekly, grouped: github-actions + uv ecosystems).
- **Docs.** New `CLAUDE.md` (agent guide: invariants, commands, scarred quality gates);
  the roadmap's WASM entry upgraded to the named "First-party WASM kernel v0" item with
  a gradeable definition of done (README + ROADMAP.md).

## 0.1.1 — 2026-06-19

Public open-source release (MIT). Part of the `edge-reco → edge-proc →
shared-libs-python` stack going public together; live demo at https://edge-reco.com.

- **Clone-and-go onboarding.** `shared-libs-python` is now pulled from public GitHub
  via a git source pinned to a tag (`[tool.uv.sources]`), so `git clone … && uv sync`
  works with no sibling checkout. A commented path-source override remains for local
  co-development.
- **CI simplified.** Dropped the private-sibling checkout + path-patch steps and the
  `PORTFOLIO_PAT` secret — CI now builds exactly as an external cloner does.
- **Docs.** README sharpened to lead with the substrate value proposition (edge compute
  cost, CDN-scale, offline resilience) and cross-link the three-repo stack.

## 0.1.0 — 2026-05-28

First public release: the AI-native local execution substrate as a library + CLI.

- **Core seam.** `EdgeProc` facade, `Runtime`/`Router`/`TelemetrySink` Protocols,
  pure-deterministic `DefaultRouter`, `RuntimeRegistry`, fail-closed
  `no_runtime_accepted` envelope. No LLM in the routing path.
- **LocalVec runtime.** FAISS-backed `EMBED` / `SEARCH` / `RANK` with hybrid BM25 +
  vector RRF fusion. `LocalVecRuntime.from_texts(catalog, encoder=...)` is the
  one-call wiring for the README quickstart.
- **Signed bundle/sync substrate** (`[bundles]` extra). Content-defined chunking
  (GearCDC), content-addressed CAS with zstd compression + atomic promote + GC,
  ed25519-signed `VersionPointer` (the only signed object), fail-closed
  signature/integrity/decompress checks, hardlink-deduped origin layout so
  re-publishing an unchanged catalog touches zero chunks.
- **CLI** (`edgeproc`). `version`, `list-runtimes`, `keygen`, `publish`, `sync`,
  `route` — every fetch path verifies against a pinned trust-root pubkey or
  refuses to run.
- **End-to-end example.** `examples/quickstart.py` + `examples/run_loop.sh`
  exercise keygen → publish → sync → route over a tiny realistic catalog.

The Wasmtime deterministic kernel, Biscuit capability tokens, and Sigstore-keyless
bundles are kept as Protocol seams for future drop-in — not in 0.1.0.
