"""Relationship & loading parity against SQLAlchemy 2.1 (FK, lazy, eager, cascades).

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
(``docs/relationship-loading-ground-truth.md``). Common cases only: single
FK, one-to-many / many-to-one, ``back_populates``, default and
``delete-orphan`` cascades, ``lazy="select"``, ``joinedload``,
``selectinload``. Many-to-many, composite ``ForeignKeyConstraint``, custom
join conditions, inheritance, and ``Session.merge``/``expunge`` are deferred.

Key verified semantics encoded below:

- ``ForeignKey("parent.id")`` renders ``FOREIGN KEY(parent_id) REFERENCES
  parent (id)`` — one constraint per FK column (two FK columns to a composite
  PK produce two clauses, not a composite constraint).
- ``uselist`` is inferred from the ``Mapped`` annotation:
  ``Mapped[list["Child"]]`` → collection, ``Mapped["Parent"]`` → scalar. A
  ``unique=True`` FK also forces scalar. ``lazy`` defaults to ``"select"``;
  default cascade is ``merge,save-update`` (NOT ``delete``).
- Lazy collection access emits ONE ``SELECT ... WHERE ? = child.parent_id``
  per parent (bind-first, not ``IN``); lazy many-to-one emits
  ``SELECT ... WHERE parent.id = ?`` and returns ``None`` with no SQL when the
  FK is NULL. Loaded collections are cached — second access emits no SQL.
- ``joinedload`` on a collection emits a single ``LEFT OUTER JOIN`` query and
  REQUIRES ``.unique()`` on the result (``InvalidRequestError`` otherwise);
  ``selectinload`` emits a second ``WHERE fk IN (...)`` query and does not
  require ``unique()``. Plain ``select(Parent).join(Child)`` does NOT dedupe.
- ``session.add(parent)`` cascades ``save-update`` to ``parent.children``
  (``session.new`` grows to include them; parent INSERT precedes child
  INSERTs). ``session.delete(parent)`` under the default cascade NULLs the
  children's FK (``IntegrityError`` when the FK is NOT NULL) rather than
  deleting them; ``cascade="all, delete-orphan"`` deletes removed children
  and, on parent delete, children first then the parent.
- Object references back-populate at ASSIGNMENT time; FK column values sync
  at FLUSH time. ``c.parent = p`` populates ``p.children`` even when unloaded;
  ``p.children.append(c)`` sets ``c.parent`` immediately. WARNING: appending
  to an UNLOADED collection of a persistent parent triggers the lazy-load
  SELECT first and autoflushes the pending child with ``parent_id NULL`` →
  ``IntegrityError`` — the suite only appends to loaded/pending collections.
- Lazy-loaded and eager-loaded instances are the identity-map instances
  (``p.children[0] is session.get(Child, id)``).

Catalyst-side models are defined lazily inside each test because
``relationship``/``ForeignKey``/loader options do not exist yet; the whole
module skips until they land. SQLAlchemy models are module-level.
"""

from __future__ import annotations

import logging
from typing import Any

