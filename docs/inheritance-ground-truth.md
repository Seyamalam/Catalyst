# Declarative inheritance ground truth (SQLAlchemy 2.1.8, sqlite)

Empirically verified 2026-11-11 against `sqlalchemy==2.1.8` + sqlite3 in
`.venv`, via `before_cursor_execute` SQL/parameter capture. Proving suite:
`tests/test_inheritance.py` (19 tests, skip-gated on `__mapper_args__`
polymorphic support). Common cases only — `with_polymorphic`,
`selectinload`/`joinedload` through hierarchies, `inherit_condition`,
composite inheritance, association-object patterns on subclass tables,
`polymorphic_abstract` intermediary classes, and concrete-table inheritance
are deferred (concrete is noted below and recommended deferred).

Model shapes used throughout:

```python
class Employee(Base):                              # STI
    __tablename__ = "person"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    type: Mapped[str] = mapped_column()
    __mapper_args__ = {"polymorphic_on": "type",
                       "polymorphic_identity": "employee"}

class Manager(Employee):                            # shares "person" table
    __mapper_args__ = {"polymorphic_identity": "manager"}

class JEmployee(Base):                              # JTI
    __tablename__ = "jemployee"                     # same shape + identities

class JManager(JEmployee):
    __tablename__ = "jmanager"
    id: Mapped[int] = mapped_column(ForeignKey("jemployee.id"), primary_key=True)
    level: Mapped[int]
    __mapper_args__ = {"polymorphic_identity": "manager"}
```

## Single-table inheritance (STI)

### Mapping / DDL

