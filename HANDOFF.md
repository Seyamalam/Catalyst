# HANDOFF — Catalyst continuation brief

You are picking up work on **Catalyst**: a Rust-backed, typed ORM and SQL toolkit
targeting **SQLAlchemy 2.0 compatibility** (`/Users/seyam/Work/Catalyst`).

Read `AGENTS.md` and `MEMORY.md` first — they are the standing rules and running log.
This file captures current state, the active task, and everything learned the hard way.

## Mission

Finish the project. Rust owns query ASTs, dialect compilation, pooling, wire
protocols, execution, identity maps, unit-of-work tracking, and bulk hydration
(PyO3/maturin). Python owns declarative APIs, metaclasses/decorators, config,
and typing. Roadmap:

1. Foundation/Core engine — **done**
2. Expression AST + dialects (SQLite/PostgreSQL/MySQL) — **done**
3. Declarative ORM + PEP 681 typing — **done** (merged `e80c02a`)
4. Sessions, identity map, UOW, relationships/loading — **in progress**
   (write path `d82c784` + relationships `dc6fb0e` done; merge/expunge/bulk
   suite merged `024bb89`, impl in flight; inheritance, many-to-many pending)
5. Parity matrices + benchmarks — **pending**

Current work: Milestone 4 — write path (`d82c784`) + relationships/loading
(`dc6fb0e`) landed. M4 slice 3 in flight on `swe-2-high`: `merge`, `expunge`/
`expunge_all`, `bulk_*` ops, ORM DML sync, session reuse-after-`close()` fix,
`get(populate_existing)`. Then: inheritance, `MappedAsDataclass`, session
savepoints, many-to-many/secondary.

## Repo state

- Branch: `fix/parity-gaps` — parity gaps + M4 slices 1-2 landed and pushed,
  lifecycle suite `024bb89` merged. Suites: `test_session_write.py` (28) +
  `test_relationships.py` (15) green, `test_session_lifecycle.py` (33, skip-
  gated); ground truths in `docs/*-ground-truth.md`.
- Working tree: clean except **untracked `uv.lock` and this file** — do NOT
  commit `uv.lock` (generated, never tracked).
- `main` is pushed and current through `e80c02a` + memory commit.
- Parity-gap plan (three atomic commits on this branch) — **complete**:
  1. `StatementError` `.orig`/`.statement`/`.params` — done (`3d122fc`)
  2. MySQL executemany — done (`9a87047`, asyncmy-verified)
  3. Numeric/Decimal — done (`7183b85`, see below)

## Just landed: MySQL executemany (verified vs SQLAlchemy 2.1.8 + asyncmy 0.2.18)

- `NativeMysqlConnection.async_executemany` in `src/mysql.rs`: all rows are
  extracted/coerced in the sync PyO3 section (one FFI crossing), then the batch
  runs in one transaction. `split_insert_values` (hand-rolled, quote/comment
  aware) collapses `INSERT|REPLACE ... VALUES (?)` into a single multi-row
  statement — **atomic**, matching asyncmy's `RE_INSERT_VALUES` rewrite
  (greedy `.+` ⇒ candidates tried right-to-left; `\bVALUES?\s+` ⇒ whitespace
  required before the `(` group; suffix only `AS …`/`ON DUPLICATE …`/`;`).
  Every other statement executes once per parameter group, discarding result
  sets, summing `rowcount`.
- **SELECT executemany is NOT rejected** — asyncmy runs it per group
  (`result.all() == []`, rowcount = summed result rows). PyMySQL's
  `ProgrammingError` is a driver difference, not SA behavior — do not "fix" it
  back. Same for compiled `select` batches.
- `extract_parameters` now takes `bind_names` and orders non-text dict rows by
  it; empty list + `text_mode` binds once against an empty mapping (missing
  named bind → `StatementError`, no `parameter group` suffix; suffix `, in
  parameter group N` appears only for rows ≥ 1 — `extract_dict_values`).