import catalyst
import catalyst.expression  # noqa: F401  (ensures submodule attribute exists)
import catalyst.orm  # noqa: F401
import pytest
import sqlalchemy as sa
from catalyst import (
    DeclarativeBase,
    IntegrityError,
    InvalidRequestError,
    Mapped,
    Session,
    create_engine,
    mapped_column,
    select,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.dialects import sqlite as sa_sqlite
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import joinedload as sa_joinedload
from sqlalchemy.orm import mapped_column as sa_mapped_column
from sqlalchemy.orm import relationship as sa_relationship
from sqlalchemy.orm import selectinload as sa_selectinload
from sqlalchemy.schema import CreateTable as SACreateTable


def _catalyst_symbol(name: str) -> Any:
    """Find ``name`` on whichever catalyst submodule exposes it."""
    for module in (catalyst, catalyst.expression, catalyst.orm):
        symbol = getattr(module, name, None)
        if symbol is not None:
            return symbol
    return None


_CATALYST_RELATIONSHIP = _catalyst_symbol("relationship")
_CATALYST_FOREIGN_KEY = _catalyst_symbol("ForeignKey")
_CATALYST_JOINEDLOAD = _catalyst_symbol("joinedload")
_CATALYST_SELECTINLOAD = _catalyst_symbol("selectinload")

_RELATIONSHIPS_MISSING = pytest.mark.skipif(
    _CATALYST_RELATIONSHIP is None or _CATALYST_FOREIGN_KEY is None,
    reason="catalyst relationship()/ForeignKey are not implemented yet",
)

pytestmark = _RELATIONSHIPS_MISSING

_LOADER_OPTIONS_MISSING = pytest.mark.skipif(
    _CATALYST_JOINEDLOAD is None or _CATALYST_SELECTINLOAD is None,
    reason="catalyst joinedload/selectinload loader options are not implemented yet",
)


# ---------------------------------------------------------------------------
# SQLAlchemy reference models (always defined; SA is a dev dependency)
# ---------------------------------------------------------------------------


class SABase(SADeclarativeBase):
    pass


class SAParent(SABase):
    __tablename__ = "rel_parent"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    children: SAMapped[list[SAChild]] = sa_relationship(back_populates="parent")


class SAChild(SABase):
    __tablename__ = "rel_child"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    parent_id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("rel_parent.id"))
    parent: SAMapped[SAParent] = sa_relationship(back_populates="children")


class SAOParent(SABase):
    __tablename__ = "rel_oparent"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    kids: SAMapped[list[SAOChild]] = sa_relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class SAOChild(SABase):
    __tablename__ = "rel_ochild"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    parent_id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("rel_oparent.id"))
    parent: SAMapped[SAOParent] = sa_relationship(back_populates="kids")


class SAProfileUser(SABase):
    __tablename__ = "rel_user"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    profile: SAMapped[SAProfile] = sa_relationship(uselist=False, back_populates="user")


class SAProfile(SABase):
    __tablename__ = "rel_profile"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    user_id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("rel_user.id"), unique=True)
    user: SAMapped[SAProfileUser] = sa_relationship(back_populates="profile")


# ---------------------------------------------------------------------------
# Catalyst model factories (defined lazily so collection stays clean)
# ---------------------------------------------------------------------------


def _catalyst_models() -> tuple[type[Any], type[Any]]:
    """Fresh Parent/Child declarative pair mirroring the SA reference models."""
    relationship = _CATALYST_RELATIONSHIP
    ForeignKey = _CATALYST_FOREIGN_KEY
    assert relationship is not None and ForeignKey is not None

    class CBase(DeclarativeBase):
        pass

    class CParent(CBase):
        __tablename__ = "rel_parent"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        children: Mapped[list[CChild]] = relationship(back_populates="parent")

    class CChild(CBase):
        __tablename__ = "rel_child"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        parent_id: Mapped[int] = mapped_column(ForeignKey("rel_parent.id"))
        parent: Mapped[CParent] = relationship(back_populates="children")

    return CParent, CChild


def _catalyst_orphan_models() -> tuple[type[Any], type[Any]]:
    relationship = _CATALYST_RELATIONSHIP
    ForeignKey = _CATALYST_FOREIGN_KEY
    assert relationship is not None and ForeignKey is not None

    class OBase(DeclarativeBase):
        pass

    class OParent(OBase):
        __tablename__ = "rel_oparent"
        id: Mapped[int] = mapped_column(primary_key=True)
        kids: Mapped[list[OChild]] = relationship(
            back_populates="parent", cascade="all, delete-orphan"
        )

    class OChild(OBase):
        __tablename__ = "rel_ochild"
        id: Mapped[int] = mapped_column(primary_key=True)
        parent_id: Mapped[int] = mapped_column(ForeignKey("rel_oparent.id"))
        parent: Mapped[OParent] = relationship(back_populates="kids")

    return OParent, OChild


