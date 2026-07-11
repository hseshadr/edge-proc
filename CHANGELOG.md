# Changelog

All notable changes to **edge-proc**. Newest first; we follow [SemVer](https://semver.org).

## [Unreleased]

Security hardening pass (#11) — **additive runtime safety only**. No persisted or signed
manifest/pointer format changed; `canonical_bytes`, signing, and verification are untouched,
so every already-signed bundle still verifies and materializes unchanged.

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
