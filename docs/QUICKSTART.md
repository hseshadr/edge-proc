# Quickstart

Goal: clone the repo, run the gate, then walk a real catalog through the full **keygen → publish → sync → route** loop. Five minutes, end-to-end.

## Prereqs

- Python 3.13+
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- About 200 MB free for the model + index (first run downloads `all-MiniLM-L6-v2` ~90 MB)

## 1. Clone and gate

```bash
git clone https://github.com/hseshadr/edge-proc.git
cd edge-proc

uv sync --all-extras    # core + [localvec] + [bundles] + dev tooling
uv run poe gate         # lint + format-check + mypy strict + Radon Grade A + pytest (≥90% statement+branch cov)
```

`poe gate` is the same set of checks CI runs. If it passes locally, CI passes.

## 2. Persist a catalog index

A `route` call needs an on-disk index. Save one (the FAISS file + a small `state.json` sidecar):

```bash
cat > save_index.py <<'PY'
import asyncio
from pathlib import Path

from edgeproc_core.vector_mgmt.core.types import IndexConfig, VectorEmbedding

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
PY

uv run python save_index.py
```

## 3. Sign a release on the publisher

`keygen` mints an ed25519 keypair. **`public.key` is the pin** a consumer trusts; distribute it out-of-band. **`private.key` never leaves the publisher.**

```bash
uv run edgeproc keygen --out keys
#   wrote keys/private.key and keys/public.key

mkdir -p src && cp -r catalog_idx src/

uv run edgeproc publish \
    --src src \
    --origin-dir origin \
    --key keys/private.key \
    --bundle-id catalog \
    --version 1.0.0 \
    --pretty
#   published v1.0.0 manifest=c4c28ab05da5
```

`origin/` now holds the full CDN contract: `latest` (the signed pointer), `manifest/<hash>`, and `chunk/<hash>` (one zstd blob per unique chunk). Point a static server or CDN at it as-is.

## 4. Sync onto a consumer

Pull the signed bundle into a fresh cache, trusting **only** the pinned pubkey. `--materialize-to` reassembles the synced files into a plain directory so a follow-on `route` can read them directly.

```bash
uv run edgeproc sync \
    --base-url origin \
    --cache-dir cache \
    --key keys/public.key \
    --materialize-to materialized \
    --pretty
#   synced v1.0.0 manifest=c4c28ab05da5 chunks_fetched=2 chunks_reused=0 bytes_fetched=5903
```

Without `--key` (and without `EDGEPROC_TRUST_ROOT_PUBKEY_PATH` set) `sync` refuses to run — an unverifiable pull is rejected fail-closed.

## 5. Route a task against the delivered index

```bash
cat > task.json <<'JSON'
{"kind": "search", "payload": {"query": "shoes for running", "k": 3}, "privacy_mode": "local_only"}
JSON

uv run edgeproc route \
    --index-dir materialized/catalog_idx \
    --task task.json \
    --pretty
#   success=True runtime=localvec latency=112.7ms
#     p1  0.219
#     p4  0.246
#     p2  0.556
```

Distances are deterministic for the same model and catalog; `latency` varies by machine.

The exit code mirrors `success` (`0` ok, `1` for `no_runtime_accepted` or any verification failure), so scripts can branch on it without parsing JSON.

## 6. Test a delta release

Publish a `1.0.1` with a small edit. Re-`sync` should fetch only the changed chunks (`chunks_reused > chunks_fetched`):

```bash
echo "tiny edit" >> src/catalog_idx/state.json

uv run edgeproc publish \
    --src src --origin-dir origin --key keys/private.key \
    --bundle-id catalog --version 1.0.1 --pretty
#   published v1.0.1 manifest=312b66ae9d63

uv run edgeproc sync \
    --base-url origin --cache-dir cache --key keys/public.key \
    --materialize-to materialized --pretty
#   synced v1.0.1 manifest=312b66ae9d63 chunks_fetched=1 chunks_reused=1 bytes_fetched=157
```

157 bytes instead of the original 5,903 — one changed chunk re-fetched, the rest reused.

## 7. Try a tampered origin

Corrupt any chunk under `origin/chunk/` and sync into a fresh cache:

```bash
printf 'corrupted' > "origin/chunk/$(ls origin/chunk | head -1)"

uv run edgeproc sync \
    --base-url origin --cache-dir cache3 --key keys/public.key --pretty
#   [bundle.integrity_failed] sync failed: stored chunk failed to decompress
echo $?
#   1

ls cache3
#   chunks		manifests
```

A healthy cache has an `active/` directory; `cache3/` has none, because the bad version was
never promoted. That's the fail-closed contract in one command.

Sync also refuses to run at all without a pinned key:

```bash
uv run edgeproc sync --base-url origin --cache-dir cache2 --pretty
#   [config.missing] no trust root: pass --key or set EDGEPROC_TRUST_ROOT_PUBKEY_PATH (refusing to sync)
```

## Next steps

- Read [ARCHITECTURE.md](ARCHITECTURE.md) for the module map and the security model.
- See [`examples/`](../examples/) for a registry-wired in-process version that doesn't use the CLI.
- Browse `docs/diagrams/` if you prefer pictures.

## Going over the wire

Add `--http` to `sync` and serve `origin/` over any static HTTP server / CDN — that's the production deployment shape. The contract is identical; only the transport changes.
