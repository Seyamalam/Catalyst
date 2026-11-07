# Core parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| Positional bound parameters for SQLite text statements | partial | `tests/test_sqlite_text.py` | Foundation-only `sqlite_text` helper compares scalar result rows and representative errors with SQLAlchemy; named binds, persistence lifecycle guarantees, and public `text()`/engine API remain unsupported. |
| Core `select` / `insert` / `update` / `delete` expressions | not started | — | Expression AST not implemented. |
| Transactions and savepoints | not started | — | Engine and transaction API not implemented. |
| SQLAlchemy-style exception names and supported DBAPI subclass hierarchy | partial | `tests/test_errors.py` | Supported exception classes are compared with SQLAlchemy; constructors, attributes, and the full exception set are not covered. `CatalystError` is an alias of `SQLAlchemyError`. |
