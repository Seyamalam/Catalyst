# Dialect parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| SQLite positional text execution | partial | `tests/test_sqlite_text.py` | A foundation helper uses rusqlite; no dialect compiler or engine/pool API exists. |
| PostgreSQL dialect and execution | not started | — | Driver selection ADR pending. |
| MySQL dialect and execution | not started | — | Driver selection ADR pending. |
