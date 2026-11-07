# ADR 0003: PostgreSQL driver selection

- **Status:** Accepted for a future PostgreSQL driver milestone; implementation not started
- **Date:** 2026-11-08

## Context

Catalyst is a Rust-first PyO3 toolkit. Query compilation, pooling, wire protocols, and result hydration belong in Rust; Python/Rust crossings must be batched rather than performed per row or column. PostgreSQL I/O must integrate with Tokio, release the GIL during native work, and avoid a libpq link dependency. Keeping the deployment lightweight and reducing native-library supply-chain and ABI exposure are project goals. No PostgreSQL driver code exists yet.

## Options considered

- **`tokio-postgres` (Rust `postgres` family):** asynchronous Tokio-native PostgreSQL protocol client implemented in Rust. TLS is supplied by a connector; a rustls connector avoids a libpq dependency. The closely related `postgres` crate is synchronous and would require blocking-task isolation, so it is not the preferred async path.
- **Other Rust `postgres` family variants:** synchronous `postgres` and lower-level protocol components offer control or compatibility, but add blocking integration or require Catalyst to assemble more protocol machinery than needed.
- **SQLx PostgreSQL:** a pure-Rust async Tokio-capable option with a broader database abstraction and built-in query/prepared-statement conveniences. Its wider abstraction and compile-time query ecosystem are not needed for Catalyst's explicit dialect layer, and would overlap with Catalyst's own cache and execution design.
- **libpq-backed clients:** mature and widely deployed, but require a native C library/linking and deployment surface. That conflicts with Catalyst's lightweight, Rust-first and reduced native dependency goals.

## Decision

Select `tokio-postgres` as the planned PostgreSQL protocol client, paired with a rustls-based TLS connector. Keep Catalyst's pool, dialect compilation, error translation, and Python-facing API in Catalyst rather than exposing client APIs. Pin and verify the crate version, feature set, and TLS connector compatibility when implementation begins. This ADR selects a library; it does not implement a driver or claim SQLAlchemy parity.

## Consequences

- **TLS:** use rustls for encrypted connections without requiring system OpenSSL/libpq linkage. Certificate validation, roots, hostname verification, and user configuration must be designed and tested in the driver milestone; the integration harness currently tests plaintext protocol startup only.
- **Authentication:** implement and test PostgreSQL authentication negotiation, including the mechanisms supported by the selected client/version. Credentials must stay out of logs; TLS and authentication behavior are not established by this decision alone.
- **Prepared statements:** Catalyst owns deterministic plan/prepared-statement caching policy. The client exposes prepared statements but their lifetime is tied to a connection; cache entries therefore need connection-scoped ownership, invalidation, and bounded capacity rather than a global cross-connection cache.
- **Milestones:** selection is recorded before driver work as required. PostgreSQL driver, dialect, pool integration, TLS/authentication tests, and SQLAlchemy parity tests remain future work (roadmap dialect/driver milestone). Existing PostgreSQL integration infrastructure is a socket/protocol harness only.
- **Trade-off:** Tokio-native async avoids blocking worker integration and Rustls avoids a C TLS dependency, but connector configuration, protocol-specific error mapping, and cache lifecycle remain Catalyst responsibilities.
