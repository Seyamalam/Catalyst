# Core parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| `text()` with bound parameters | not started | — | No Catalyst execution API exists yet. |
| Core `select` / `insert` / `update` / `delete` expressions | not started | — | Expression AST not implemented. |
| Transactions and savepoints | not started | — | Engine and transaction API not implemented. |
| SQLAlchemy-style exception names and supported DBAPI subclass hierarchy | partial | `tests/test_errors.py` | Supported exception classes are compared with SQLAlchemy; constructors, attributes, and the full exception set are not covered. `CatalystError` is an alias of `SQLAlchemyError`. |
