# Changelog

All notable changes to **edge-proc**. Newest first; we follow [SemVer](https://semver.org).

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
