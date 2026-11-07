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