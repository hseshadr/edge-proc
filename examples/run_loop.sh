#!/usr/bin/env bash
# End-to-end EdgeProc demo: keygen → build local index → publish → sync → route.
#
# Walks every shipped CLI verb against a tiny realistic catalog so a stranger can
# see what the substrate actually does in under a minute. Uses a per-run temp
# workspace; cleans up automatically.
set -euo pipefail

# Resolve paths relative to this script so it runs from anywhere.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
WORK="$(mktemp -d -t edgeproc-demo-XXXXXX)"
trap 'rm -rf "$WORK"' EXIT

# Prefer `uv run` if available so the example uses the repo's locked env.
if command -v uv >/dev/null 2>&1 && [ -f "$REPO_ROOT/pyproject.toml" ]; then
    EDGEPROC=(uv --project "$REPO_ROOT" run edgeproc)
    PYTHON=(uv --project "$REPO_ROOT" run python)
else
    EDGEPROC=(edgeproc)
    PYTHON=(python)
fi

banner() { printf "\n=== %s ===\n" "$1"; }

banner "1. keygen — mint an ed25519 keypair (the trust root)"
"${EDGEPROC[@]}" keygen --out "$WORK/keys"

banner "2. build a saved FAISS index from examples/catalog.json"
"${PYTHON[@]}" "$HERE/quickstart.py" --catalog "$HERE/catalog.json" --out "$WORK/src/catalog_idx"

banner "3. publish — chunk + sign the saved index into a content-addressed origin"
"${EDGEPROC[@]}" publish \
    --src "$WORK/src" \
    --origin-dir "$WORK/origin" \
    --key "$WORK/keys/private.key" \
    --bundle-id catalog \
    --version 1.0.0 \
    --pretty

banner "4. sync — pull onto a fresh consumer cache, verifying against the pinned pubkey"
"${EDGEPROC[@]}" sync \
    --base-url "$WORK/origin" \
    --cache-dir "$WORK/cache" \
    --key "$WORK/keys/public.key" \
    --materialize-to "$WORK/materialized" \
    --pretty

banner "5. route — run a SEARCH task through a LocalVecRuntime over the synced cache"
cat > "$WORK/task.json" <<'JSON'
{"kind": "search", "payload": {"query": "trail running shoes for the morning", "k": 3}, "privacy_mode": "local_only"}
JSON
"${EDGEPROC[@]}" route \
    --index-dir "$WORK/materialized/catalog_idx" \
    --task "$WORK/task.json" \
    --pretty

banner "done — every stage succeeded; workspace ($WORK) will be cleaned up"
