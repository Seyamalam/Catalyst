# Session merge / expunge / bulk / lifecycle ground truth (SQLAlchemy 2.1.8, sqlite)

Empirically verified 2026-11-11 against `sqlalchemy==2.1.8` + sqlite3 in the
project `.venv` (Python 3.12), via `before_cursor_execute` SQL capture.
Proving suite: `tests/test_session_lifecycle.py` (skip-gated on
`Session.merge`). Scope: `Session.merge`, `expunge`/`expunge_all`, the three
`bulk_*` methods, ORM-enabled `session.execute(update/delete)`, and
session-lifecycle edges (`close` reuse, `populate_existing`, detached-expired
access). All semantics below are dialect-agnostic session behavior; SQL shapes
quoted are the SQLite rendering.

## `session.merge(instance)`

`merge` returns **the persistent instance belonging to this session** — it is
almost never the object you passed:

- Transient or detached object **with a PK that exists**: emits
  `SELECT <cols> FROM t WHERE t.id = ?`, copies the source state onto the
  identity-map instance (creating one if needed), and returns it. The SOURCE
  object is untouched — `merged is not obj`, `inspect(obj).session is None`,
  `inspect(obj).transient is True`. Changed attributes land as dirty state →
  `UPDATE t SET <dirty cols> WHERE t.id = ?` on flush/commit.
- Transient/detached object **with a PK not in the DB**: the SELECT returns
  nothing → the merged copy is **pending** (`obj in session.new`); flush
  INSERTs it, keeping the supplied PK (`INSERT INTO u (id, name, score)
  VALUES (?, ?, ?)`).
- Object **without a PK** (`id=None`): merged copy is pending; flush INSERTs
  without the PK column and assigns `lastrowid`.
- Persistent object **in another open session**: target session SELECTs by
  PK, returns ITS instance; the source object stays persistent+dirty in its
  own session. State (including unflushed dirty values like `name='from-a'`)
  is copied → `UPDATE` on target commit.
- Pending object already in `session.new`: merge **autoflushes** (INSERT
  emitted, PK assigned), then returns the **same** object, now persistent.
- Object pending `delete()` in the same session: autoflush DELETEs the row,
  the merge SELECT finds nothing → merged copy is pending → flush re-INSERTs
  (the delete is effectively resurrected as a new row).
- Merging into a session that already holds the PK in its identity map
  returns that same instance; a second `merge` of the same PK overwrites the
  first merge's state (last writer wins, both become one UPDATE with the
  final values — observed `UPDATE ... ('again2', 9, 1)` only).
- Non-mapped `__dict__` attributes are **not** copied to the merged instance.
- `merge` of an unmapped object → `UnmappedInstanceError`
  ("Class 'builtins.object' is not mapped").
- `merge` **always autoflushes pending work first — even when the session was
  constructed with `autoflush=False`** (verified: pending INSERT emitted
  before merge's SELECT in both cases).
- Quirk: merging a transient copy whose state equals the persistent
  instance's state leaves the instance in `session.dirty` (`u in s.dirty`
  is True) while `session.is_modified(u)` is False.

### `merge(load=False)`

- Transient object → `InvalidRequestError`: "merge() with load=False option
  does not support objects transient (i.e. unpersisted) objects. flush() all
  changes on mapped instances before merging with load=False."
- Dirty object → `InvalidRequestError`: "...does not support objects marked
  as 'dirty'..."
- Clean detached object, PK exists: **no SELECT is emitted**; returns a
  persistent instance with the copied state (not dirty if unchanged).
