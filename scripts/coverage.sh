#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v cargo-llvm-cov >/dev/null 2>&1; then
  echo "cargo-llvm-cov is required; install with: cargo install cargo-llvm-cov --locked" >&2
  exit 2
fi

cargo llvm-cov --workspace --summary-only
