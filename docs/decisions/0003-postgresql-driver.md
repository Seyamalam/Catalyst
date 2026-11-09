# ADR 0003: PostgreSQL driver selection

- **Status:** Accepted; initial async text-execution slice implemented
- **Date:** 2026-11-08

## Context

Catalyst is a Rust-first PyO3 toolkit. Query compilation, pooling, wire protocols, and result hydration belong in Rust; Python/Rust crossings must be batched rather than performed per row or column. PostgreSQL I/O must integrate with Tokio, release the GIL during native work, and avoid a libpq link dependency. Keeping the deployment lightweight and reducing native-library supply-chain and ABI exposure are project goals. The first implementation slice covers asynchronous PostgreSQL connections and buffered text SELECT/DML execution only; it does not include a connection pool, expression dialect, ORM, or full SQLAlchemy parity.

## Options considered

- **`tokio-postgres` (Rust `postgres` family):** asynchronous Tokio-native PostgreSQL protocol client implemented in Rust. TLS is supplied by a connector; a rustls connector avoids a libpq dependency. The closely related `postgres` crate is synchronous and would require blocking-task isolation, so it is not the preferred async path.
- **Other Rust `postgres` family variants:** synchronous `postgres` and lower-level protocol components offer control or compatibility, but add blocking integration or require Catalyst to assemble more protocol machinery than needed.
- **SQLx PostgreSQL:** a pure-Rust async Tokio-capable option with a broader database abstraction and built-in query/prepared-statement conveniences. Its wider abstraction and compile-time query ecosystem are not needed for Catalyst's explicit dialect layer, and would overlap with Catalyst's own cache and execution design.
- **libpq-backed clients:** mature and widely deployed, but require a native C library/linking and deployment surface. That conflicts with Catalyst's lightweight, Rust-first and reduced native dependency goals.

## Decision

Use `tokio-postgres` 0.7.18 with `tokio-postgres-rustls` 0.14.0, `rustls` 0.23.46 (ring provider), and `webpki-roots` 1.0.11. Catalyst owns the async API, named-bind scanner, buffered result conversion, error redaction, and subsequent pool/dialect work rather than exposing client APIs. This initial slice implements only a native async single connection and text execution; it does not claim full driver or SQLAlchemy parity.

## Consequences

- **TLS:** use rustls for encrypted connections without system OpenSSL/libpq. Connections require TLS and verify hostnames/certificates against the vendored Mozilla WebPKI root set by default, including URLs that omit `sslmode` or request a weaker non-disable mode. `sslmode=disable` is the sole explicit plaintext opt-out and is intended only for local development/testing. Root-store verification is implemented, but a live TLS server/certificate test is not yet part of the harness.
- **Authentication:** implement and test PostgreSQL authentication negotiation, including the mechanisms supported by the selected client/version. Credentials must stay out of logs; TLS and authentication behavior are not established by this decision alone.
- **Prepared statements:** Catalyst owns deterministic plan/prepared-statement caching policy. The client exposes prepared statements but their lifetime is tied to a connection; cache entries therefore need connection-scoped ownership, invalidation, and bounded capacity rather than a global cross-connection cache.
- **Milestones:** this small first implementation slice has no Catalyst PostgreSQL pool, dialect compiler, ORM, executemany, savepoints, streaming, or support for result/bind types beyond bool, integer, float, text, and bytea. The Docker PostgreSQL service is plaintext; a real SQL integration test uses explicit `sslmode=disable`. TLS handshake/certificate validation against a TLS-enabled server, broader authentication coverage, and SQLAlchemy result/exception parity remain future tests.
- **Trade-off:** Tokio-native async avoids blocking worker integration and Rustls avoids a C TLS dependency, but connector configuration, protocol-specific error mapping, and cache lifecycle remain Catalyst responsibilities.
