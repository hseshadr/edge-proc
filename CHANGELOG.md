# Changelog

All notable changes to **edge-proc**. Newest first; we follow [SemVer](https://semver.org).

## [Unreleased]

House engineering-standard alignment — CI and docs only, no library code changes:

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