- The subclass has **no `__tablename__` of its own**; `Manager.__tablename__`
  reports the parent's (`"person"`) and `Manager.__table__ is
  Employee.__table__`. `Manager.__mapper__.local_table` is `person`. Exactly
  ONE `CREATE TABLE` is emitted:

  ```sql
  CREATE TABLE person (
      id INTEGER NOT NULL,
      name VARCHAR NOT NULL,
      type VARCHAR NOT NULL,
      PRIMARY KEY (id)
  )
  ```

  Identical to Catalyst's existing `Table`/`mapped_column` DDL — the
  discriminator is just a normal `String` column (`VARCHAR NOT NULL` from
  `Mapped[str]`).
- `polymorphic_on` must name a **mapped attribute**. An unmapped name
  raises `ArgumentError` at class creation:
  `Can't determine polymorphic_on value 'dtype' - no attribute is mapped to
  this name.` The discriminator column is NOT auto-created.
- **`polymorphic_identity` has NO default in 2.1.8.** Omitting it on ANY
  class in the hierarchy yields `mapper.polymorphic_identity is None` plus
  `SAWarning` ("does not indicate a 'polymorphic_identity' ... may include
  the 'polymorphic_abstract=True' parameter"). Flushing such an object
  INSERTs `NULL` into the discriminator (with a second `SAWarning`
  "incompatible polymorphic identity None") — on a NOT NULL column that is
  an `IntegrityError`. Every concrete class must declare its identity
  explicitly; there is no classname fallback.
- A subclass MAY declare additional `mapped_column`s — they are appended to
  the shared parent `Table` (verified: `extra` appears in `person`'s column
  list, NULL for other rows). Python-only attributes/mixins also work.
- Discriminator column type is unrestricted: `Mapped[int]` +
  `polymorphic_identity: 1/2` produces `kind INTEGER NOT NULL` and
  round-trips int identities (`INSERT ... (kind) VALUES (?)` → `2`).

### INSERT

- `session.add(Manager(name="boss"))` + commit emits ONE INSERT with the
  discriminator auto-set to the class identity:

  ```sql
  INSERT INTO person (name, type) VALUES (?, ?) RETURNING id   -- ('boss','manager')
  INSERT INTO person (name, type) VALUES (?, ?) RETURNING id   -- ('worker','employee')
  ```

  `mgr.type == 'manager'` in memory after flush. Passing an explicit
  `type=` value that differs from the identity triggers `SAWarning`
  "incompatible polymorphic identity" and the row may not load correctly.
- Batched inserts of mixed classes still emit per-class-statement groups
  (`executemany` per identity when several pending objects share one).

### SELECT / identity resolution

- `session.get(Employee, mgr_id)` → returns a `Manager` instance. SQL is
  UNFILTERED: `SELECT person.id, person.name, person.type FROM person
  WHERE person.id = ?` — the discriminator VALUE in the row drives Python
  class resolution at hydration.
- `session.get(Manager, emp_id)` (row is `type='employee'`) → **`None`**.
  SQL appends the identity filter: `WHERE person.id = ? AND person.type IN
  (?)` params `(id, 'manager')`.
- `select(Manager)` → `WHERE person.type IN (?)`. The `IN` set includes
  ALL descendant identities: with `SeniorManager(Manager)` present,
  `select(Manager)` binds `('manager', 'senior_manager')` and returns both
  classes; `select(SeniorManager)` binds `('senior_manager',)` only.
- `select(Employee)` → no WHERE at all; hydrates mixed `Employee`/`Manager`/
  `SeniorManager` instances in row order. `scalars()` behaves identically.
- Unknown discriminator values in the table hydrate as the BASE class
  (nearest mapped ancestor) — no error (standard SA fallback; not
  re-probed but implied by the `polymorphic_map` mechanics).

### UPDATE / DELETE

- Instance flush (`mgr.name = "x"`): `UPDATE person SET name=? WHERE
  person.id = ?` — `type` appears in NEITHER `SET` nor `WHERE` (the PK
  suffices; the discriminator is never rewritten).
- ORM bulk `update(Manager).values(...)`: `UPDATE person SET ... WHERE
  person.type IN (?, ?)` — the identity filter IS appended (verified with
  `values(type='x')` → `SET type=? WHERE type IN ('manager','senior_manager')`).
- ORM bulk `delete(Manager)`: `DELETE FROM person WHERE person.type IN (?)`
  — deletes only rows of that identity subtree (rowcount verified).
- ORM bulk `delete(Employee)`: `DELETE FROM person` — no filter.
- `session.delete(manager_instance)` flush: `DELETE FROM person WHERE
  person.id = ?` — PK only, no type filter.

## Joined-table inheritance (JTI)

### DDL

- Two `CREATE TABLE`s. Child PK column is simultaneously the FK:

  ```sql
  CREATE TABLE jemployee (id INTEGER NOT NULL, name VARCHAR NOT NULL,
                          type VARCHAR NOT NULL, PRIMARY KEY (id))
  CREATE TABLE jmanager (id INTEGER NOT NULL, level INTEGER NOT NULL,
                         PRIMARY KEY (id),
                         FOREIGN KEY(id) REFERENCES jemployee (id))
  ```

  `JManager.__table__` is its own table; `__mapper__.local_table` is
  `jmanager`. `polymorphic_on`/`polymorphic_identity` semantics are
  IDENTICAL to STI (still required per concrete class, still auto-set on
  INSERT, discriminator lives on the PARENT table).
- JTI works with **no `__mapper_args__` at all** (no discriminator): both
  tables create fine, subclass INSERTs/SELECTs work — but parent queries
  can no longer resolve the Python subclass (see SELECT).

### INSERT

- `session.add(JManager(name="jm", level=3))` emits TWO INSERTs in
  dependency order — parent first, child second, child id bound to the
  parent-generated `lastrowid`:

  ```sql
  INSERT INTO jemployee (name, type) VALUES (?, ?) RETURNING id  -- ('jm','manager')
  INSERT INTO jmanager (id, level) VALUES (?, ?)                -- (1, 3)
  ```

  (The `RETURNING id` parent refresh plus a post-commit expiry SELECT is
  standard SA session behavior, unchanged by inheritance.)

### SELECT

- `select(JManager)` → INNER JOIN, child columns first, parent PK aliased
  `id_1`, **no discriminator filter** (the join itself restricts to
  managers):

  ```sql
  SELECT jmanager.id, jemployee.id AS id_1, jemployee.name,
         jemployee.type, jmanager.level
  FROM jemployee JOIN jmanager ON jemployee.id = jmanager.id
  ```

- `select(JEmployee)` / `session.get(JEmployee, id)` → hits ONLY the
  parent table: `SELECT jemployee.id, jemployee.name, jemployee.type FROM
  jemployee [WHERE jemployee.id = ?]`. The discriminator still resolves
  the Python class (`get` on a manager row returns `JManager`), but
  **subclass-table columns are NOT joined** — they load lazily per
  instance on first access:

  ```sql
  SELECT jmanager.level AS jmanager_level FROM jmanager WHERE ? = jmanager.id
  ```

  Verified: one parent SELECT for the whole `select(JEmployee)` result;
  one child SELECT per manager instance whose subclass attribute is
  touched. Plain `JE` rows never trigger it.
- Without `polymorphic_on`, `select(JEmployee)` returns base-class
  instances even for rows that have child-table rows (the lone
  `x`/`JM5` row hydrated as `JE5`). `select(JM5)` still inner-joins and
  returns `JM5`. Discriminator is what buys subclass resolution on
  parent-side queries.

### UPDATE / DELETE

- Instance flush on a `JManager`: UPDATEs go to BOTH tables as needed —
  parent-column changes emit `UPDATE jemployee ... WHERE id=?`, child-column
  changes `UPDATE jmanager ... WHERE id=?` (standard SA per-table UOW;
  not separately re-probed beyond the delete path, which is verified).
- `session.delete(jmanager_instance)` → TWO DELETEs, child first:

  ```sql
  DELETE FROM jmanager WHERE jmanager.id = ?
  DELETE FROM jemployee WHERE jemployee.id = ?
  ```

- ORM bulk `delete(JManager)` → `DELETE FROM jmanager` ONLY — verified
  surprise: the parent `jemployee` row is ORPHANED (row counts: `je` kept
  its row, `jm` emptied). Bulk `delete(JEmployee)` → `DELETE FROM
  jemployee` only, likewise orphaning `jmanager` rows (`jm` count stayed
  1). Bulk DML on JTI hierarchies does not cascade across the table pair;
  only instance-level `session.delete()` walks both tables.

## Shared behavior

- `__abstract__ = True` intermediate bases contribute their `Mapped`
  columns to concrete subclasses and are themselves unmapped (no
  `__table__`). A plain (non-declarative) mixin class with `Mapped`
  annotations works the same way. NOTE for Catalyst: its mapper scan
  currently reads only `cls.__dict__["__annotations__"]` — inherited
  annotations must be collected along the MRO for mixins/abstract bases
  to contribute columns.
- Relationships TO an inheritance parent work unchanged: `ForeignKey` to
  `sti_person.id` / `jti_employee.id` reaches subclass rows, and the lazy
  load (`SELECT ... WHERE id = ?`) hydrates the right subclass through
  the same discriminator resolution. `select(Dept.head)` returns `PM`/`Mgr`
  instances.
- `MappedAsDataclass` + STI: the discriminator column must be
  `init=False` (otherwise the generated `__init__` requires it as a
  positional arg — `TypeError: __init__() missing ... 'type'`). With
  `init=False`, `DM(name="dm")` constructs fine and flush auto-sets
  `type='manager'`. Likely a separate slice (depends on
  `MappedAsDataclass` landing first).
- Concrete-table inheritance (`concrete=True`, each class fully
  redeclares all columns on its own table, no FK link): works in SA but
  every concrete class must redeclare inherited columns and PKs don't
  auto-generate the same way — awkward, rarely used. **Recommended
  defer** ("won't support" candidate unless a real use case appears).

## Recommended implementation slice

STI first, JTI as a follow-up slice — they share the mapper-hierarchy
machinery but JTI's two-table flush is the hard part:

**Slice A — STI (+ shared hierarchy plumbing):**

1. `__mapper_args__` parsing on declarative subclasses: `polymorphic_on`
   (must resolve to a mapped column name → `ArgumentError` otherwise),
   `polymorphic_identity` (NO default — `None` + warning semantics), and
   subclass-without-`__tablename__` → share parent's `Table`/`Mapper`
   hierarchy instead of the current "intermediate base" early return.
2. Discriminator auto-set at flush: INSERT includes the column with the
   instance's identity; UPDATE/DELETE-where never touch it for instance
   ops; bulk `update`/`delete` on a subclass append `type IN (identity
   subtree)`.
3. Hydration class resolution: `select`/`get` on any hierarchy class
   emits the identity-filtered or unfiltered parent-table SELECT per the
   rules above; the discriminator value picks the Python class (identity
   set = self + all descendants).
4. `__abstract__`/mixin annotation collection along the MRO (needed for
   realistic hierarchies anyway).
5. Non-string discriminators fall out for free once the column type is
   honored.

**Slice B — JTI:**

1. Subclass with `__tablename__` + PK-that-is-FK → second `Table`, child
   columns only; flush emits parent INSERT (with `lastrowid` write-back)
   then child INSERT — the existing `execute_flush` dependency ordering
   already handles FK propagation for relationships, so this is mostly a
   new UOW edge (parent/child same-object, not two objects).
2. `select(Subclass)` → inner join with child-columns-first + `id_1`
   aliasing; `select(Parent)`/`get` → parent table only + lazy per-instance
   child-column load (a new deferred-attribute mechanism — today Catalyst
   hydrates eagerly, so this is genuinely new).
3. Instance delete → child DELETE then parent DELETE; document/accept the
   bulk-delete orphaning surprise or gate it.

Do NOT split STI further — items 1–4 are one coherent mapper change.
JTI should be its own slice: the deferred child-column load and
two-table-per-object flush are each independently sizable.

## Boundary notes / surprises

- **`polymorphic_identity` does NOT default to the class name** in 2.1.8 —
  the task brief's assumption is wrong for this version; omitting it gives
  `None` + `SAWarning` + NULL inserts. Every concrete class must declare it.
- `polymorphic_on` requires a *mapped* attribute; SA does not synthesize
  the discriminator column (`ArgumentError` otherwise).
- `get(Subclass, parent_typed_row_id)` → `None`, not an error — the
  identity filter is part of the PK lookup.
- JTI parent queries resolve the Python class from the discriminator but
  do NOT join the child table — subclass attributes are per-instance lazy
  loads (`SELECT child.col WHERE ? = child.id`), a third lazy-load flavor
  beyond relationship loading.
- Bulk ORM `delete()`/`update()` on JTI parents emits single-table DML and
  orphans the other table's rows — only `session.delete(instance)` walks
  both tables.
- STI subclass `mapped_column`s append to the SHARED parent table (NULL
  for sibling rows) — there is no per-subclass column isolation in STI.
- `MappedAsDataclass` requires the discriminator be `init=False` or the
  generated `__init__` demands it.
