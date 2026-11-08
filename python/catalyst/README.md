# `catalyst` Python package

This package exposes Catalyst's public Python API and typed declarations over the compiled Rust extension `catalyst._catalyst`. It intentionally contains no query-execution loops; native operations belong in Rust and should cross the FFI in batches.

## Current API

The current public API includes the Catalyst/SQLAlchemy-style error hierarchy, SQLite engines and connections (`create_engine`, `create_async_engine`, pooling, transactions, savepoints), Core expression construction (`select`, `insert`, `update`, `delete`, table/column metadata), native SQLite SQL compilation, and DDL execution (`MetaData.create_all`/`drop_all`, `Table.create`/`drop` with SQLAlchemy `checkfirst` semantics). Sessions and the ORM are not implemented yet. See the repository [parity matrix](../../docs/parity/index.md) before relying on a feature; each capability lists its remaining gaps.

## Development

Build the extension from the repository root with `maturin develop --release` inside the supported virtual environment. Package sources and native type declarations are in this directory; the Rust implementation is in `src/lib.rs`.