- `python/catalyst/engine.py`: batch-shaped `native_parameters` on MySQL routes
  to `async_executemany`; MySQL compilation is **deferred to the shared compile
  site** (`compile_dialect` local) so `Insert` batch preprocessing keeps
  first-row shape + missing-key `StatementError`s — do not re-add an early
  `compile("mysql")`.
- Proving suite: `tests/test_mysql_executemany.py` (12 live tests) +
  `test_mysql.py::test_mysql_select_executemany_runs_per_group_like_sqlalchemy`.
- Known residual gap (documented in tests + parity row): bad bind values raise
  `StatementError` wrapping `TypeError` client-side; SA+asyncmy surfaces
  `DataError` (errno 1366). Fine to leave; note it if Numeric/Decimal work
  touches bind coercion.

## Just landed: Numeric/Decimal (`7183b85`, 31 live tests in `tests/test_numeric.py`)

- `Numeric(precision, scale, decimal_return_scale, asdecimal)` marker in
  `expression.py`; options ride in the type tag as
  `numeric:p:s:rs:ad` (`-` = unset) through compiled bind/result metadata
  (`_column_type_tag`) and `TypeArgs::Numeric` for DDL.
- DDL `NUMERIC`/`NUMERIC(p)`/`NUMERIC(p, s)` **identical on all three
  dialects** — MySQL aliases NUMERIC→DECIMAL server-side (unbounded becomes
  `decimal(10,0)`, lossy); PG unbounded stays `numeric` (exact).
- **SQLite**: numeric binds run `float(value)` (NaN→REAL nan→stored NULL;
  ±Infinity→REAL extremes); results format `Decimal("{:.eff}")` where
  `eff` = `decimal_return_scale` → `scale` → **10**; `inf`→`Decimal('Infinity')`
  falls out of float formatting. `asdecimal=False` returns raw floats.
  Precision/scale NOT enforced. Untyped `text()` Decimal binds still raise
  `ProgrammingError` (conversion is type-scoped).
- **PostgreSQL**: hand-rolled binary NUMERIC codec in `src/postgres.rs`
  (base-10000 digits; sign codes 0xC000/0xD000/0xF000 for NaN/±Inf);
  `Decimal`/`str`/`int`/`float` binds → `encode_numeric`; results decode to
  `Decimal` (dscale-padded). **New `async_executemany`**: one prepare +
  per-group execute, aggregate rowcount (psycopg reports `-1` — catalyst
  reports the true count; documented driver gap).
- **MySQL**: native `Decimal` results; Decimal binds as text bytes; `text()`
  finite Decimals inline as SQL literals (asyncmy has no Decimal param type —
  non-finite inline as bare identifiers → server 1054 → `OperationalError`).
- **New `DataError(DatabaseError)`**: PG SQLSTATE 22-class + MySQL 1264 →
  `DataError`; MySQL 1054 → `OperationalError`.
- `CursorResult` gained `first/one/one_or_none/scalar/scalar_one/
  scalar_one_or_none`; `Mapped[Decimal]`→`Numeric` inference.
- **Suite-writing lessons**: (a) dual-engine live tests need **per-engine
  table names** or explicit-PK inserts collide on the shared table;
  (b) `id=0` into MySQL AUTO_INCREMENT auto-assigns 1 → collides with
  explicit id=1 — verified failing for pure SA+asyncmy, ids must start at 1;
  (c) psycopg `executemany` rowcount is `-1` — assert catalyst's true count
  and document the driver gap.

## Build & test environment (read — non-obvious)

- **maturin** is at `/Users/seyam/.local/bin/maturin` — `.venv/bin/maturin` does
  NOT exist. Rebuild after every Rust edit:
  `/Users/seyam/.local/bin/maturin develop` (from repo root, venv active).
- **Stale `.so` trap**: if Rust src is newer than the built extension, tests run
  old code silently. Rebuild before trusting any runtime result.
