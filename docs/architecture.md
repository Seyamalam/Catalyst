# Architecture

Catalyst is an early-stage, Rust-first Python database toolkit. This document describes the intended architecture; items marked **planned** are not implemented and do not establish parity.

## Crate dependency graph (planned)

```mermaid
graph TD
    Py[Python API and stubs] --> FFI[PyO3 extension]
    FFI --> Core[Rust core: AST, execution, hydration]
    Core --> Drivers[Native database drivers]
    Core --> Cache[Identity / prepared-plan caches]
```

The current Rust workspace contains one extension crate; the diagram is the target decomposition, not a claim that these modules exist.

## Query lifecycle (planned)

```mermaid
sequenceDiagram
    participant User as Python caller
    participant API as Thin Python API
    participant Rust as Rust/PyO3 core
    participant DB as Native driver / database
    User->>API: build query or text + parameter batch
    API->>Rust: one call with complete query and parameters
    Rust->>Rust: compile and acquire pooled connection
    Rust->>DB: execute without holding GIL
    DB-->>Rust: result batch
    Rust->>Rust: hydrate rows in bulk
    Rust-->>API: result batch
    API-->>User: typed result
```

## FFI and ownership

The Python/Rust boundary is for query and parameter batches, never per row or column. Blocking work must release the GIL using PyO3 `Python::detach`; async work uses Tokio and `pyo3-async-runtimes`. The SQLite implementation is planned to isolate synchronous driver calls from Python execution. There is no intentional unsafe code; unsafe remains denied.

Python-side runtime logic is limited to the declarative/user-facing shell and URL/configuration parsing where Python APIs are the compatibility surface. Document any additional Python runtime logic with its reason here.

## Session and Unit of Work (planned)

```mermaid
flowchart LR
    S[Session] --> I[Identity map]
    S --> U[Unit of Work]
    U --> D[Change set]
    D --> C[Compiled statements]
    C --> T[Transaction]
    T --> DB[(Database)]
```

## Connection pool states (planned)

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> CheckedOut: acquire
    CheckedOut --> Idle: return
    CheckedOut --> Closed: dispose / failure
    Idle --> Closed: dispose
```

## Current implementation

The native extension defines a SQLAlchemy-style exception hierarchy, a foundation `sqlite_text` helper, and a `NativeEngine`/`NativeConnection`/`NativeResult` set. `NativeEngine` owns a bounded synchronous pool implemented with standard-library mutex/condition-variable primitives. File-backed engines lazily open up to five distinct rusqlite connections; returned checkouts are reused, and a sixth simultaneous checkout raises the mapped `TimeoutError` (immediate exhaustion rather than a timed wait). `:memory:` engines cap the pool at one connection to preserve SQLite's default shared engine database semantics. Blocking checkout, return, disposal, and driver calls run with the GIL released. `NativeResult` buffers rows and exposes `keys`, `rowcount`, and positional `fetchone`/`fetchall`. The `sqlite_text` helper continues using a process-local SQLite connection and returns rows in bulk.

Python-side runtime logic (documented per the FFI rule above): `catalyst.engine` is a user-facing shell only — `create_engine` parses `:memory:`, filesystem paths, and `sqlite:///` URLs; `Engine.connect` asks Rust for a checkout; `Connection.close` returns it, and Python adds context-manager and use-after-close guards; `CursorResult` adapts the buffered native result. No query building, explicit transaction API, async APIs, or dialect compilation exist yet.