# `catalyst` Python package

This package exposes Catalyst's public Python API and typed declarations over the compiled Rust extension `catalyst._catalyst`. It intentionally contains no query-execution loops; native operations belong in Rust and should cross the FFI in batches.

## Current API

The current public API includes a foundation-only `sqlite_text(sql, parameters)` helper for parameterized SQL against a process-local SQLite connection and the Catalyst/SQLAlchemy-style error hierarchy. Engine pooling, persistent connections, engine factories, ORM, and sessions are not implemented yet. See the repository [parity matrix](../../docs/parity/index.md) before relying on a feature.

## Development

Build the extension from the repository root with `maturin develop --release` inside the supported virtual environment. Package sources and native type declarations are in this directory; the Rust implementation is in `src/lib.rs`.