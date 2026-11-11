# M4 tail ground truth — `MappedAsDataclass`, session savepoints, `SessionEvents`

Empirically verified 2026-11-11 against `sqlalchemy==2.1.8` + sqlite3
(CPython `sqlite3` driver) in `.venv`, via `inspect.signature`,
`dataclasses.fields`, `before_cursor_execute` SQL capture, and event
recorders on every `SessionEvents` hook. Proving suites (all skip-gated
until the features land):

- `tests/test_mapped_as_dataclass.py` (12 tests, gate: `MappedAsDataclass` symbol)
- `tests/test_savepoints.py` (10 tests, gate: `begin_nested` returning a
  `SessionTransaction` with `nested is True` + UOW snapshot semantics)
- `tests/test_session_events.py` (9 tests, gate: `catalyst.event` /
  `SessionEvents`)

The pre-existing `test_mapped_as_dataclass_discriminator_init_false` in
`tests/test_inheritance.py` matches real behavior: the discriminator column
marked `init=False` stays out of `__init__`, the mapper still stamps the
`polymorphic_identity`, and the INSERT writes it.

---

## 1. `MappedAsDataclass`

`class U(MappedAsDataclass, Base)` turns the mapped class into a real
stdlib dataclass (`dataclasses.is_dataclass(U) is True`).

### Base ordering

