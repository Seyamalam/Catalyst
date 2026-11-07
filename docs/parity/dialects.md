# Dialect parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| SQLite positional text execution | partial | `tests/test_sqlite_text.py` | A foundation helper uses rusqlite; no dialect compiler or engine/pool API exists. |
| PostgreSQL dialect and execution | not started | — | Driver selected in [ADR 0003](../decisions/0003-postgresql-driver.md); no driver code or SQLAlchemy parity test exists. |
| MySQL dialect and execution | not started | — | Driver selected in [ADR 0004](../decisions/0004-mysql-driver.md); no driver code or SQLAlchemy parity test exists. |
| Docker integration harness: plaintext service protocol probes | partial | `tests/test_integration_infra.py::test_postgres_plaintext_startup_response`; `tests/test_integration_infra.py::test_mysql_plaintext_protocol_greeting` | Harness-infrastructure coverage only: checks TCP and initial protocol framing with TLS disabled. It does not exercise Catalyst drivers, SQL statements, TLS, successful authentication, or SQLAlchemy parity. |
