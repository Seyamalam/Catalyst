# ADR 0001: Database driver selection

- **Status:** Accepted for the foundation milestone
- **Date:** 2026-11-07

## Context

Catalyst needs native database drivers that integrate with Tokio without putting per-row or per-column work across the Python FFI boundary. Milestone 1 needs a runnable local test database, while the roadmap also requires PostgreSQL and MySQL integration.

## Decision

Use `rusqlite` with its `bundled` SQLite feature for the first foundation driver. Defer PostgreSQL and MySQL driver selection until their dialect and async integration slices are scheduled; do not add driver code until separate ADRs record one selected driver per database.

## Rationale

SQLite is in-process, reproducible, and suitable for validating parameter binding and basic engine behavior without a service dependency. `rusqlite` is synchronous; Tokio integration must isolate its work (for example, a blocking task) and must never hold the Python GIL during database execution. This ADR does not claim nonblocking SQLite I/O or production pool readiness.

## Consequences

- SQLite is the first engine integration target and runs without Docker.
- PostgreSQL/MySQL integration remains unimplemented until their drivers are selected and pinned test services are available.
- Revisit this choice if async SQLite throughput or connection-pool requirements make another driver preferable.