def _catalyst_o2o_models() -> tuple[type[Any], type[Any]]:
    relationship = _CATALYST_RELATIONSHIP
    ForeignKey = _CATALYST_FOREIGN_KEY
    assert relationship is not None and ForeignKey is not None

    class UBase(DeclarativeBase):
        pass

    class CUser(UBase):
        __tablename__ = "rel_user"
        id: Mapped[int] = mapped_column(primary_key=True)
        profile: Mapped[CProfile] = relationship(uselist=False, back_populates="user")

    class CProfile(UBase):
        __tablename__ = "rel_profile"
        id: Mapped[int] = mapped_column(primary_key=True)
        user_id: Mapped[int] = mapped_column(ForeignKey("rel_user.id"), unique=True)
        user: Mapped[CUser] = relationship(back_populates="profile")

    return CUser, CProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engines() -> tuple[Any, Any, type[Any], type[Any]]:
    """(catalyst_engine, sa_engine, CParent, CChild) with both schemas created."""
    CParent, CChild = _catalyst_models()
    catalyst_engine = create_engine(":memory:", echo=True)
    CParent.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    return catalyst_engine, sa_engine, CParent, CChild


def _seed_parent_child(session: Any, Parent: type[Any], Child: type[Any], prefix: str = "") -> None:
    """Insert parent id=1 with children id=1,2 using explicit FK values."""
    session.add(Parent(id=1, name=f"{prefix}p1"))
    session.add(Parent(id=2, name=f"{prefix}p2"))
    session.add(Child(id=1, name=f"{prefix}c1", parent_id=1))
    session.add(Child(id=2, name=f"{prefix}c2", parent_id=1))
    session.add(Child(id=3, name=f"{prefix}c3", parent_id=2))
    session.commit()


def _seed_sa_parent_child(session: SASession, prefix: str = "") -> None:
    session.add(SAParent(id=1, name=f"{prefix}p1"))
    session.add(SAParent(id=2, name=f"{prefix}p2"))
    session.add(SAChild(id=1, name=f"{prefix}c1", parent_id=1))
    session.add(SAChild(id=2, name=f"{prefix}c2", parent_id=1))
    session.add(SAChild(id=3, name=f"{prefix}c3", parent_id=2))
    session.commit()


def _sa_statements(engine: Any) -> list[str]:
    statements: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    return statements


def _catalyst_selects(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "catalyst.engine" and "SELECT" in record.getMessage().upper()
    ]


# ---------------------------------------------------------------------------
# ForeignKey / DDL
# ---------------------------------------------------------------------------


def test_foreign_key_ddl_matches_sqlalchemy() -> None:
    """``mapped_column(ForeignKey("rel_parent.id"))`` compiles to the same
    ``FOREIGN KEY(parent_id) REFERENCES rel_parent (id)`` clause SA emits."""
    _CParent, CChild = _catalyst_models()
    compiled = CChild.__table__.compile_ddl()
    sa_compiled = SACreateTable(SAChild.__table__).compile(dialect=sa_sqlite.dialect())
    assert compiled.string == str(sa_compiled)
    assert "REFERENCES rel_parent (id)" in compiled.string


