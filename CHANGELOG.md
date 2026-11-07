# Changelog

All notable changes to Catalyst are recorded here.

## Unreleased

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
