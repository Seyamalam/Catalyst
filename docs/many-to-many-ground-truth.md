# Many-to-many / `secondary` ground truth (SQLAlchemy 2.1.8, sqlite)

Empirically verified 2026-11-11 against `sqlalchemy==2.1.8` + sqlite3 in
`.venv`, via `before_cursor_execute` SQL capture. Proving suite:
`tests/test_many_to_many.py` (skip-gated on `relationship()` accepting the
`secondary` kwarg). Builds on `docs/relationship-loading-ground-truth.md`;
common cases only — the association-object pattern, `secondaryjoin` beyond
the self-referential case, `and_`/custom `primaryjoin`, `ForeignKeyConstraint`
composites, and non-PK association tables at the ORM level are deferred.

Model used throughout:

```python
assoc = Table(
    "assoc", Base.metadata,
    Column("a_id", Integer, ForeignKey("a.id"), primary_key=True),
    Column("b_id", Integer, ForeignKey("b.id"), primary_key=True),
)

class A(Base):
    __tablename__ = "a"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    bs: Mapped[list["B"]] = relationship(secondary="assoc", back_populates="as_")

class B(Base):
    __tablename__ = "b"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    as_: Mapped[list[A]] = relationship(secondary=assoc, back_populates="bs")
```

## Association table DDL

- Composite PK + two FK columns renders:

  ```sql
  CREATE TABLE assoc (
      a_id INTEGER NOT NULL,
      b_id INTEGER NOT NULL,
      PRIMARY KEY (a_id, b_id),
      FOREIGN KEY(a_id) REFERENCES a (id),
      FOREIGN KEY(b_id) REFERENCES b (id)
  )
  ```

  Identical to Catalyst's `Table`/`Column`/`ForeignKey` output today (the
  DDL machinery needs nothing new).
- A **non-PK** association table compiles the same way minus the
  `PRIMARY KEY` clause — no warning at `create_all`. ORM behavior with a
  non-unique secondary is deferred (duplicates would produce duplicate
  collection members / ambiguous deletes).
