# Catalyst project memory

## Current baseline

- Work is on `feat/m1-foundation`; never commit directly to `main`. Make logical, passing Conventional Commits frequently. Existing commits: `96e2d1e feat(m1): bootstrap Rust extension and exception API`; `8572748 chore(project): document agent workflow and local CI`; `73746f9 docs(m1): add architecture decisions and parity skeleton`; `35945a8 docs(memory): record foundation milestone commits`; `af8a717 feat(sqlite): execute bound text statements in Rust`; `4767a87 fix(sqlite): classify driver failures and clarify database lifetime`.
- Project instructions are in `AGENTS.md`. User requires durable agent instructions and this current-state memory.
- Delegation is now available: the DeepAI provider reports `canRunChildTask: true`. Per user direction (2025 session), subagents must be only `deepai/gpt-6-luna` (AGENTS-permitted implementer) or `deepai/glm-5.3-flash` (user-authorized). Reviews must stay independent from implementers. `delegate_task` children share this thread's checkout, so isolation is single-writer sequencing with clean commits between slices; report this limitation rather than running parallel writers.
- User explicitly deferred GitHub Actions; local CI is `scripts/ci.sh`. No workflow files should be added.
- Native extension: PyO3 0.29, abi3-py310, maturin, edition 2024. Python project targets 3.10–3.13. SQLAlchemy-style exception family is exposed; `CatalystError` aliases `SQLAlchemyError`.
- SQLite is selected through `rusqlite` bundled feature (ADR 0001). `sqlite_text(sql, parameters)` uses a process-local, in-memory connection, accepts scalar positional parameters, executes without holding GIL, and bulk-converts rows. A `NativeEngine`/`NativeResult` pair backs the new synchronous engine facade: `create_engine` (Python `catalyst.engine`) accepts `:memory:`, filesystem paths, and `sqlite:///` URLs; `Engine.connect()` returns logical `Connection` handles over one shared native handle (no pooling); `CursorResult` exposes `keys`, `rowcount`, `fetchone`/`fetchall`. No `text()` construct, transactions, URL options, async API, or dialect registry yet.
- PostgreSQL and MySQL compose services are pinned in `docker-compose.yml`; no integration tests exist yet.
- MkDocs is selected in ADR 0002. Parity tests cover exception names/hierarchy and basic SQLite positional execution/error cases. SQLAlchemy exception constructor/attribute/full hierarchy parity remains partial. All other API areas not started.

## Verified checks

- Latest `./scripts/ci.sh`: Rust fmt/Clippy (warnings denied)/tests, maturin release install, Ruff, strict mypy/Pyright, stubtest, and 9 pytest tests pass (exception parity, sqlite_text parity, and engine facade lifecycle/guards/fetch parity). Python package initializer shows 100% line coverage; this does not represent Rust coverage. SQLAlchemy warning in the parameter-count case was removed using a lower-level driver parity call.
- MkDocs strict build passes. Docker Compose config validates. `cargo-llvm-cov` is not installed; Rust coverage is unmeasured. No benchmark has been run.

## Delegation constraint

GPT-6 Luna is the only permitted subagent model, but orchestration reports `canRunChildTask: false` for this account/session. Do not delegate with another model or bypass the restriction; continue in the lead thread and state this constraint when relevant.

Update this file whenever commits, decisions, checks, or milestone status change.