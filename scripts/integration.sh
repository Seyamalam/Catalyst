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

# Use the project virtualenv explicitly so the script works outside an
# activated shell (ci.sh requires VIRTUAL_ENV; this script must not).
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "Project virtualenv missing at .venv; create it first." >&2
  exit 2
fi
export CATALYST_POSTGRES_URL="${CATALYST_POSTGRES_URL:-postgresql://catalyst:catalyst@127.0.0.1:5432/catalyst?sslmode=disable}"
export CATALYST_MYSQL_URL="${CATALYST_MYSQL_URL:-mysql://catalyst:catalyst@127.0.0.1:3306/catalyst?sslmode=disable}"
PYTHONPATH="$ROOT/python${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m pytest -m integration tests/ -q
