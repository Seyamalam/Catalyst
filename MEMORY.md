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
- PostgreSQL compiled-statement execution slice (in review on `feat/pg-compiled-execution`): `AsyncConnection.execute` runs compiled Core DML/SELECT and `Compiled` statements; typed `bind_types` metadata coerces compile-processed values (datetime/date strings → TIMESTAMP/DATE, boolean ints, binary), mapping overrides order by bind name, `has_table` uses the `pg_catalog` search-path probe, and DDL helpers pick the dialect from the bind. Live-verified vs SQLAlchemy+psycopg (`psycopg[binary]` added to dev extras; SA+psycopg reports `-1` INSERT rowcounts, Catalyst reports the true count). `aiosqlite` also installed in `.venv`, which un-skipped `test_async_executemany_merges_statement_values` (fixed to use `run_sync`).

- PostgreSQL connection pool (merged at `62c18ca`): semaphore-bounded async pool with `pool_size`/`max_overflow`/`pool_timeout`, idle reuse, `TimeoutError` checkout exhaustion in SQLAlchemy's QueuePool message shape, rollback reset-on-return, dead-client skipping, dispose draining. Live-verified in `tests/test_postgres_pool.py` including reuse via `pg_backend_pid()`. Queue item 1 is now fully implemented.
- MySQL first driver slice (merged at `58958c3`, pushed): `NativeMysqlEngine`/`NativeMysqlConnection` over `mysql_async` 0.37.1 per ADR 0004 — `mysql://`/`mysql+driver://` URL routing, same semaphore pool semantics as PG (`CONNECTION_ID()` reuse, timed checkout `TimeoutError`, reset-on-return rollback via `async_close`), named `:bind` → `?` scanning, autobegin/commit/rollback transactions, `has_table` via `information_schema.TABLES`, redacted error mapping (1062 → `IntegrityError`), rustls TLS default with `sslmode=disable` for plaintext. Execution prefers the prepared binary protocol (`exec`) so int/float/text/date/datetime(6)/blob/decimal hydrate typed — row-identical to SQLAlchemy+asyncmy; unpreparable statements (`ER_UNSUPPORTED_PS`, e.g. `SHOW`) fall back to text protocol with column-type decoding. Compiled Core statements and executemany raise `ArgumentError` (no MySQL dialect compiler yet); `TIME` values unsupported. `asyncmy` + `aiosqlite` added to dev extras (aiomysql 0.3 breaks on PyMySQL 1.2). Live suite `tests/test_mysql.py` (13 tests).

## In progress

- Async savepoints merged at `aa417ce` (pushed): `AsyncSavepointTransaction` on all three drivers + aiosqlite parity tests; fixed latent `_finished`-guard bug in sync `SavepointTransaction.__exit__`. Next: MySQL dialect compilation + compiled Core execution (mirrors the PG compiled slice).

## Remaining queue (priority order, user-approved 2026-11-09)

1. ~~PostgreSQL pool + compiled-statement execution~~ (done: compiled execution merged at `8b1f815`, pool merged at `62c18ca`).
2. ~~MySQL first driver slice per ADR 0004 (`mysql_async`)~~ (done: merged at `58958c3`, 14 live tests).
3. ~~Async savepoints + `aiosqlite` real async parity~~ (done: merged `aa417ce`). Next: MySQL dialect compilation + compiled Core execution (same slice shape as PG compiled execution), then MySQL DDL (`create_all`/`has_table` already half-wired via `_bind_dialect`).
4. Milestone 3: declarative ORM (`Mapped[T]`, mappings, PEP 681, stubs, batched hydration).
5. Close documented parity gaps: Numeric/Decimal, StatementError wrapping, `.orig`/`.params` (string `+` → `||` closed in the dialect slice).
6. Milestone 4: Session, identity map, unit of work, relationships/loading; then milestone 5 parity/benchmarks.

## Environment facts

- Native extension: PyO3 0.29, abi3-py310, maturin, edition 2024. Python targets 3.10–3.13; `.venv` is uv-managed (use `uv sync --extra dev`; `uv pip` installs get wiped on sync — declare deps in pyproject). Dev extras include SQLAlchemy 2.1.8, `psycopg[binary]`, `asyncmy`, and `aiosqlite`.
- Colima provides the Docker daemon (`colima start` if down); compose services pinned: postgres:16.6 (5432, catalyst/catalyst), mysql:8.4.3 (3306). `scripts/integration.sh` health-gates and tears down services; default pytest skips live tests when URLs/TCP are unavailable.
- Benchmarks (local baselines): pool checkout 28.7ns; buffered SELECT 10/100/1000 rows 4.1/5.9/23.5µs; named binds 331ns; txn cycle 198ns; plan cache ~4.5x repeated execution. Rust coverage baseline 29% lines (pool.rs 88%); PyO3 entry points are exercised via pytest, not Rust unit tests.
- Known stale-doc risk: parity matrices are the source of truth; update them with every slice. `docs/parity/session.md` still says transactions not started (Core transactions exist; the row refers to Session-level behavior).

Update this file whenever commits, decisions, checks, or milestone status change.