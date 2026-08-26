#!/bin/sh
set -eu

exec uv run python scripts/release_contract.py github \
  --repository "$3" --sha "$1" --tag "$2"