- Venv python: `.venv/bin/python`; deps via `uv` (`uv sync`, `uv add --dev`).
- Live DBs (docker, usually running): `catalyst-postgres-1`, `catalyst-mysql-1`.
  - `CATALYST_POSTGRES_URL=postgresql://catalyst:catalyst@127.0.0.1:5432/catalyst?sslmode=disable`
  - `CATALYST_MYSQL_URL=mysql://catalyst:catalyst@127.0.0.1:3306/catalyst?sslmode=disable`
  - `sslmode=disable` is REQUIRED (local containers are plaintext; drivers default
    to verified TLS). Strip the query string when building a SQLAlchemy/asyncmy URL.
- SA parity driver for MySQL is **asyncmy** (already a dev dep). aiomysql is
  broken with PyMySQL 1.2.4 on this Python — do not use it.
- Gates (all must pass before committing):
  - `cargo test`, `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`
  - `ruff check`, `ruff format --check`
  - `mypy --strict python/catalyst` — **only this path is gated**; running strict
    mypy over `tests/` shows many pre-existing errors (not your problem).
  - `pyright python/catalyst`, `mypy.stubtest catalyst` (currently clean)
  - `pytest` (~330 tests; live runs need the two `CATALYST_*_URL` env vars)
  - `mkdocs build --strict`
  - `scripts/ci.sh` = authoritative full gate (builds **--release**; debug builds
    behave differently — e.g. a stack-overflow bug only reproduced in debug).
- Compile stack safety: `compile_expression` runs on a dedicated 64 MiB-stack
  thread; keep it that way (2000-deep guard must fire before stack exhaustion).

## Conventions & landmines

- Atomic Conventional Commits on feature/fix branches; never commit to `main`
  directly; merge commits to `main` only after full local CI.
- Every parity claim needs a **passing proving test** vs real SQLAlchemy — never
  infer parity from implementation. `docs/parity/` rows: feature, status,
  proving test, difference notes. If a parity driver differs from another
  driver (PyMySQL vs asyncmy SELECT-executemany), the chosen driver wins.
- **Always ground-truth SA behavior live before encoding expectations** — this
  session's first executemany pass implemented PyMySQL semantics from memory
  and had to be revised after asyncmy probing contradicted them.
