# Relationship & loading ground truth (SQLAlchemy 2.1.8, sqlite)

Empirically verified 2026-11-11 against `sqlalchemy==2.1.8` + sqlite3 in
`.venv`, via `before_cursor_execute` SQL capture. Proving suite:
`tests/test_relationships.py` (skip-gated on `catalyst.relationship` +
`catalyst.ForeignKey`). Common cases only — custom join conditions,
`secondary`/many-to-many, inheritance, and polymorphic loading are deferred.

Model used throughout:

```python
class Parent(Base):
    __tablename__ = "parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    children: Mapped[list["Child"]] = relationship(back_populates="parent")

class Child(Base):
    __tablename__ = "child"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    parent_id: Mapped[int] = mapped_column(ForeignKey("parent.id"))
    parent: Mapped["Parent"] = relationship(back_populates="children")
```

## ForeignKey / DDL

- `ForeignKey("parent.id")` works on `mapped_column(...)` (declarative) and
  `Column(...)` (Core). String target is resolved lazily against table names.
- SQLite DDL renders one constraint **per FK column** (two FK columns to the
  same composite PK produce two separate `FOREIGN KEY` clauses, not one
  composite constraint — a true composite needs `ForeignKeyConstraint`,
  deferred):

  ```sql
  CREATE TABLE child (
      id INTEGER NOT NULL,
      name VARCHAR NOT NULL,
      parent_id INTEGER NOT NULL,
      PRIMARY KEY (id),
      FOREIGN KEY(parent_id) REFERENCES parent (id)
  )
  ```

- `unique=True` on the FK column + `Mapped["Profile"]` (scalar annotation)
  infers `uselist=False` — verified: `Profile.user` got `uselist=False`
  with no explicit `uselist=`.

## relationship() resolution & inference

- String forward refs (`Mapped[list["Child"]]`, `Mapped["Parent"]`) resolve via
  the declarative class registry at mapper-configuration time.
- `uselist` inference from the annotation: `Mapped[list["X"]]` → collection
  (`uselist=True`); `Mapped["X"]` / `Mapped["X | None"]` → scalar
  (`uselist=False`).
- `lazy` default is `'select'` on both sides.
- Default `cascade` is `CascadeOptions('merge,save-update')` — **not**
  `delete`. `cascade="all, delete-orphan"` is opt-in.
- `backref="name"` works but the attribute only materializes after
  `configure_mappers()` (first attribute access triggers it); `back_populates`
  is the explicit two-sided form.

## Lazy loading (`lazy="select"` default)

- `parent.children` (collection): one SELECT per parent access,
  `SELECT child.id, child.name, child.parent_id FROM child WHERE ? =
  child.parent_id` — bind-first, **not** `IN`, for a single parent. Result is
  an `InstrumentedList` (subclasses `list`; `append`/`remove`/`pop` instrumented).
  Second access emits **zero** SQL (collection cached on the instance).
- `child.parent` (many-to-one): `SELECT parent.id, parent.name FROM parent
  WHERE parent.id = ?`. If `parent_id` is NULL → returns `None` with **no
  SQL**.
- `lazy="raise"`: access raises `InvalidRequestError` —
  `"'Parent.children' is not available due to lazy='raise'"`.
- `noload(...)`: collection access returns `[]` (empty `InstrumentedList`)
  with no SQL; scalar many-to-one returns `None`.

## Eager loading

- `select(Parent).options(joinedload(Parent.children))` emits ONE query:

  ```sql
  SELECT parent.id, parent.name,
         child_1.id AS id_1, child_1.name AS name_1, child_1.parent_id
  FROM parent LEFT OUTER JOIN child AS child_1
    ON parent.id = child_1.parent_id
  ```

  Child columns are aliased (`id_1`, `name_1`; the FK column keeps its name).
- **`session.scalars(select(Parent).options(joinedload(Parent.children)))`
  RAISES `InvalidRequestError`**: "The unique() method must be invoked on
  this Result, as it contains results that include joined eager loads against
  collections". Correct form: `.scalars(...).unique().all()` (or
  `session.execute(...).unique()`). `subqueryload` on a collection has the
  same requirement.
- `selectinload(Parent.children)`: TWO queries. First the plain parent SELECT;
  then `SELECT child.parent_id, child.id, child.name FROM child WHERE
  child.parent_id IN (?, ?)` — FK column first in the SELECT list for
  client-side grouping. **Does not require `.unique()`**.
- `subqueryload`: parent SELECT, then
  `SELECT child.id AS child_id, ..., anon_1.parent_id FROM (SELECT parent.id
  AS parent_id FROM parent) AS anon_1 JOIN child ON anon_1.parent_id =
  child.parent_id`. Requires `.unique()` for collections.
- `session.scalars(select(Parent).join(Child))` does **NOT** deduplicate —
  returns one row per join match (`[1, 1, 2]`). Dedup is opt-in via
  `.unique()`.

## Cascades

- `session.add(parent)` cascades `save-update` to `parent.children` objects:
  `len(session.new)` goes 0 → 3 after adding a parent with two children.
  Flush ordering: parent INSERT first, then child INSERTs with `parent_id`
  populated.
