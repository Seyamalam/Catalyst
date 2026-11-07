#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "Activate the project virtualenv first (source .venv/bin/activate)." >&2
  exit 2
fi

required_tool() {
  local tool="$1"
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "Required local CI tool '$tool' is missing; install development dependencies." >&2
    exit 2
  fi
}

required_tool maturin
required_tool ruff
required_tool mypy
required_tool pyright
required_tool pytest
required_tool mkdocs

cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
maturin develop --release

# Rebuild native code before parity tests so the checked-in stub matches the extension.

ruff check python/ tests/
ruff format --check python/ tests/
mypy --strict python/catalyst
pyright python/catalyst
python -m mypy.stubtest catalyst
if python -c 'import pytest_cov' >/dev/null 2>&1; then
  PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}" pytest --cov=catalyst --cov-report=term-missing tests/
else
  PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}" pytest tests/
  echo "NOTE: pytest-cov is not installed; Python coverage not measured" >&2
fi
mkdocs build --strict

if command -v cargo-llvm-cov >/dev/null 2>&1; then
  cargo llvm-cov --workspace --fail-under-lines 90
else
  echo "NOTE: cargo-llvm-cov is not installed; Rust coverage not measured" >&2
fi
