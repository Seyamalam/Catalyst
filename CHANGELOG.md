# Changelog

All notable changes to Catalyst are recorded here.

## Unreleased

- Add a two-layer deterministic plan cache: Python-side LRU memoization of compiled Core statements keyed by the serialized statement, and Rust-side bounded per-connection prepared-statement caching (capacity 128) via rusqlite; measured ~4.5x faster repeated execution.

- Add native SQLite DDL compilation: SQLAlchemy-exact `CREATE TABLE`/`DROP TABLE` text in Rust (types, single/composite primary keys, NOT NULL, column-level UNIQUE, string `server_default` literals), `NativeConnection.has_table` probes matching SQLAlchemy's sqlite checkfirst pragmas, and Python `MetaData.create_all`/`drop_all` plus `Table.create`/`drop` for sync and async engine binds with checkfirst semantics and commit handling.
- Finish expression-compiler divergences: SQLAlchemy `is_precedent`/natural-self-precedent parenthesization for arithmetic and boolean groups, the SQLite `left / (right + 0.0)` true-division rewrite, `anon_N` labels for unlabeled non-column select items, empty `select()`, dropped empty `and_()`/`or_()` with SQLAlchemy's deprecation message (as `DeprecationWarning`), and `NOT` grouping for arithmetic/nested-negation shapes; `not_` now also reverses `not_in`/`not_like_escape`/`not_between`.
- Map SQLite prepare-time driver errors (`SqlInputError`) to `OperationalError` (constraint violations to `IntegrityError`), matching SQLAlchemy's sqlite DBAPI classification for errors such as "table already exists".
- Add a Rust-backed SQLite Core expression AST/compiler with SQLAlchemy-shaped table/column metadata, select/insert/update/delete construction, per-column scalar/expanding bind names, operator-aware negation, scalar `IS`, escaped `LIKE`, duplicate-column labels, empty `IN` handling, and a native expression-depth guard; document partial parity and unsupported AST features.
- Add a Tokio-backed SQLite async engine skeleton (`create_async_engine`, async connection execution/transactions, context managers, and disposal). Async results reuse the natively buffered `CursorResult`; no async streaming or full SQLAlchemy async parity is claimed.
- Store transaction/savepoint bookkeeping on physical pool slots, aligning serialized SQLite `:memory:` checkout interleaving and shared commit/rollback behavior with SQLAlchemy; retain independent file connections and add async checkout exhaustion coverage.

- Bootstrap the Rust workspace and Python package metadata for PyO3/maturin with the Python 3.10 stable ABI.
- Add a tested SQLAlchemy-style native exception hierarchy, Python API, and stubs.
- Add a foundation rusqlite-backed positional `sqlite_text` helper with bulk result conversion and parity tests.
- Record Rust-first architecture, driver/docs decisions, parity status, and local CI instructions.
- Remove the generated GitHub Actions workflow per project direction; CI is local only.
- Add a synchronous SQLite engine facade: Rust `NativeEngine`/`NativeResult` (buffered results, GIL-released execution) behind Python `create_engine`, `Engine`, `Connection`, and `CursorResult` with context managers, dispose lifecycle, use-after-close guards, and parity tests.
- Replace the engine's shared SQLite handle with a Rust bounded pool: distinct reusable file connections, a singleton connection for `:memory:`, and SQLAlchemy-style `TimeoutError` on immediate pool exhaustion; add pool unit and SQLAlchemy parity tests.
- Add Python `text()`/`TextClause` and Rust-native SQLite named binds (`:name`, `@name`, `$name`), escaped literal colons, positional execution compatibility, and result/error parity tests.
- Add Rust-native SQLite commit-as-you-go transactions and arbitrary nested savepoints, Python root/savepoint transaction shells and `Engine.begin()`, rollback-on-close, and SQLAlchemy parity tests.
- Add SQLAlchemy-style SQLite URL and engine option parsing, dialect error registry, configurable pool size/overflow and condition-variable checkout timeout, SQL echo logging, and parity tests.
- Select planned PostgreSQL (`tokio-postgres`) and MySQL (`mysql_async`) clients in ADRs 0003/0004 and add a Docker-backed plaintext protocol harness; no non-SQLite driver code or parity is implemented.
- Add opt-in Rust coverage through `cargo-llvm-cov` and Rust-only Criterion benchmarks for pool checkout, buffered SQLite queries, named binds, and transaction cycles; both are optional local CI gates.
