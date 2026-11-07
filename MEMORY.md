# Catalyst project memory

## Current baseline

- Work is on branch `feat/m1-foundation`; never commit directly to `main`. Commit logical passing milestones frequently using Conventional Commits. The bootstrap native package slice is committed as `96e2d1e feat(m1): bootstrap Rust extension and exception API`.
- Project instructions are in `AGENTS.md`. User requires durable agent instructions and this current-state memory.
- User explicitly deferred GitHub Actions; local CI is `scripts/ci.sh`. No workflow files should be added.
- Native extension: PyO3 0.29, abi3-py310, maturin, edition 2024. Python project targets 3.10–3.13. Native SQLAlchemy-style exception family is exposed; `CatalystError` aliases `SQLAlchemyError` for now.
- Milestone 1 engine functionality (engine factories, pool, bound `text()` execution, async APIs) remains unimplemented. PostgreSQL and MySQL compose services are pinned in `docker-compose.yml`; no database integration tests exist yet.
- SQLite driver choice (`rusqlite` bundled) and MkDocs choices are recorded in ADR 0001 and 0002. No driver code is written yet.
- Parity tests currently cover exception names and the supported DBAPI error hierarchy only; exception support is partial because constructor/attribute and full hierarchy parity are untested. All other areas are not started.

## Verified checks

- `./scripts/ci.sh`: passed Rust fmt, Clippy with warnings denied, Rust tests (zero currently), maturin release install, Ruff lint/format, strict mypy and Pyright, `mypy.stubtest`, pytest (2 tests), and strict MkDocs build.
- Python coverage reports 100% for the two-line Python package initializer; this says nothing about Rust coverage. `cargo-llvm-cov` is not installed, so Rust coverage is unmeasured. No benchmark has been run.
- Docker Compose YAML validates with `docker compose config --quiet`. Docs currently emit Material for MkDocs lifecycle warning, but strict build succeeds.

## Delegation constraint

GPT-6 Luna is the only permitted subagent model, but orchestration reports `canRunChildTask: false` for this account/session. Do not delegate with another model or bypass the restriction; continue in the lead thread and state this constraint when relevant.

Update this file as commits, decisions, checks, and milestone status change. Keep it concise and current.