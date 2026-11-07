# ADR 0004: MySQL driver selection

- **Status:** Accepted for a future MySQL driver milestone; implementation not started
- **Date:** 2026-11-08

## Context

Catalyst is a Rust-first PyO3 toolkit. Query compilation, pooling, wire protocols, and result hydration belong in Rust; Python/Rust crossings must be batched rather than performed per row or column. MySQL I/O must integrate with Tokio and release the GIL during native work. Avoiding libmysqlclient/connector-c linkage supports lightweight deployment and reduces native-library ABI and supply-chain exposure. No MySQL driver code exists yet.

## Options considered

- **`mysql_async`:** a Rust MySQL protocol implementation designed for async Tokio applications. It provides the most direct fit for the planned async engine and does not require a MySQL C client library.
- **`mysql` (synchronous Rust crate):** a pure-Rust protocol client with synchronous APIs. It would require isolating blocking calls in Tokio blocking workers and adds friction for the planned async interface.
- **SQLx MySQL:** a pure-Rust async Tokio-capable client with a broad multi-database abstraction and prepared-statement support. Its broader abstraction overlaps Catalyst's own dialect, execution, and cache layers; choosing a focused MySQL protocol client keeps this integration narrower.
- **libmysqlclient/connector-c-backed clients:** mature native clients, but introduce system package/linking and deployment requirements that conflict with Catalyst's lightweight Rust-first goals.

## Decision

Select `mysql_async` as the planned MySQL protocol client, with rustls-based TLS support. Keep Catalyst's pool, dialect compilation, error translation, and Python-facing API in Catalyst. Pin and verify the crate version, feature set, and TLS behavior when implementation begins. This decision records library selection only; no MySQL driver code exists and no SQLAlchemy parity is claimed.

## Consequences

- **TLS:** prefer rustls to avoid requiring OpenSSL or connector-c TLS linkage. Driver implementation must configure certificate roots, hostname verification, and secure defaults explicitly. The current harness exercises only a plaintext handshake.
- **Authentication:** verify the server authentication plugin(s) and credential handling supported by the selected crate/version against the pinned MySQL service and intended deployment matrix. Never log credentials. This ADR does not assert authentication parity.
- **Prepared statements:** Catalyst owns cache policy. Server prepared statements are session/connection scoped, so any cache must be bounded and attached to an individual checked-out connection, with correct cleanup on reset, reconnect, and pool return.
- **Milestones:** driver/dialect implementation, pool integration, TLS and authentication coverage, error mapping, and SQLAlchemy parity tests remain future work in the dialect/driver milestone. The current MySQL integration harness only verifies a TCP connection and initial protocol greeting.
- **Trade-off:** `mysql_async` aligns with Tokio and avoids a native C client dependency; Catalyst still owns SQLAlchemy-compatible semantics, connection lifecycle, cache invalidation, and proof of behavior.