def test_foreign_key_column_round_trips() -> None:
    """SQLite does not enforce FKs by default; parity here is that the FK
    column accepts/round-trips values like any other column and the ORM
    populates it from the relationship (covered by flush tests below)."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)
            catalyst_fk = session.execute(
                sa_text("SELECT parent_id FROM rel_child WHERE id = 1")
            ).scalar_one()
            sa_fk = sa_session.execute(
                sa_text("SELECT parent_id FROM rel_child WHERE id = 1")
            ).scalar_one()
            assert catalyst_fk == sa_fk == 1
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# uselist inference / lazy loading
# ---------------------------------------------------------------------------


def test_lazy_collection_load_matches_sqlalchemy(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """First access to ``parent.children`` emits exactly one SELECT against
    the child table; the collection caches afterwards."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            parent = session.get(CParent, 1)
            children = list(parent.children)
            catalyst_sql = _catalyst_selects(caplog)
            assert [child.name for child in children] == ["c1", "c2"]
            child_selects = [s for s in catalyst_sql if "rel_child" in s]
            assert len(child_selects) == 1

            statements.clear()
            sa_parent = sa_session.get(SAParent, 1)
            sa_children = list(sa_parent.children)
            assert [child.name for child in sa_children] == ["c1", "c2"]
            sa_child_selects = [
                s
                for s in statements
                if "rel_child" in s and s.lstrip().upper().startswith("SELECT")
            ]
            assert len(sa_child_selects) == 1
            # Single-parent lazy load binds the FK, it does not batch with IN.
            assert " IN " not in sa_child_selects[0].upper()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_lazy_many_to_one_and_identity_map(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``child.parent`` lazy-loads the parent row; the loaded instances are the
    identity-map instances in BOTH directions."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            child = session.get(CChild, 1)
            parent = child.parent
            assert parent.name == "p1"
            catalyst_sql = _catalyst_selects(caplog)
            parent_selects = [s for s in catalyst_sql if "rel_parent" in s]
            assert len(parent_selects) == 1
            # Identity map: relationship loads return the same instances.
            assert child.parent is session.get(CParent, 1)
            assert parent.children[0] is child or child in parent.children

            statements.clear()
            sa_child = sa_session.get(SAChild, 1)
            sa_parent = sa_child.parent
            assert sa_parent.name == "p1"
            sa_parent_selects = [
                s
                for s in statements
                if "rel_parent" in s
                and s.lstrip().upper().startswith("SELECT")
                and "rel_child" not in s
            ]
            assert len(sa_parent_selects) == 1
            assert sa_child.parent is sa_session.get(SAParent, 1)
            assert sa_child in sa_parent.children
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_loaded_collection_does_not_requery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)
            caplog.set_level(logging.INFO, logger="catalyst.engine")

            parent = session.get(CParent, 1)
            assert len(list(parent.children)) == 2
            caplog.clear()
            assert len(list(parent.children)) == 2
            assert _catalyst_selects(caplog) == []

            sa_parent = sa_session.get(SAParent, 1)
            assert len(list(sa_parent.children)) == 2
            statements.clear()
            assert len(list(sa_parent.children)) == 2
            assert [
                s
                for s in statements
                if "rel_child" in s and s.lstrip().upper().startswith("SELECT")
            ] == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# back_populates / FK population
# ---------------------------------------------------------------------------


