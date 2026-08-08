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

banner "2. BUILD MACHINE — index examples/catalog.json and save the model beside it"
# This is the one step permitted to use the network, and it says so out loud. EdgeProc
# refuses model downloads by default; a build machine opts in, a device never does. The
# model lands in $WORK/src/model so step 3 signs it into the same bundle as the index —
# that is what makes step 5's "no network" real rather than a warm-cache accident.
EDGEPROC_ALLOW_MODEL_DOWNLOAD=1 "${PYTHON[@]}" "$HERE/quickstart.py" \
    --catalog "$HERE/catalog.json" \
    --out "$WORK/src/catalog_idx" \
    --model-out "$WORK/src/model"

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

banner "5. DEVICE — route a SEARCH task using only what sync verified, no network"
# --model-path points at the model that arrived inside the signed bundle. No
# EDGEPROC_ALLOW_MODEL_DOWNLOAD here on purpose: if the model had not shipped in step 3,
# this refuses with [config.missing] rather than quietly fetching it from the hub.
#
# Every Hugging Face cache variable is redirected at an empty directory for this step, so
# the machine's own warm cache cannot answer. If this search returns hits, the weights
# came out of the verified bundle and nothing else. That redirection IS the evidence —
# without it a passing run would prove only that this laptop had downloaded the model
# once, which is exactly how a false "works offline" claim shipped green before.
mkdir -p "$WORK/cold-hf"
cat > "$WORK/task.json" <<'JSON'
{"kind": "search", "payload": {"query": "trail running shoes for the morning", "k": 3}, "privacy_mode": "local_only"}
JSON
env HF_HOME="$WORK/cold-hf" \
    HF_HUB_CACHE="$WORK/cold-hf" \
    SENTENCE_TRANSFORMERS_HOME="$WORK/cold-hf" \
    TRANSFORMERS_CACHE="$WORK/cold-hf" \
    XDG_CACHE_HOME="$WORK/cold-hf" \
    "${EDGEPROC[@]}" route \
    --index-dir "$WORK/materialized/catalog_idx" \
    --task "$WORK/task.json" \
    --model-path "$WORK/materialized/model" \
    --pretty

banner "6. the guard, shown refusing — same device, same cold cache, no local model"
# A guard nobody has watched fail is not evidence. Drop --model-path and the identical
# command must REFUSE, not quietly fetch 87 MB from huggingface.co. Non-zero exit here is
# the expected outcome, so the script asserts the refusal instead of aborting on it.
if env HF_HOME="$WORK/cold-hf" \
    HF_HUB_CACHE="$WORK/cold-hf" \
    SENTENCE_TRANSFORMERS_HOME="$WORK/cold-hf" \
    TRANSFORMERS_CACHE="$WORK/cold-hf" \
    XDG_CACHE_HOME="$WORK/cold-hf" \
    "${EDGEPROC[@]}" route \
        --index-dir "$WORK/materialized/catalog_idx" \
        --task "$WORK/task.json" \
        --pretty 2>"$WORK/refusal.txt"; then
    echo "FAIL: route without a local model should have refused, but it succeeded" >&2
    exit 1
fi
grep -q 'config.missing' "$WORK/refusal.txt" || {
    echo "FAIL: refused, but not with the canonical config.missing code:" >&2
    cat "$WORK/refusal.txt" >&2
    exit 1
}
sed 's/^/    /' "$WORK/refusal.txt"
echo "  -> refused as expected, and nothing was fetched"

banner "done — every stage succeeded; workspace ($WORK) will be cleaned up"
