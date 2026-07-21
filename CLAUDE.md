# EdgeProc (edge-proc)

AI-native local execution substrate: a pure deterministic router over pluggable
runtimes, a FAISS local-vector runtime, and a signed bundle/sync substrate.
Repo/package name is `edge-proc`; the **import name is `edgeproc`**.

## Status

Portfolio status (rank, tier, current state, next gating move) lives in the
portfolio status table at `~/dev/project-ideas/oss/README.md` — **never restate
it here**. Design spec: `~/dev/project-ideas/oss/edgeproc.md`.

## Stack

- Python ≥3.13, uv (committed `uv.lock`), hatchling build, `py.typed` shipped.
- pydantic v2 + pydantic-settings (`EdgeProcSettings`), typer CLI.
- Quality: ruff (lint + `format --check`), mypy `--strict`, xenon A/A/A,
  pytest ≥90% coverage, poethepoet task runner.
- Optional extras: `[localvec]` (faiss-cpu, sentence-transformers, rank-bm25,
  numpy) and `[bundles]` (httpx, structlog, cryptography, zstandard).
- `edgeproc-core` resolves from public GitHub via a tag-pinned git source
  in `pyproject.toml` (commented path-source override for local co-development).

## Layout

- `edgeproc/core/` — task/engine models (incl. the `CUSTOM_WASM` seam in
  `models.py`), `settings.py` (`EdgeProcSettings`), deterministic router,
  runtime registry.
- `edgeproc/localvec/` — FAISS-backed `EMBED` / `SEARCH` / `RANK` runtime
  (hybrid BM25 + vector RRF fusion).
- `edgeproc/bundles/` — content-defined chunking, content-addressed CAS,
  ed25519 signing, publish/sync with fail-closed verification.
- `edgeproc/cli/` — `edgeproc` typer app: `version` · `list-runtimes` ·
  `keygen` · `publish` · `sync` · `route`.
- `tests/` — unit + integration + public-surface tests (one pytest suite).
- `examples/` — `quickstart.py`, `run_loop.sh`, tiny realistic catalog.
- `docs/` — `ARCHITECTURE.md`, `QUICKSTART.md`, `diagrams/` (d2 sources +
  rendered SVGs — **d2, never mermaid**).

## Invariants (don't break without updating the spec)

- **Deterministic core** — routing is pure and deterministic; **no LLM in the
  routing path**. Inference is a bounded adapter behind the `Runtime` Protocol,
  never a decision-maker in the core.
- **Fail-closed verification** — every fetch verifies signature + integrity
  against the pinned ed25519 trust root or refuses to proceed. No trust root ⇒
  `sync` is refused, never "warn and continue". The `VersionPointer` is the
  only signed object.
- **Trust root and all tunables are config-driven** — `EdgeProcSettings`
  (`EDGEPROC_` env prefix; token via ecosystem-standard `HF_TOKEN`). A library
  reads config lazily (construct where needed, never at import time) and uses
  `extra="ignore"` so a host app's `.env` never crashes it.
- **Purely a dependency** — NEVER frame the portfolio's other projects
  (edge-reco, aml-filter, spookie, …) as "the consumer" or "the source repo"
  in this repo's docs, code, or tests. Generic example mentions only.
- **Truth-in-labeling (standard §8.2)** — never claim first-party WASM (or any
  unshipped runtime) as shipped. `CUSTOM_WASM` is a seam and is legitimate only
  while it points at the named roadmap item in [ROADMAP.md](ROADMAP.md).

## Workflow

- TDD is the default: failing test first, smallest change to green, refactor.
- Branches converge or die: branch → PR → CI green → merge → delete (local +
  remote). No parking lots.
- Keep-a-Changelog with `[Unreleased]` always present; the latest pushed tag
  equals the CHANGELOG's top released version; tag-forward-only (never backfill
  tags onto historical commits).

## Commands

```bash
uv sync --all-extras       # install everything (dev group included by default)
uv run poe gate            # CI mirror: lint → format-check → mypy strict → xenon A → pytest ≥90% cov
uv run poe test            # pytest with coverage only
uv run poe fmt             # apply formatting
uv run edgeproc --help     # the CLI
sh examples/run_loop.sh    # end-to-end keygen → publish → sync → route demo
```

## Quality Gates (Non-Negotiable)

Each rule carries the real shipped scar it prevents:

- **`poe gate` mirrors CI exactly, both directions — CI literally runs
  `uv run poe gate`.** Scar: a sibling repo's local lint lacked
  `ruff format --check` while CI enforced it — green locally, red remotely,
  patched with fix-forward noise commits. One-sided gate/CI drift is a config
  bug fixed in the same commit that finds it.
- **mypy `--strict`, xenon A/A/A, coverage ≥90% — no exceptions in this repo.**
  Scar: "trivial" untyped helpers are where the five-line-test bugs live.
- **CI action pins: `astral-sh/setup-uv` is full-pinned (v8.3.2).** Scar: the
  floating `@v8` major tag does not exist and fails to resolve — a "routine"
  alias bump broke CI outright. Dependabot walks the pin forward.
- **Weekly `security-audit.yml` (pip-audit over the exported lock) must stay
  green.** Scar: its first portfolio run caught 2 live CVEs sitting behind a
  green CI badge. Scheduled workflows count as CI: a red scheduled run is a red
  repo — check `gh run list`, it never blocks a merge on its own.
- **Latest pushed tag == CHANGELOG top released version.** Scar: a portfolio
  repo reached CHANGELOG 4.0.0 with tags stopped at v2.1.0 — CHANGELOG-only
  releases decay silently.
- **No hardcoded config.** Scar: the default embedding model, top-k, and HTTP
  timeout were once hardcoded (one of them duplicated) across modules — they
  are `EdgeProcSettings` fields now. New tunables go there, never inline.
- **No key material in the tree.** `*.key` is gitignored, gitleaks scans full
  history in CI, and the trust-root public key is *pointed at* by config
  (`EDGEPROC_TRUST_ROOT_PUBKEY_PATH`), never embedded.

## Engineering-standard declaration

- Standard: `~/dev/project-ideas/oss/ENGINEERING-STANDARDS.md` (Published tier).
- **§8 (WASM / edge-compute): partially applicable.** edge-proc core is a
  pure-Python library — out of §8 scope by declaration — but it owns the
  `CUSTOM_WASM` engine-kind seam (`edgeproc/core/models.py`) and the named
  §8.3 roadmap item ("First-party WASM kernel v0" — see
  [ROADMAP.md](ROADMAP.md)). §8.2 truth-in-labeling binds all docs here.