- Clean detached object, PK NOT in DB: still no SELECT up front; the merged
  instance is treated as persistent — `flush()` then emits a refresh
  `SELECT ... WHERE id = ?`, finds no row, and raises
  `ObjectDeletedError` ("Instance '<User ...>' has been deleted, or its row
  is otherwise not present.").

### Merge cascades

- Default relationship cascade `"merge,save-update"` recurses the object
  graph: `merge(detached_parent)` SELECTs the parent by PK, lazy-loads
  `parent.children` (`SELECT ... WHERE ? = child.parent_id`), and merges each
  child by PK — changed children → `UPDATE`, brand-new transient children →
  pending → `INSERT INTO c (name, parent_id) VALUES (?, ?)` with the FK
  populated from the merged parent.
- Merged children ARE the session identity-map instances
  (`s2.get(Child, 1) is m.children[0]`).
- With `cascade="save-update"` (no `merge` token) on the relationship, the
  observed result was that children still ended up `persistent` in the
  session after merge — flag for implementer verification; the safe parity
  target is the default-cascade behavior.

## `session.expunge(instance)` / `expunge_all()`

- `expunge(persistent)` → detached: `inspect(obj).session is None`,
  `detached is True`, `persistent is False`; loaded attribute values stay
  readable; removed from the identity map — a later `session.get` returns a
  DIFFERENT instance. Returns `None`.
- `expunge(pending)` → the object becomes **transient** (`transient is
  True`, `detached is False`, `session is None`), leaves `session.new`, and
  is never INSERTed.
- `expunge` of an object pending `delete()` → leaves `session.deleted`; the
  row survives flush.
- `expunge(dirty persistent)` → detached but keeps the current (dirty)
  attribute values in `__dict__`. Re-`add()`ing that detached object makes it
  **persistent** again (not pending) and the kept values flush as an UPDATE.
- `expunge` of a transient object, or of an object not in this session →
  `InvalidRequestError` ("Instance <...> is not present in this Session").
- `expunge` does **NOT** autoflush: a pending object's `id` stays `None`
  across an expunge call.
- `expunge_all()` detaches everything (pending → transient, dirty →
  detached), empties the identity map, returns `None`.
- Attribute writes on a detached object succeed silently (plain Python
  attribute set). Accessing an **expired** attribute on a detached object
  raises `DetachedInstanceError` ("Instance <...> is not bound to a Session;
  attribute refresh operation cannot proceed").

## Bulk operations

`Session` has exactly three bulk methods in 2.1.8 —
`dir(Session)` shows `bulk_insert_mappings`, `bulk_save_objects`,
`bulk_update_mappings`. **There is no `bulk_delete_mappings`.**

Signatures (verified via `inspect.signature`):

- `bulk_insert_mappings(mapper, mappings, return_defaults=False, render_nulls=False) -> None`
- `bulk_update_mappings(mapper, mappings) -> None` — no kwargs at all.
- `bulk_save_objects(objects, return_defaults=False, update_changed_only=True, preserve_order=True) -> None`

### `bulk_insert_mappings`

- Emits a single batched `INSERT INTO u (id, name, score) VALUES (?, ?, ?)`
  (executemany params list). Returns `None`.
- Client-side `default=0` IS materialized into the row (`score` omitted →
  `params=(30, 'nodefault', 0)`).
- Rows do NOT enter the session: `session.new`/`dirty`/`deleted` and the
  identity map stay empty; no PK write-back to any object.
- `return_defaults=True` still returns `None` on SQLite.

### `bulk_update_mappings`

- Requires the PK in every mapping row; a row missing `id` →
  `InvalidRequestError` ("No primary key value supplied for column(s) u.id;
  per-row ORM Bulk UPDATE by Primary Key requires that records contain
  primary key values").
- Emits ONE single-row `UPDATE u SET <provided cols> WHERE u.id = ?` per
  mapping (not executemany).
- `None` values are rendered as `NULL` binds by default.
- A PK matching no row → `StaleDataError` ("UPDATE statement on table 'u'
  expected to update 1 row(s); 0 were matched.").
- **Does NOT synchronize the session**: in-session instances keep stale
  values (`u1.name` stays `'u1'` after the UPDATE), are not marked dirty,
  and a later `session.get` returns the same stale identity-map instance.
  Only `expire`/`refresh`/commit-expiry picks up the new values.

### `bulk_save_objects` (legacy)

- Bypasses the unit of work entirely: passed objects are NOT added to
  `session.new`, are not pending afterwards, and their PKs are NOT written
  back (`obj.id` stays `None` even after `flush()` — rows get
  server-assigned ids the objects never see).
- Persistent dirty objects passed in DO get their UPDATE emitted — but the
  objects remain dirty, so `commit()` emits the same UPDATE a second time.

### Autoflush interaction

- **Bulk methods do NOT autoflush**: with a pending INSERT and a dirty
  persistent object in the session, `bulk_insert_mappings` /
  `bulk_update_mappings` / `bulk_save_objects` all run their SQL first;
  `pending.id` stays `None`, `obj in session.new` stays True, and the dirty
  UPDATE is only emitted at `commit()`. (Contrast: `merge` DOES autoflush,
  unconditionally.)

### ORM-enabled `session.execute(update(Model)...)` / `delete(Model)`

- `session.execute(update(User).where(User.id == 1).values(name=...))` with
  default `synchronize_session="auto"` → evaluates to `'evaluate'` for
  simple PK WHEREs: emits plain `UPDATE u SET name=? WHERE u.id = ?`
  (no RETURNING), and matching in-session instances are updated in-memory
  (`u1.name == 'ormupd'`) WITHOUT being marked dirty
  (`is_modified` False).
- `synchronize_session=False` → same SQL, in-session instances keep stale
  values.
- `synchronize_session="fetch"` → `UPDATE ... RETURNING id`, then session
  state synchronized.
- Non-evaluateable WHERE (e.g. `func.upper(User.name) == "U1"`) under
  `"auto"` falls back to the fetch strategy (`UPDATE ... RETURNING id`).
- `session.execute(delete(User).where(User.id == 3))` default → emits
  `DELETE FROM u WHERE u.id = ?`; the in-session instance moves to the
  deleted state (`inspect(u3).deleted is True`, still attached), and
  `session.get(User, 3)` returns `None`. With `synchronize_session=False`
  the instance stays attached and stale.

## Session lifecycle edges

- `close()` rolls back uncommitted work and detaches EVERYTHING: persistent
  objects → detached with loaded values retained (dirty in-memory values
  also retained, e.g. `u.name` still `'dirty-before-close'`), pending
  objects → transient (`p.id is None`, `p.name` readable, never inserted),
  `in_transaction()` False. `close()` is idempotent (double-close fine).
- **SA `Session` is reusable after `close()`**: `session.add(...)` +
  `commit()` works and autobegins a new transaction. (Catalyst gap: its
  `Session.close()` sets a permanent `_closed` flag — see parity notes.)
- `with Session() as s:` exit calls `close()` — pending work rolled back —
  and the session remains usable afterwards.
- `session.get(Entity, pk, populate_existing=True)` re-SELECTs and refreshes
  the existing identity-map instance in place (`is`-identical); a plain
  `get` returns the stale cached instance with no SQL.
- Detached + expired attribute access → `DetachedInstanceError` (see
  expunge section).
- `in_transaction()`: False on fresh session, True after first DB-touching
  call, False after `commit()`/`rollback()`/`close()`.