- `MappedAsDataclass` MUST precede the declarative base:
  `class U(MappedAsDataclass, DeclarativeBase)` works;
  `class U(DeclarativeBase, MappedAsDataclass)` raises `ArgumentError`
  ("Attribute 'id' on class ... includes dataclasses argument(s): 'init'
  but class does not specify SQLAlchemy native dataclass configuration").
- A `MappedAsDataclass` subclass of an already-declarative dataclass base
  (`class Sub(Base2)` where `Base2(MappedAsDataclass, DeclarativeBase)`)
  works and gets its own `__init__` — repeating `MappedAsDataclass` in the
  bases of such a subclass raises `TypeError: Cannot create a consistent
  method resolution order`.

### `__dataclass_params__`

`init=True, repr=True, eq=True, order=False, unsafe_hash=False,
frozen=False, match_args=True, kw_only=False, slots=False`. There is no
public knob on the mixin to change these; per-field control goes through
`mapped_column` arguments.

### `__init__` signature

- Fields are `POSITIONAL_OR_KEYWORD` unless `mapped_column(kw_only=True)`,
  which makes them `KEYWORD_ONLY`.
- `init=False` fields are excluded from `__init__`.
- `default=`/`default_factory=` materialize **at construction**, not at
  flush: `U(name="a").age == 0`, `.score == 1.5`, `.nick == "n"` (factory)
  before any session exists. `init=False` columns with defaults also
  materialize (`hidden == 7`); an `init=False` PK with no default stays
  `None` until flush.
- Mechanism note: the dataclass field's default for a column default is
  the `LoaderCallableStatus.DONT_SET` sentinel; the generated `__init__`
  resolves it. `dataclasses.fields()` shows `init`/`kw_only`/`repr` flags
  faithfully (`compare=True` on every field).
- Required fields have no default → `TypeError` when omitted; unknown
  keywords → `TypeError`.

### eq / repr / hash / post-init

- `eq=True`: `__eq__` compares ALL fields — including `init=False` and
  `repr=False` ones — and requires identical class
  (`U(name="x") == U(name="x")`, `!= U(name="x", age=1)`, `!= object()`).
  Instances are unhashable (`__hash__ = None`).
- `repr=False` on `mapped_column` omits the field from `__repr__`;
  `init=False` fields still render: `U(id=None, name='a', age=3, nick='n',
  score=1.5, hidden=7)` — note `descr` (repr=False) absent.
- `__post_init__` runs after generated `__init__` assignment and can set
  `init=False` attributes (`self.upper = self.name.upper()` works).

### Ordering rules

Dataclass rules propagate: a non-default field after a defaulted one
raises `InvalidRequestError` wrapping
`TypeError("non-default argument 'b' follows default argument")`.
`kw_only=True` on the later field rescues the ordering (keyword-only
fields don't participate in positional ordering).

### Relationships

- `relationship()` fields join `__init__` with `init=True` by default:
  a uselist collection with no default is a REQUIRED argument —
  `P(name="x")` → `TypeError: missing 1 required positional argument:
  'children'`; `P(name="x", children=[...])` works and wires
  `back_populates` both directions at construction.
- `relationship(init=False)` is honored and drops the field from
  `__init__` entirely (`P3(name="x").children == []`).
- Scalar relationships and FK columns with `default=` are optional
  (`DONT_SET` → resolve to `None`/default at construction).

### Inheritance discriminator

`type: Mapped[str] = mapped_column(init=False)` under
`polymorphic_on`/`polymorphic_identity` stays out of `__init__`; the
identity is stamped at construction (`Mgr(name="boss").type == "m"`
before flush) and written by INSERT. Subclass `__init__` signatures
contain only the subclass's init fields.

### Boundary notes for Catalyst

- `MappedColumn` already carries `init`/`kw_only`/`default`/
  `default_factory` but has **no `repr` field** — `mapped_column(repr=False)`
  is not currently accepted and must be added for parity.
- `relationship()` has no `init` kwarg today.
- Catalyst's kwargs `__init__` is hand-rolled; `MappedAsDataclass` should
  synthesize a real `dataclasses.dataclass` (or equivalent signature/eq/
  repr semantics) — eq must include `init=False`/`repr=False` fields and
  instances must be unhashable.
- Flush/commit of dataclass instances is otherwise identical to normal
  mapped instances (verified roundtrip + loaded-row `__eq__`).

---

## 2. Session savepoints

`session.begin_nested()` returns a `SessionTransaction` (NOT the bare
`SavepointTransaction` `Connection.begin_nested` returns today) with
`nested is True`, `.session`, `.parent`, commit/rollback/close/context-
manager support.

### SQL shape (verified via `before_cursor_execute`)

- `SAVEPOINT sa_savepoint_N` — counter is per session transaction:
  `sa_savepoint_1`, then `sa_savepoint_2` for a nested level in the SAME
  transaction; resets for the next transaction.
- `txn.commit()` → `RELEASE SAVEPOINT <name>`.
- `txn.rollback()` → `ROLLBACK TO SAVEPOINT <name>` — NO `RELEASE`
  follows (SQLite doesn't require it).
- `with session.begin_nested():` commits (RELEASE) on clean exit,
  rolls back to the savepoint on exception; the session stays usable.
- `session.begin_nested()` inside `begin_nested()` stacks savepoints.
- `session.begin()` twice → `InvalidRequestError` ("A transaction is
  already begun on this Session.").

### UOW snapshot semantics on `ROLLBACK TO SAVEPOINT` (the important part)

Rollback to a savepoint reverts the SESSION to its state at savepoint
creation, not just the database:

- **Pending (unflushed) objects added inside the savepoint are expunged
  entirely** — `session is None`, out of `session.new`/identity map,
  `inspect().pending/detached` both False. They do NOT return to pending.
- **Objects INSERTed (flushed) inside the savepoint are detached from the
  session** — `inspect(obj).session is None`, removed from the identity
  map, all state flags False, Python attribute values retained
  (`obj.id`, `obj.name` still readable). Outer `commit()` does NOT
  resurrect the row; re-`add` works and inserts exactly once.
- **Dirty persistent objects** have in-memory attribute values reverted
  to the pre-savepoint snapshot and leave `session.dirty` — no SELECT is
  emitted for the revert (verified: `name` goes `"in-sp"` → `"base"`
  silently). The DB keeps the pre-savepoint value after outer commit.
- **DELETEs flushed inside the savepoint are undone**: the row survives
  and the object returns to persistent (not in `session.deleted`).
- With a REAL outer transaction (any DML ran before the savepoint),
  `RELEASE` merges into it and a later `session.rollback()` undoes
  EVERYTHING including released-savepoint work.

### SQLITE GOTCHA (verified, dialect-specific)

pysqlite's legacy isolation handling issues an implicit `BEGIN` only
before INSERT/UPDATE/DELETE — never for `SAVEPOINT`, `SELECT`, or an
explicit `session.begin()` (which emits no BEGIN at all). Consequence:
if `begin_nested()` is the FIRST database-touching operation of the
session transaction, the `SAVEPOINT` itself opens the SQLite transaction,
and `RELEASE SAVEPOINT` returns to autocommit — **the work is committed
and a later `session.rollback()` is a DBAPI no-op that cannot undo it**.
Verified: `begin_nested → add → flush → sp.commit() → session.rollback()`
leaves the row in the DB; inserting/flushing anything first makes
`session.rollback()` undo everything. `session.begin()` does NOT avoid
this (no BEGIN is emitted). Catalyst's Core savepoint path issues an
explicit `BEGIN` when autocommit (`src/lib.rs` `begin_savepoint`), so
Catalyst does NOT reproduce this gotcha at the Core level — the session
slice must decide whether to mirror SA's pysqlite-visible behavior or
document the divergence.

### Catalyst boundary

`Session.begin_nested` currently delegates to `Connection.begin_nested`
and returns `SavepointTransaction` (`catalyst_sp_N` names — SA uses
`sa_savepoint_N`; a naming difference to note, not necessarily parity-
blocking since names are internal). Missing for parity: the
`SessionTransaction` wrapper (`nested is True`), the UOW snapshot/restore
on `ROLLBACK TO SAVEPOINT` (expunge-inside objects, revert dirty
snapshots, resurrect deletes), `SessionTransaction` context-manager
semantics at session level, and event hooks (`after_begin` etc.).

---

## 3. `SessionEvents`

No event API exists in Catalyst today. SA exposes
`sqlalchemy.event.listen/contains/remove/listens_for` targeting the
`Session` CLASS or individual session INSTANCES, plus the
`SessionEvents` interface listing every hook. Unknown event names raise
`InvalidRequestError` ("No such event 'x' for target ...").

### Verified firing matrix (all events attached, ordered)

`session.add(obj)` on a fresh session:

```
after_transaction_create, before_attach, after_attach, transient_to_pending
```

`session.flush()` (autobegin; inside an active transaction the same
minus nothing — the flush ALWAYS brackets itself):

```
before_flush, after_transaction_create, after_begin, after_flush,
pending_to_persistent, after_flush_postexec, after_transaction_end
```

(`after_transaction_create`/`after_transaction_end` bracket every flush
— they are SessionTransaction markers, not DB transactions;
`after_begin` fires only when the DB transaction actually begins.)

`commit()` with pending work (fresh transaction):

```
before_commit, before_flush, after_transaction_create, after_begin,
after_flush, pending_to_persistent, after_flush_postexec,
after_transaction_end, after_commit, after_transaction_end
```

`commit()` with nothing pending still fires
`after_transaction_create, before_commit, after_commit,
after_transaction_end` — commit always opens a session transaction.

`session.rollback()` on an active transaction (pending-only or flushed):

```
after_rollback, after_transaction_end, after_soft_rollback
```

On a session with NO transaction: nothing fires.

`delete(persistent); commit()` (fresh transaction):

```
before_commit, before_flush, after_transaction_create, after_begin,
after_flush, persistent_to_deleted, after_flush_postexec,
after_transaction_end, after_commit, deleted_to_detached,
after_transaction_end
```

Note `persistent_to_deleted` lands between `after_flush` and
`after_flush_postexec`, and `deleted_to_detached` lands AFTER
`after_commit`.

`get(Entity, pk)` on a fresh session that loads a row:

```
after_transaction_create, after_begin, loaded_as_persistent
```

`loaded_as_persistent` fires once per newly hydrated instance NOT already
in the identity map (bulk SELECTs, autoflush-triggered loads, `merge` of
a detached object); identity-map hits do not re-fire it.

`session.expunge(obj)` → `persistent_to_detached`.
`session.expunge_all()` → **no events at all** (verified surprise —
`expunge_all` bypasses per-instance dispatch).
`session.merge(detached)` → `loaded_as_persistent` (NOT
`detached_to_persistent`); `merge(transient)` behaves like `add`
(`before_attach, after_attach, transient_to_pending`).

`session.delete(obj)` itself fires nothing (the transition event fires
at flush); `session.close()`:

- clean session, no transaction → nothing;
- pending work → `pending_to_transient` then `after_transaction_end`;
- dirty persistent → `persistent_to_detached` then
  `after_transaction_end`.

Re-`add` of a deleted object → `InvalidRequestError` ("has been deleted").

### `before_flush(session, flush_context, instances)`

- `session.new`/`session.dirty`/`session.deleted` are live, mutable
  collections — an object `session.add`ed INSIDE `before_flush` is
  flushed by the SAME flush (verified: row inserted, second
  `pending_to_persistent` fired).
- `instances` is NOT `session.new` (verified `is` → False).
- Signature: `before_flush(session, flush_context, instances)`;
  `after_flush(session, flush_context)`;
  `after_flush_postexec(session, flush_context)`;
  state-transition hooks get `(session, instance)`; transaction hooks
  get `(session)`.

### Full `SessionEvents` surface (2.1.8)

`after_attach`, `after_begin`, `after_bulk_delete`, `after_bulk_update`,
`after_commit`, `after_flush`, `after_flush_postexec`, `after_rollback`,
`after_soft_rollback`, `after_transaction_create`,
`after_transaction_end`, `before_attach`, `before_commit`, `before_flush`,
`deleted_to_detached`, `deleted_to_persistent`, `detached_to_persistent`,
`loaded_as_persistent`, `pending_to_persistent`, `pending_to_transient`,
`persistent_to_deleted`, `persistent_to_detached`,
`persistent_to_transient`, `transient_to_pending`, plus
`do_orm_execute` (query-level, out of scope here).

### Catalyst boundary

Needs: an `event` module (`listen`/`contains`/`remove`/`listens_for`)
accepting the `Session` class or instances, a `SessionEvents` registry of
valid names, dispatch at every point in the matrix above (the
transaction-marker events imply `SessionTransaction` objects must exist
first — savepoints slice is a prerequisite), mutable `session.new`/
`dirty`/`deleted` visibility inside `before_flush`, and the
`InvalidRequestError` on unknown names.
