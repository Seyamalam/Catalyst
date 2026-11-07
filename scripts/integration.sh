#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for the integration harness." >&2
  exit 2
fi

# Cleanup runs once via EXIT; guard its status so teardown failures cannot
# mask the real test result.
cleanup() {
  docker compose down --remove-orphans >&2 2>&1 || :
}
trap cleanup EXIT
docker compose up -d --wait --wait-timeout 120

PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}" python -m pytest -m integration tests/test_integration_infra.py -q
