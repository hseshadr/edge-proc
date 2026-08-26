#!/bin/sh
set -eu

exec uv run python scripts/release_contract.py github --repository "$2" --sha "$1"