def test_back_populates_scalar_assignment() -> None:
    """``c.parent = p`` syncs ``p.children`` immediately (even unloaded) and
    the FK column value lands on flush."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cp = CParent(name="p1")
            cc = CChild(name="c1")
            session.add(cp)
            session.add(cc)
            cc.parent = cp
            assert cc.parent is cp
            assert cc in cp.children
            session.commit()
            assert cc.parent_id == cp.id

            sp = SAParent(name="p1")
            sc = SAChild(name="c1")
            sa_session.add(sp)
            sa_session.add(sc)
            sc.parent = sp
            assert sc.parent is sp
            assert sc in sp.children
            sa_session.commit()
            assert sc.parent_id == sp.id
            assert sc.parent_id == cc.parent_id
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_back_populates_collection_append() -> None:
    """``p.children.append(c)`` sets ``c.parent`` at assignment; the FK column
    syncs at flush (verified: INSERT/UPDATE carries parent_id)."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cp = CParent(name="p1")
            session.add(cp)
            session.flush()
            # Load the collection BEFORE the child is pending — appending to an
            # UNLOADED collection triggers the lazy-load SELECT whose autoflush
            # INSERTs the pending child with parent_id NULL (SA-verified
            # IntegrityError); that edge is documented, not asserted here.
            assert list(cp.children) == []
            cc = CChild(name="c1")
            session.add(cc)
            cp.children.append(cc)
            assert cc.parent is cp
            session.commit()
            assert cc.parent_id == cp.id

            sp = SAParent(name="p1")
            sa_session.add(sp)
            sa_session.flush()
            assert list(sp.children) == []
            sc = SAChild(name="c1")
            sa_session.add(sc)
            sp.children.append(sc)
            assert sc.parent is sp
            sa_session.commit()
            assert sc.parent_id == sp.id
            assert sc.parent_id == cc.parent_id
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_collection_assignment_replaces_membership() -> None:
    """``p.children = [c]`` back-populates ``c.parent`` immediately."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cp = CParent(name="p1")
            cc = CChild(name="c1")
            cp.children = [cc]
            assert cc.parent is cp
            session.add(cp)  # save-update cascade picks up cc
            session.commit()
            assert cc.parent_id == cp.id

            sp = SAParent(name="p1")
            sc = SAChild(name="c1")
            sp.children = [sc]
            assert sc.parent is sp
            sa_session.add(sp)
            sa_session.commit()
            assert sc.parent_id == sp.id
            assert sc.parent_id == cc.parent_id
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# cascades
# ---------------------------------------------------------------------------


def test_cascade_save_update_adds_children() -> None:
    """``session.add(parent)`` cascades save-update: ``session.new`` includes
    the children and flush inserts parent before children."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cp = CParent(name="p1")
            cp.children = [CChild(name="c1"), CChild(name="c2")]
            assert len(list(session.new)) == 0
            session.add(cp)
            assert len(list(session.new)) == 3
            session.commit()

            sp = SAParent(name="p1")
            sp.children = [SAChild(name="c1"), SAChild(name="c2")]
            assert len(list(sa_session.new)) == 0
            sa_session.add(sp)
            assert len(list(sa_session.new)) == 3
            sa_session.commit()

            assert (
                session.execute(
                    sa_text(f"SELECT count(*) FROM rel_child WHERE parent_id = {cp.id}")
                ).scalar_one()
                == 2
            )
            assert (
                sa_session.execute(
                    sa_text(f"SELECT count(*) FROM rel_child WHERE parent_id = {sp.id}")
                ).scalar_one()
                == 2
            )
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_delete_parent_default_cascade_nulls_fk() -> None:
    """Default cascade (``merge,save-update``) does NOT delete children:
    flush NULLs their FK → ``IntegrityError`` on a NOT NULL column, and the
    transaction rolls back leaving both parents and children intact."""
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_parent_child(session, CParent, CChild, prefix="x")
            _seed_sa_parent_child(sa_session, prefix="x")

            parent = session.get(CParent, 1)
            session.delete(parent)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            sa_parent = sa_session.get(SAParent, 1)
            sa_session.delete(sa_parent)
            with pytest.raises(sa.exc.IntegrityError):
                sa_session.commit()
            sa_session.rollback()

            assert session.execute(sa_text("SELECT count(*) FROM rel_child")).scalar_one() == 3
            assert sa_session.execute(sa_text("SELECT count(*) FROM rel_child")).scalar_one() == 3
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_delete_orphan_cascade() -> None:
    """``cascade="all, delete-orphan"``: removing a child from the loaded
    collection deletes it at flush; deleting the parent deletes children
    first, then the parent."""
    OParent, OChild = _catalyst_orphan_models()
    catalyst_engine = create_engine(":memory:")
    OParent.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            op = OParent()
            op.kids = [OChild(), OChild()]
            session.add(op)
            session.commit()
            assert op.id is not None

            sop = SAOParent()
            sop.kids = [SAOChild(), SAOChild()]
            sa_session.add(sop)
            sa_session.commit()

            # Remove one kid from each loaded collection -> orphan DELETE.
            op2 = session.get(OParent, op.id)
            assert len(list(op2.kids)) == 2
            op2.kids.pop()
            session.commit()
            assert session.execute(sa_text("SELECT count(*) FROM rel_ochild")).scalar_one() == 1

            sop2 = sa_session.get(SAOParent, sop.id)
            assert len(list(sop2.kids)) == 2
            sop2.kids.pop()
            sa_session.commit()
            assert sa_session.execute(sa_text("SELECT count(*) FROM rel_ochild")).scalar_one() == 1

            # Deleting the parent cascades to remaining children.
            session.delete(op2)
            session.commit()
            sa_session.delete(sop2)
            sa_session.commit()
            assert session.execute(sa_text("SELECT count(*) FROM rel_ochild")).scalar_one() == 0
            assert sa_session.execute(sa_text("SELECT count(*) FROM rel_ochild")).scalar_one() == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# eager loading
# ---------------------------------------------------------------------------


