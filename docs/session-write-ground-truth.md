# Session write-path ground truth (SQLAlchemy 2.1.8, sqlite)

Empirically verified 2026-11-11 against `sqlalchemy==2.1.8` + sqlite3/aiosqlite
in `.venv`, via `before_cursor_execute` SQL capture. Proving suite:
`tests/test_session_write.py` (28 tests, skip-gated on `Session.add`).

## State collections

- `Session()` defaults: `autoflush=True`, `expire_on_commit=True`.
- `session.new` / `session.dirty` / `session.deleted` are `IdentitySet`s;
  membership and `len()` are the observable contract.
- `session.add(obj)` returns `None`; `add_all` returns `None`.
- After `add`: `obj in session.new`, `dirty`/`deleted` empty,
  `session.is_modified(obj) is True`, `inspect(obj).session is session`,
  `inspect(obj).pending is True`.
- `Session` has **no `in_session` or `was_deleted` methods** in 2.1 — use
  `inspect(obj).session` / `inspect(obj).deleted`. (The task brief assumed
  `in_session`; it does not exist.)

## flush()

- Pending INSERT, single object: `INSERT INTO t (name, score) VALUES (?, ?)`
  — PK column omitted; `obj.id` populated from `lastrowid`.
- Pending INSERT, multiple objects: batched executemany with
  `INSERT ... VALUES (?, ?) RETURNING id` (`many=True`), PKs assigned in add
  order. Observable contract: sequential PKs; mechanism is dialect-dependent.
- Column order = table definition order minus PK.
- Client-side `default=0` is already materialized on the instance before flush.
- `session.get(Entity, pk)` autoflushes pending objects first; with
  `autoflush=False`, `get` of an unflushed explicit PK returns `None`.
- `session.execute(select(...))` and `session.scalars(select(...))` autoflush.

## commit()

- `commit()` = flush + COMMIT. Afterwards `new`/`dirty`/`deleted` are empty.
- `expire_on_commit=True` (default): `inspect(obj).expired is True` right after
  commit; ANY attribute access (including PK) emits
  `SELECT <all cols> FROM t WHERE t.id = ?` and clears `expired`.
- `expire_on_commit=False`: no refresh; stale values served.
- `session.refresh(obj)` re-SELECTs by PK. `refresh` on a pending object raises
  `InvalidRequestError` ("is not persistent within this Session").

## Dirty tracking / UPDATE

- Attribute assignment → object enters `session.dirty`, `is_modified` True.
- `flush()` emits `UPDATE t SET <only dirty cols> WHERE t.id = ?`.
- Reverting an attribute to its original value does NOT un-dirty it — SA still
  emits the UPDATE (set-on-write tracking, not value diffing).
- Re-`add` of a persistent object: silent no-op.
- Reading an expired attribute before `flush()` triggers a refresh SELECT
  first (seen as `SELECT id, name ... WHERE id = ?` before the UPDATE).

## delete()

- `session.delete(persistent)` → object in `session.deleted`; `flush()` emits
  `DELETE FROM t WHERE t.id = ?`.
- After flush but before commit: object still attached, attributes readable,
  `session.get` returns `None`.
- After commit: `inspect(obj).session is None`; `get` → `None`.
- `delete` of a pending (never-flushed) object → `InvalidRequestError`
  ("Instance ... is not persisted").
- `delete` of a detached object → silent no-op.

## rollback() / close()

- `rollback()` after flush: transaction rolled back, `new`/`dirty`/`deleted`
  emptied, pending objects DETACHED but keep flushed Python-side values
  (`obj.id == 1`, `obj.name` readable without SELECT).
- `rollback()` on dirty persistent objects: state expired; next attribute
  access returns the committed DB value.
- `close()` without commit rolls back (verified: row count 0).

## Transactions

- Session autobegins on first use. `session.begin()` when a transaction is
  already begun → `InvalidRequestError` ("A transaction is already begun on
  this Session.").
- `session.begin_nested()` → SAVEPOINT transaction (out of scope for slice 1).
- `in_transaction()`: False on fresh session, after `commit()`, after
  `rollback()`, after `close()`. Post-commit expired attribute access
  autobegins a new transaction (observed True after touching `obj.id`).

## Errors

- `session.add(object())` / unmapped class → `UnmappedInstanceError`
  ("Class 'builtins.object' is not mapped").
- `session.add(obj)` where obj is pending in another session →
  `InvalidRequestError` ("Object ... is already attached to session '8'
  (this is '7')").
- UNIQUE violation on `commit()` → `IntegrityError`
  (`(sqlite3.IntegrityError) UNIQUE constraint failed: ...`, `.orig` set).
  Afterwards the session is in partial-rollback: `session.get` etc. raise
  `PendingRollbackError` until `session.rollback()`.

## Identity map

- `session.get(E, pk)` twice → same instance (`is`); `scalars(select(E)...)`
  for a loaded PK → same instance; the instance returned IS the one that was
  added/committed.
- Different `Session` → different instance.
- `get` missing PK → `None`.
- `sa.inspect(obj).identity_key == (User, (1,), None)` (internal; catalyst may
  not expose an equivalent — suite asserts `is`-identity instead).

## Boundary notes

- Relationships, lazy/eager loading, `merge`/`expunge`, `Session.bulk_*`,
  `begin_nested` semantics, and `session.execute` returning `Result` with
  ORM-enabled UPDATE/DELETE (`session.execute(update(Model)...)` synchronization)
  are LATER slices — not probed deeply.
- Catalyst `Session` today (`python/catalyst/orm.py`) is a read facade:
  `execute`/`scalars`/`close` only — **no `get`, `add`, `flush`, `commit`,
  `rollback`, `delete`, `refresh`, `new`/`dirty`/`deleted`, `is_modified`**.
  The task brief mentioned `get`; it does not exist yet either.
