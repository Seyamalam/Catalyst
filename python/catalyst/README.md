# `catalyst` Python package

This package exposes Catalyst's public Python API and typed declarations over the compiled Rust extension `catalyst._catalyst`. It intentionally contains no query-execution loops; native operations belong in Rust and should cross the FFI in batches.

## Current API

The initial public API is the Catalyst/SQLAlchemy-style error hierarchy. Engine, SQL expression, ORM, and session APIs are not implemented yet. See the repository [parity matrix](../../docs/parity/index.md) before relying on a feature.

## Development

Build the extension from the repository root with `maturin develop --release` inside the supported virtual environment. Package sources and native type declarations are in this directory; the Rust implementation is in `src/lib.rs`.