- `secondary=` accepts **both** a `Table` object and a **string table
  name** — the string resolves against the declarative metadata's table
  registry at mapper-configuration time (NOT the class registry; an
  unresolvable name raises `InvalidRequestError` "failed to locate a
  name"). A string that names a *class* does not resolve either.
- Bare `Column("a_id", ForeignKey("a.id"))` with no type → `NullType` →
  `CompileError` at DDL time. Types must be explicit on Core `Table`
  columns (declarative `mapped_column` infers them).

## Direction / uselist inference

- `secondary` presence forces `direction=MANYTOMANY` regardless of which
  side declares it; `back_populates` resolves the pair symmetrically.
- Default `uselist` is collection (`True`) on both sides.
- `uselist=False` + `secondary` is **allowed**: `direction=MANYTOMANY,
  uselist=False` — scalar m2m (verified configuring and round-tripping).

## Lazy loading

- `a.bs` emits ONE SELECT with an **implicit comma join** — no `JOIN`
  keyword, bind-first:

  ```sql
  SELECT b.id, b.name
  FROM b, assoc
  WHERE ? = assoc.a_id AND b.id = assoc.b_id
  ```

  Reverse direction mirrors it: `FROM a, assoc WHERE ? = assoc.b_id AND
  a.id = assoc.a_id`.
- Empty association still emits the SELECT and returns `[]`.
- Loaded collections cache on the instance (same as one-to-many).

## Flush ordering / cascades

- `a.bs = [b1, b2]` + `a2.bs.append(b3)` then flush:
  parent INSERTs (`RETURNING id`), then child INSERTs, then **one batched**
  `INSERT INTO assoc (a_id, b_id) VALUES (?, ?)` — executemany, params a
  list of tuples. Observed param order `[(2, 3), (1, 1), (1, 2)]` — UOW
  dependency ordering, not declaration order; do not assert a specific
  order beyond "all rows present in one statement".
- Appending a **transient** `b` to a persistent `a` cascades `save-update`:
  `b in session.new` → `True`; flush INSERTs `b` then the assoc row.
- `session.add(a)` cascades to `a.bs` members (default
  `merge,save-update`), same as one-to-many.

## Remove / delete-orphan

- `a.bs.remove(b)` + flush emits ONLY
  `DELETE FROM assoc WHERE assoc.a_id = ? AND assoc.b_id = ?` (parent key
  first). The `b` row is untouched. Both sides of `back_populates` clear
  at assignment time.
- **`delete-orphan` on many-to-many RAISES `ArgumentError` at mapper
  configure** unless `single_parent=True`:

  > For many-to-many relationship X.bs, delete-orphan cascade is normally
  > configured only on the "one" side of a one-to-many relationship ...
  > set the single_parent=True flag.

  The docs' claim that delete-orphan deletes the child row on m2m is only
  true with `single_parent=True`. With it:
  - `a.bs.pop()` + flush → `DELETE FROM assoc ...` THEN
    `DELETE FROM b WHERE b.id = ?` (assoc first, then child).
  - `session.delete(a)` + flush → assoc-row DELETEs (batched),
    `DELETE FROM a`, then child DELETEs (batched) — verified order
    `[assoc, parent, children]`.
- `single_parent=True` is required even though the relationship is a
  collection — it asserts each child has at most one parent *through this
  relationship*.

## Back-population

- `a.bs.append(b)` → `b in a.bs` and `a in b.as_` immediately, both
  directions. No FK values are stored on either object — association rows
  materialize only at flush. (Contrast with one-to-many where the FK
  column syncs at flush; m2m has no object-level FK at all.)

## Eager loading

- `selectinload(A.bs)`: TWO queries. Second query puts the ASSOCIATION
  table first in FROM and the grouping FK column first in SELECT:

  ```sql
  SELECT assoc.a_id, b.id, b.name
  FROM assoc JOIN b ON b.id = assoc.b_id
  WHERE assoc.a_id IN (?, ?)
  ```

  Does NOT require `.unique()`.
- `joinedload(A.bs)`: ONE query with a **parenthesized join chain**:

  ```sql
  SELECT a.id, a.name, b_1.id AS id_1, b_1.name AS name_1
  FROM a LEFT OUTER JOIN (assoc AS assoc_1 JOIN b AS b_1
    ON b_1.id = assoc_1.b_id) ON a.id = assoc_1.a_id
  ```

  Target columns aliased (`id_1`, `name_1`); the association table is
  aliased `assoc_1`. **Requires `.unique()`** on the result — same
  `InvalidRequestError` as collection joinedload in one-to-many.
- Loader strategies skip already-populated collections: tests must
  expunge/expire the seeded graph before asserting query counts.

## `secondary` edge cases

- **Extra columns**: plain `relationship(secondary=...)` writes only the
  two FK columns in the assoc INSERT; extra columns fall to
  `server_default`/NULL. Verified: `INSERT INTO assoc_extra (ea_id, eb_id)
  VALUES (?, ?)` with `extra` defaulted to `'dflt'`. The association-object
  pattern (mapping the assoc table itself with extra attributes) is
  deferred — it needs `secondary` + a mapped class, not just a `Table`.
- **Self-referential m2m** REQUIRES `primaryjoin`/`secondaryjoin` —
  without them configure raises `AmbiguousForeignKeysError` ("multiple
  foreign key paths linking the tables via secondary table"). Working
  form (friends table, `left_id`/`right_id` both FK to `person.id`):

  ```python
  friends = relationship(
      secondary=friendship,
      primaryjoin=id == friendship.c.left_id,
      secondaryjoin=id == friendship.c.right_id,
      backref="friended_by",
  )
  ```

  Lazy load emits the same implicit comma join bound on `left_id`:
  `FROM person, friendship WHERE ? = friendship.left_id AND person.id =
  friendship.right_id`.
- **`viewonly=True`**: loads through the association normally (same lazy
  SQL) but NEVER writes assoc rows — `append` is accepted in memory yet
  flush emits no assoc INSERT/DELETE.
- **Overlapping relationships** (two `relationship()`s on the same class
  sharing one `secondary` table): configure SUCCEEDS but emits
  `SAWarning` "relationship 'X.bs' will copy column t.id to column
  assoc.fk, which conflicts with relationship(s): 'X.cs' ..." per
  direction. Both relationships then write the same assoc rows.
- Non-PK association tables, `ForeignKeyConstraint` composite secondaries,
  `and_()` join clauses, `secondaryjoin` on non-self-referential
  relationships, and the association-object pattern are all deferred.

## Recommended implementation slice

Dependency order for the first m2m slice:

1. **`secondary=` kwarg on `relationship()`** accepting a `Table` object
   (string-name resolution can follow or land together — SA supports
   both; string resolution needs the metadata table registry, which
   `MetaData.tables` already provides).
2. **Join-condition inference for m2m**: direction `MANYTOMANY`,
   `uselist` collection default (`uselist=False` allowed), FKs discovered
   through the secondary table (both-FK-paths → ambiguous error for
   self-refs until `primaryjoin`/`secondaryjoin` land).
3. **Association-row UOW**: track added/removed (parent, child) pairs per
   relationship; flush emits batched `INSERT INTO assoc` / `DELETE FROM
   assoc WHERE fk1=? AND fk2=?`; save-update cascade reaches transient
   children. No object-level FK state.
4. **Lazy m2m load**: implicit comma-join SELECT (or an equivalent
   `JOIN` — SQL text differs from SA but semantics match; decide whether
   byte-parity of SQL text is a goal, since one-to-many asserts exact
   shapes).
5. **`selectinload`/`joinedload` through secondary**: selectin second
   query `FROM assoc JOIN target ... WHERE assoc.fk IN (...)`; joinedload
   parenthesized join + `.unique()` requirement.
6. **`viewonly`** (load-only, never writes) and the `delete-orphan` +
   `single_parent` gate (`ArgumentError` without it).

Defer: `primaryjoin`/`secondaryjoin`/`foreign_keys` overrides beyond the
self-referential minimum, association-object pattern, composite
`ForeignKeyConstraint` secondaries, `and_()`/custom clauses, `overlaps`
parameter, non-PK secondaries, remote-side ordering.

## Boundary notes / surprises

- The big surprise vs. the task brief: **`delete-orphan` on m2m is NOT
  allowed by default** — SA raises `ArgumentError` at configure unless
  `single_parent=True`. The "deletes the child row" behavior only holds
  with that flag.
- Lazy m2m SQL uses an implicit comma join (`FROM b, assoc WHERE ...`),
  not the `JOIN` keyword — different text shape from every other loader.
- `secondary="name"` string resolves against **table names in metadata**,
  and only when the `Table` was created on the same `MetaData` as the
  declarative base (a `Table` on a foreign `MetaData` fails with
  `InvalidRequestError: failed to locate a name`).
- Assoc INSERT param order follows UOW dependency ordering
  (`[(2,3),(1,1),(1,2)]` observed) — not declaration order.
