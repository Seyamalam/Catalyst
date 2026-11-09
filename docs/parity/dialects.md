# Dialect parity matrix

| SQLAlchemy feature | Status | Proving test | Notes |
| --- | --- | --- | --- |
| SQLite positional text execution | partial | `tests/test_sqlite_text.py` | A foundation helper uses rusqlite; no dialect compiler or engine/pool API exists. |
| PostgreSQL async text execution | partial | `tests/test_postgres.py::test_postgres_named_text_select_dml_and_transactions` | Tokio-native single connection; named `:bind` parameters, buffered primitive SELECT results, DML rowcount, and basic transactions. No pool, SQLAlchemy dialect/compiler, ORM, savepoints, executemany, streaming, or broad type support. Real test requires `CATALYST_POSTGRES_URL`; text exceptions redact driver messages. TLS verification is default; local plaintext requires explicit `sslmode=disable`. |
| MySQL dialect and execution | not started | — | Driver selected in [ADR 0004](../decisions/0004-mysql-driver.md); no driver code or SQLAlchemy parity test exists. |
| Docker integration harness: plaintext service protocol probes | partial | `tests/test_integration_infra.py::test_postgres_plaintext_startup_response`; `tests/test_integration_infra.py::test_mysql_plaintext_protocol_greeting` | Harness-infrastructure coverage only: checks TCP and initial protocol framing with TLS disabled. Real PostgreSQL SQL execution is proven separately by `tests/test_postgres.py`; no PostgreSQL TLS-enabled server or SQLAlchemy cross-driver parity test is included. |