- Delegation (updated 2026-11-10, supersedes AGENTS.md's GPT-6 Luna note):
  workers run via `delegate_task` with `isolation:"worktree"`; swe-2-flash for
  lighter tasks (ground truth, tests, docs), swe-2-high for heavier ones
  (Rust implementation); lead thread reviews independently and integrates.
- SA ground truth already verified:
  - `StatementError` message: `({module}.{Exc}) {orig}\n[SQL: {sql}]` (+ `[parameters: {repr}]`)
  - bind-processor failures → `StatementError` (orig `TypeError`); unbindable
    types → `ProgrammingError` (a `StatementError` subclass).
  - executemany missing-bind: `A value is required for bind parameter 'x'` —
    `, in parameter group N` appended only for group N ≥ 1 (0-based).
  - SA+asyncmy `insert(t)` + `[]` executes once with defaults (rowcount 1);
    `text()` + `[]` → missing-bind `StatementError`.
  - SA `AsyncConnection.begin_nested()` is awaitable — `AsyncSavepointTransaction`
    has `__await__` for parity.
  - `conn.execute(select(Entity))` returns raw tuples; hydration lives in
    `Session.scalars` (full UOW deferred to Milestone 4).
  - SA `Mapped[float]` → `DOUBLE` DDL (distinct `Double` marker added), `float`
    literals, `Mapped[str|None]` → nullable VARCHAR.
  - `Mapped[Decimal]` → `Numeric`; `decimal_return_scale`/`asdecimal` kwargs.
- **`__replace__` stub gotcha**: Python 3.13 dataclasses synthesize
  `__replace__` at runtime; 3.12 does not. `.pyi` stubs must match the LOCAL
  runtime (this env: 3.12) — a worker on 3.13 added stub `__replace__` lines
  that broke this env's stubtest; reverted. If stubtest shows
  `__replace__ is not present` errors, check the Python version first.
- `typing_extensions` is a declared dep (needed for `dataclass_transform` on
  Python 3.10 targets).
- Sync `SavepointTransaction.__exit__` previously double-finished on manual
  rollback — a `_finished` guard now exists on both sync/async versions.
- New database-driver work needs an ADR under `docs/decisions/` (Numeric type
  work likely doesn't need one — it extends existing types/drivers).

## Just landed: M4 slice 1 — Session write path (`d82c784`)

- **`NativeUow`** (`src/session.rs`, `#[pyclass]`): identity map keyed
  `(class_ptr, pk_hash)` → `==`-compared collision buckets; per-object state
  keyed by `id(obj)` (status, set-on-write `dirty_attrs`, `expired_attrs`,
  stored `pk`, `new_in_txn`, `seq` for add order); batch transitions
  `flush_transition`/`expire_all`/`rollback`; `reconcile` merges hydrated
  rows with the identity map in one FFI crossing.
- **`Session`** (`orm.py`): `add`/`add_all`/`delete`/`flush`/`commit`/
  `rollback`/`get`/`refresh`/`expire`/`expire_all`/`begin`/`begin_nested`/
  `in_transaction`, `autoflush` (default True), `expire_on_commit` (default
  True). Flush compiles per-object ops → ONE `NativeConnection.execute_flush`
  FFI call → `lastrowid` PK write-back. `new`/`dirty`/`deleted` return
  `_StateSet` (identity-keyed snapshots).
- **Lazy reload**: `_expire_instance` drops `__dict__` entries; `Column`
  gained `key` + non-data `__get__` → `_catalyst_state` (NativeUow) →
  `session._load_expired` → whole-row SELECT repopulates ALL expired attrs.
- **Exceptions**: `PendingRollbackError`/`UnmappedInstanceError`
  (InvalidRequestError subclasses) — in `src/lib.rs` + exports.
- **`__setattr__` set-on-write**: `DeclarativeBase.__setattr__` notifies
  `uow.attr_set` — revert-to-original stays dirty.
- Lessons: worker died on rate limit mid-slice → captured uncommitted WIP as
  `da4a2db` and relaunched a continuation (kept ~90%). Defect fixed at
  integration: expired access re-SELECTs the WHOLE row at first touch — an
  external UPDATE must land before that access to be visible.

## Next up

1. **Milestone 4 remaining**: session-level savepoints beyond `begin_nested`
   delegation, `merge`/`expunge`/`expunge_all`, bulk ops
   (`bulk_save_objects`/`bulk_insert_mappings`), ORM update/delete
   synchronization, **relationships + cascades + lazy/eager loading**
   (biggest remaining chunk), inheritance, `MappedAsDataclass`, registry,
   `populate_existing`/loader options, `identity_key`, SessionEvents.
2. **Milestone 5**: finish parity matrices, publish measured benchmarks.

## Key files

- `src/lib.rs` — SQLite PyO3 engine, exceptions, `wrap_statement_error`, batch exec
- `src/expr.rs` — expression compiler + dialects (64 MiB compile thread)
- `src/mysql.rs` — MySQL driver (`async_execute`, `async_executemany`,
  `extract_parameters`, `split_insert_values`, `execute_buffered`,
  `coerce_mysql_value`, `scan_named_binds`)
- `src/postgres.rs` — PG driver (reference impl for typed async execution)
- `python/catalyst/engine.py` — sync/async engine shells, param dispatch,
  executemany preprocessing, `_statement_error`
- `python/catalyst/expression.py` — Core AST shells, `Compiled` (has `bind_error`),
  compile LRU
- `python/catalyst/orm.py` + `.pyi` — declarative base, `Mapped`, `mapped_column`
- `python/catalyst/_catalyst.pyi`, `engine.pyi`, `expression.pyi` — stubs (keep
  in sync; verified by stubtest)
- `tests/test_mysql.py`, `test_mysql_core.py`, `test_mysql_executemany.py`,
  `test_executemany.py`, `test_statement_error.py` — relevant suites
- `docs/parity/{core,dialects,types,orm,session,async}.md` — parity truth
- `docs/architecture.md`, `CHANGELOG.md`, `MEMORY.md` — keep aligned
- Prior conversation summaries with full history:
  `/Users/seyam/.local/share/devin/cli/summaries/` (latest:
  `history_9274328e3d7b4cfc.md`)
