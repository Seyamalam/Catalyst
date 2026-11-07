# Catalyst agent instructions

Catalyst is a Rust-backed, typed ORM and SQL toolkit targeting SQLAlchemy 2.0 compatibility. Read this stable brief and [`MEMORY.md`](MEMORY.md) before substantial work.

## Architecture and implementation

- Keep query ASTs, dialect compilation, pooling, wire protocols, identity maps, unit-of-work tracking, and bulk hydration in Rust (`pyo3` + `maturin`). Keep Python to declarative/user-facing APIs, metaclasses/decorators, and typing stubs. Record Python-side runtime logic and its rationale in `docs/architecture.md`.
- Cross the Python–Rust boundary in batches, never per row or column. Use PyO3 `Python::detach` (0.26+) around blocking/native work, Tokio for async work, and bulk hydration into Python objects or Arrow buffers.
- Rust uses `#![deny(clippy::all, unsafe_code)]`; errors use `Result<T, CatalystError>` and map to a Python hierarchy mirroring `sqlalchemy.exc`. Isolate and document any justified unsafe FFI with `// SAFETY:` comments.
- Python targets 3.10–3.13, uses strict mypy/pyright, and has accurate `.pyi` stubs for every exposed PyO3 symbol, including PEP 681 where relevant. Verify stubs with `mypy.stubtest`.
- Preserve the SQLAlchemy 2.0 API target: engines, sessions, declarative mappings, Core expressions, relationships/loading, text execution, transactions, and savepoints. Track Arrow/Polars export, prepared statement and deterministic plan caches, and multiplexing separately.
- Before implementing a database driver, record one driver per database in an ADR under `docs/decisions/`. Before building docs, record the docs-tool choice in an ADR.

## Parity and quality

- `docs/parity/` is the source of parity coverage. Every row has a SQLAlchemy feature, status (`done`, `partial`, `not started`, or `won't support`), proving parity test, and difference notes. A feature is `done` only when a passing test demonstrates parity; otherwise state the gap and reason. Never infer parity from implementation alone.
- Compare Catalyst and SQLAlchemy results, SQL, and exceptions where applicable. Preserve attribution when adapting MIT-licensed upstream patterns. Add unit, real-database integration, typing, property, concurrency, coverage, and Criterion/end-to-end benchmarks as features require; never substitute mocks for DB integration.
- Public Rust and Python APIs need documentation and examples. Keep `docs/architecture.md`, relevant parity matrices, package/crate READMEs, and `CHANGELOG.md` aligned with each milestone.
- Keep changes scoped to the requested vertical slice. Do not claim full SQLAlchemy replacement or parity prematurely.

## Local workflow and commits

- Run all build, lint, test, docs, and benchmark checks locally. GitHub Actions is deferred by project direction; keep local CI runnable via `scripts/ci.sh`.
- Never commit directly to `main`. Use `feat/<module>-<short-desc>`, `fix/<issue>`, `perf/<target>`, `test/<suite>`, or `docs/<topic>` branches. Make frequent, atomic Conventional Commits: commit each logical, compiling milestone as soon as its focused tests pass; avoid accumulating unrelated changes into a large commit. Only the orchestrator merges after review and the applicable local CI gate pass.
- When delegated work is authorized, use only GPT-6 Luna. Each worker gets a scoped brief (goal, files, acceptance checks, docs/parity items) and an isolated worktree/branch. A reviewer must be independent from the implementer. If the exact model or isolation cannot be provided, stop delegation and report the limitation; never substitute a model or silently share a branch.
- Split work into independent vertical slices; sequence dependent tasks. Report each merge with scope, test and coverage results, and parity updates.

## Roadmap

1. **Foundation & Core Engine:** maturin/PyO3, error hierarchy, sync/async engines, Rust pool, parameterized raw `text()`, local CI, docs/parity skeleton, benchmark harness.
2. **Expression AST & dialects:** Rust expressions, PostgreSQL/SQLite/MySQL compilers, plan cache.
3. **Declarative ORM & typing:** mappings, PEP 681/stubs, batched hydration.
4. **Sessions & relationships:** identity map, dirty tracking, transactions/savepoints, eager/lazy loading.
5. **Parity & benchmarks:** port/adapt SQLAlchemy patterns, resolve or document gaps, publish measured results.