@_LOADER_OPTIONS_MISSING
def test_joinedload_emits_single_join(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``joinedload`` produces ONE ``LEFT OUTER JOIN`` query and requires
    ``.unique()`` for collection loads (SA-verified ``InvalidRequestError``)."""
    joinedload = _CATALYST_JOINEDLOAD
    assert joinedload is not None
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with (
            Session(catalyst_engine, expire_on_commit=False) as session,
            SASession(sa_engine, expire_on_commit=False) as sa_session,
        ):
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            parents = (
                session.scalars(select(CParent).options(joinedload(CParent.children)))
                .unique()
                .all()
            )
            assert sorted(p.name for p in parents) == ["p1", "p2"]
            by_name = {p.name: sorted(c.name for c in p.children) for p in parents}
            assert by_name == {"p1": ["c1", "c2"], "p2": ["c3"]}
            catalyst_sql = _catalyst_selects(caplog)
            assert len(catalyst_sql) == 1
            assert "JOIN" in catalyst_sql[0].upper()

            statements.clear()
            sa_parents = (
                sa_session.scalars(sa.select(SAParent).options(sa_joinedload(SAParent.children)))
                .unique()
                .all()
            )
            assert sorted(p.name for p in sa_parents) == ["p1", "p2"]
            sa_by_name = {p.name: sorted(c.name for c in p.children) for p in sa_parents}
            assert sa_by_name == by_name
            assert len(statements) == 1
            assert "LEFT OUTER JOIN" in statements[0].upper()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_LOADER_OPTIONS_MISSING
def test_joinedload_collection_requires_unique() -> None:
    """SA raises ``InvalidRequestError`` when a collection joinedload result is
    consumed without ``.unique()``; catalyst must match."""
    joinedload = _CATALYST_JOINEDLOAD
    assert joinedload is not None
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)
            with pytest.raises(InvalidRequestError):
                session.scalars(select(CParent).options(joinedload(CParent.children))).all()
            with pytest.raises(sa.exc.InvalidRequestError):
                sa_session.scalars(
                    sa.select(SAParent).options(sa_joinedload(SAParent.children))
                ).all()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_LOADER_OPTIONS_MISSING
def test_selectinload_emits_batch_in_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``selectinload`` runs the parent SELECT then ONE ``WHERE fk IN (...)``
    batch query; no ``.unique()`` required."""
    selectinload = _CATALYST_SELECTINLOAD
    assert selectinload is not None
    catalyst_engine, sa_engine, CParent, CChild = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with (
            Session(catalyst_engine, expire_on_commit=False) as session,
            SASession(sa_engine, expire_on_commit=False) as sa_session,
        ):
            _seed_parent_child(session, CParent, CChild)
            _seed_sa_parent_child(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            parents = session.scalars(select(CParent).options(selectinload(CParent.children))).all()
            by_name = {p.name: sorted(c.name for c in p.children) for p in parents}
            assert by_name == {"p1": ["c1", "c2"], "p2": ["c3"]}
            catalyst_sql = _catalyst_selects(caplog)
            assert len(catalyst_sql) == 2
            assert " IN " in catalyst_sql[1].upper()
            assert "rel_child" in catalyst_sql[1]

            statements.clear()
            sa_parents = sa_session.scalars(
                sa.select(SAParent).options(sa_selectinload(SAParent.children))
            ).all()
            sa_by_name = {p.name: sorted(c.name for c in p.children) for p in sa_parents}
            assert sa_by_name == by_name
            sa_selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            assert len(sa_selects) == 2
            assert " IN " in sa_selects[1].upper()
            assert "rel_child" in sa_selects[1]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# one-to-one (unique FK -> scalar uselist inference)
# ---------------------------------------------------------------------------


def test_one_to_one_scalar_relationship() -> None:
    """``unique=True`` FK + scalar ``Mapped`` annotations infer ``uselist=False``
    on BOTH sides; assignment back-populates both directions."""
    CUser, CProfile = _catalyst_o2o_models()
    catalyst_engine = create_engine(":memory:")
    CUser.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cu = CUser()
            cp = CProfile()
            cu.profile = cp  # scalar assignment back-populates cp.user
            assert cp.user is cu
            session.add(cu)  # save-update cascade reaches cp
            session.commit()
            assert cp.user_id == cu.id

            su = SAProfileUser()
            sp = SAProfile()
            su.profile = sp
            assert sp.user is su
            sa_session.add(su)
            sa_session.commit()
            assert sp.user_id == su.id
            assert sp.user_id == cp.user_id

            # Lazy scalar load returns the related instance.
            got_user = session.get(CUser, cu.id)
            assert got_user.profile.id == cp.id
            got_profile = sa_session.get(SAProfile, sp.id)
            assert got_profile.user.id == su.id
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()