- `session.delete(parent)` with the DEFAULT cascade does **not** delete
  children. Instead each child's `parent` is nulled → flush emits
  `UPDATE child SET parent_id=? WHERE child.id=?` with `None` →
  `IntegrityError` (NOT NULL) on a non-nullable FK. With a nullable FK the
  children survive with `parent_id NULL` (orphans).
- `cascade="all, delete-orphan"`: removing a child from the loaded collection
  (`pop()`/`remove()`) marks it deleted → `DELETE FROM ochild WHERE
  ochild.id = ?` at flush. `session.delete(parent)` then deletes children
  first (`DELETE FROM ochild ...` per child) then the parent.
- `merge` cascade exists in the default set but `Session.merge` is a later
  slice — unprobed.

## Back-population & FK synchronization

- Object references sync **at assignment time**; FK column values sync **at
  flush time**.
  - `c.parent = p` → `c.parent is p` immediately, and `c in p.children` is
    True even when the collection was never loaded (back-populates into the
    unloaded collection). `c.parent_id` stays `None`/stale until flush; the
    INSERT/UPDATE then carries the parent's PK.
  - `p.children.append(c)` (collection already loaded) → `c.parent is p`
    immediately; `c.parent_id` syncs at flush (`UPDATE child SET parent_id=?
    WHERE child.id=?` or INSERT param).
  - `p.children = [c]` (assignment) → `c.parent is p` immediately, FK at flush.
  - `kids.remove(c)` → `c.parent is None` immediately; `c.parent_id` still
    holds the old value until flush.
- **Surprise**: appending to an *unloaded* collection of a persistent parent
  triggers the lazy-load SELECT first; autoflush then INSERTs the pending
  child with `parent_id NULL` (the backref hasn't fired yet) →
  `IntegrityError` on a NOT NULL FK. Load the collection (or use a pending
  parent) before appending.
- Moving a child `p2.children.append(c)` where `c` belonged to `p1`:
  `c.parent is p2` immediately, removed from `p1.children`, flush emits
  `UPDATE child SET parent_id=? WHERE child.id=?`.

## Identity map

- Lazy-loaded related instances ARE the identity-map instances:
  `p.children[0] is session.get(Child, c.id)` → `True`. Same for
  `child.parent is session.get(Parent, 1)`.
- Eager-loaded (joinedload/selectinload) instances likewise reconcile with
  the identity map.

## Recommended implementation slice

Minimum API surface for the first relationships slice, in dependency order:

1. **`ForeignKey`** (Core `Column`/`mapped_column` arg) + DDL rendering —
   `FOREIGN KEY(col) REFERENCES tbl (col)` per FK column. Prerequisite for
   everything else; alone it unlocks the DDL parity test.
2. **`relationship()`** on `DeclarativeBase` — target resolution from the
   `Mapped[...]` annotation (string forward refs MUST be deferred: the
   current `_map_declarative_class` calls `get_type_hints` eagerly at class
   creation, which cannot resolve `"Child"` before it exists — the impl needs
   SA-style deferred mapper configuration or a class registry), `uselist`
   inference (`list[...]` → collection, scalar/`X | None` → scalar, `unique`
   FK → scalar), `back_populates`/`backref`, attribute instrumentation
   (collection type, append/remove/setitem events), and cascade
   `save-update`/`delete`/`delete-orphan` names.
3. **Lazy loading** (`lazy="select"`): many-to-one SELECT-by-PK, collection
   SELECT-by-FK, NULL-FK → `None`/no-SQL, instance caching, identity-map
   reconciliation.
4. **FK synchronization at flush**: object assignment back-populates
   immediately; FK column values materialize during flush dependency
   processing (parent INSERT before child INSERT; `UPDATE ... SET fk` on
   move). The unloaded-collection-append autoflush trap must be preserved or
   deliberately deviated (documented above).
5. **`joinedload` + `selectinload`** loader options on `Select.options()`
   with `.unique()` requirement parity for collection joinedloads.

Defer to later slices: `subqueryload`/`raiseload`/`noload`/`immediateload`,
`lazy="joined"/"raise"/"write"/"dynamic"`, `secondary`/many-to-many,
`ForeignKeyConstraint` composites, custom `primaryjoin`/`foreign_key`,
self-referential, inheritance, `merge`/`expunge`, relationship query
expressions (`Parent.children.any()`), async ORM (`AsyncSession` lazy-load
semantics differ — `lazy="raise"` default, `MissingGreenlet`).

## Boundary notes / deferred

- `lazy="joined"`, `lazy="raise"`/`noload`/`selectin"` per-relationship,
  `raiseload`/`noload`/`selectinload`/`subqueryload`/`immediateload` options
  beyond the three probed, `lazy="write"`, `dynamic` relationships.
- `secondary`/many-to-many, `ForeignKeyConstraint` composite FKs, custom
  `primaryjoin`/`foreign_key` annotations, self-referential, inheritance,
  `with_parent`, `of_type`, `contains_eager`, `Load`/`lazyload_from` options.
- `Session.merge`, `expunge`, `bulk_*`, relationship events
  (`@event.listens_for`), `InstrumentedAttribute` query expressions
  (`Parent.children.any(...)`), `of_type`/`with_loader_criteria`.
- Async (`AsyncSession` + `lazy="raise"` default, `selectinload`/`joinedload`
  still work, lazy access raises `MissingGreenlet`-style errors) — later
  slice; Catalyst async ORM surface doesn't exist yet.
