# Catalyst project memory

## Current baseline

- Work lands on `main` via reviewed feature branches; `origin/main` is the integration branch (origin HEAD still points at the historical `feat/m1-foundation`). Never accumulate unrelated changes; make logical, passing Conventional Commits frequently.
- Project instructions are in `AGENTS.md`. User requires durable agent instructions and this current-state memory.
- User direction (2026-11-09): work the priority queue continuously with frequent commits and pushes until the project roadmap is finished.
- Delegation: AGENTS.md permits only GPT-6 Luna implementers with independent review and isolated worktrees. When that exact model/isolation is unavailable, work in the lead thread and state the limitation; never substitute a model.
- User explicitly deferred GitHub Actions; local CI is `scripts/ci.sh` (optionally `CATALYST_INTEGRATION=1`, `CATALYST_COVERAGE=1`, `CATALYST_BENCH=1`). No workflow files should be added.

## Completed milestones (committed on main, pushed)

- Milestone 1 (Foundation & Core Engine): PyO3 0.29/abi3-py310/maturin, SQLAlchemy-style exception hierarchy, rusqlite engine facade, bounded Rust pool (file connections + `:memory:` singleton, timed checkout), `text()` named binds, sync transactions/savepoints per physical pool slot, SQLite URL/options/dialect registry/echo, Tokio async engine skeleton (pyo3-async-runtimes 0.29 + tokio), cargo-llvm-cov + Criterion harness, PostgreSQL/MySQL ADRs 0003/0004 + Docker integration harness.
- Milestone 2 (Expression AST & dialects): Rust Core expression AST/compiler with SQLAlchemy-exact SQL rendering (parenthesization, true division, anon labels, empty groups), SQLite DDL compilation + `create_all`/`drop_all`/`Table.create`/`drop` with checkfirst, two-layer deterministic plan/statement cache (~4.5x repeated execution), typed SQLite coercion (Integer/String/Text/Boolean/Float/Date/DateTime/binary) and one-crossing executemany with aggregate rowcounts. PostgreSQL dialect compilation (Core + DDL, `$N` binds, casts, SERIAL, reserved-word quoting) proven against SQLAlchemy in `tests/test_postgres_dialect.py`; string `+` → `||` aligned on SQLite too.
- PostgreSQL async text driver slice landed at `12ce351` (pushed): tokio-postgres + rustls, single connection, named binds, buffered results, basic transactions, redacted errors.
- Latest committed state before this slice: `12ce351`. Test suite with the dialect compiler: 239 passed, 5 skipped (live-DB gated).

## In progress

- PostgreSQL pool + compiled-statement execution (next half of the dialect task): wire `AsyncConnection.execute` for PG `Compiled` statements, PG `has_table` (information_schema probe), `create_all` dialect selection from the bind, and a connection pool; live parity tests against compose Postgres. The dialect compiler itself is complete in this slice (compilation only — PG execution is still text-only).

## Remaining queue (priority order, user-approved 2026-11-09)

1. PostgreSQL pool + compiled-statement execution with SQLAlchemy parity tests against real Postgres (dialect compiler done this slice).
2. MySQL first driver slice per ADR 0004 (`mysql_async`), same pattern as the PG slice.
3. Async savepoints; install `aiosqlite` so async parity compares against a real async SQLAlchemy driver.
4. Milestone 3: declarative ORM (`Mapped[T]`, mappings, PEP 681, stubs, batched hydration).
5. Close documented parity gaps: Numeric/Decimal, StatementError wrapping, `.orig`/`.params` (string `+` → `||` closed in the dialect slice).
6. Milestone 4: Session, identity map, unit of work, relationships/loading; then milestone 5 parity/benchmarks.

## Environment facts

- Native extension: PyO3 0.29, abi3-py310, maturin, edition 2024. Python targets 3.10–3.13; `.venv` holds dev deps (SQLAlchemy 2.1.8 comparison target; `aiosqlite` not installed).
- Colima provides the Docker daemon (`colima start` if down); compose services pinned: postgres:16.6 (5432, catalyst/catalyst), mysql:8.4.3 (3306). `scripts/integration.sh` health-gates and tears down services; default pytest skips live tests when URLs/TCP are unavailable.
- Benchmarks (local baselines): pool checkout 28.7ns; buffered SELECT 10/100/1000 rows 4.1/5.9/23.5µs; named binds 331ns; txn cycle 198ns; plan cache ~4.5x repeated execution. Rust coverage baseline 29% lines (pool.rs 88%); PyO3 entry points are exercised via pytest, not Rust unit tests.
- Known stale-doc risk: parity matrices are the source of truth; update them with every slice. `docs/parity/session.md` still says transactions not started (Core transactions exist; the row refers to Session-level behavior).

Update this file whenever commits, decisions, checks, or milestone status change.