# Catalyst

Catalyst is an early-stage Rust-backed ORM and SQL toolkit for Python, targeting SQLAlchemy 2.0 compatibility. The project is in Milestone 1; APIs and parity are not yet complete.

## Development setup

Requires Rust, Python 3.10–3.13, and (for PostgreSQL/MySQL integration tests) Docker Compose.

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python -e '.[dev]'
source .venv/bin/activate
./scripts/ci.sh
```

The native extension uses PyO3 with the Python 3.10 stable ABI and maturin. Local checks are the only CI/CD workflow at this stage; see [local development](docs/development.md).

## Architecture

Rust owns native query execution, compilation, pooling, and data-path work. Python provides the user-facing declarative and typing layer. See [`docs/architecture.md`](docs/architecture.md) and the [`architecture decisions`](docs/decisions/).

## Compatibility status

Catalyst does not yet claim SQLAlchemy parity. Each area is tracked in [`docs/parity/`](docs/parity/); a feature is done only when a passing parity test proves it.

## Contributing

Work in a feature branch, use tested Conventional Commits, and run the local CI script before requesting review. See [`AGENTS.md`](AGENTS.md) for project rules and [`MEMORY.md`](MEMORY.md) for current status.