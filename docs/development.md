# Local development and CI

Catalyst's checks run locally. GitHub Actions is deferred by project direction.

## Environment

Use a supported Python (3.10–3.13) and Rust toolchain. Install the project and development tools in a virtual environment:

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
```

## Local CI

Run `./scripts/ci.sh`. The script stops at the first failure and runs formatting, Clippy, Rust tests, release extension build, Python lint/type/stub checks, pytest with coverage, strict documentation build, and Rust coverage when the relevant tools are installed. Install development dependencies first so tool checks do not skip. The script reports missing optional check tools; do not report skipped checks as passing.

## Database services

`docker compose up -d` starts PostgreSQL 16.6 and MySQL 8.4.3. Integration tests for these services are not yet present; starting the services does not establish dialect parity. SQLite tests run in process once the driver is implemented.
