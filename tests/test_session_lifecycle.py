"""Session merge / expunge / bulk / lifecycle parity against SQLAlchemy 2.1.

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
(``docs/session-merge-expunge-ground-truth.md``). The whole module skips
until ``Session.merge`` exists; expunge/bulk/lifecycle land in the same M4
slice.

Key verified semantics encoded below:

- ``session.merge(obj)`` returns the persistent instance belonging to THIS
  session — almost never the passed object. Transient/detached input with an
  existing PK emits ``SELECT ... WHERE id = ?`` and copies state onto the
  identity-map instance; the source stays transient/detached. Unknown PK →
  merged copy is pending (INSERT keeps the supplied PK); ``id=None`` →
  pending, PK assigned on flush. Cross-session persistent input is copied
  into the target session; the source session is untouched. Merge ALWAYS
  autoflushes first — even with ``autoflush=False``. ``merge(load=False)``
  rejects transient and dirty objects with ``InvalidRequestError``; a clean
  detached object merges without a SELECT, and a ghost PK surfaces as
  ``ObjectDeletedError`` at flush. Default ``"merge,save-update"`` cascades
  recurse the graph (children merged by PK, new children INSERTed with FK
  populated).
- ``session.expunge(obj)``: persistent → detached (attrs readable, later
  ``get`` returns a NEW instance); pending → transient (never inserted);
  pending-delete → removed from ``deleted`` (row survives); does NOT
  autoflush; absent/transient input → ``InvalidRequestError``. Re-``add`` of
  a detached object resurrects it as persistent and flushes kept values as
  UPDATE. ``expunge_all`` empties the identity map. Detached + expired
  attribute access raises (``DetachedInstanceError`` in SA).
- Bulk: only ``bulk_insert_mappings`` / ``bulk_update_mappings`` /
  ``bulk_save_objects`` exist — NO ``bulk_delete_mappings``.
  ``bulk_insert_mappings`` returns ``None``, materializes client-side
  defaults, and never touches the session. ``bulk_update_mappings`` requires
  a PK per row (``InvalidRequestError`` otherwise), emits one single-row
  UPDATE per mapping, raises ``StaleDataError`` on unmatched PKs, and does
  NOT synchronize the session (in-memory instances stay stale). Bulk methods
  do NOT autoflush pending/dirty work. ``bulk_save_objects`` bypasses the
  UOW: objects never enter ``session.new`` and PKs are not written back.
- ``session.execute(update(Model)...)`` / ``delete(Model)``: default
  ``synchronize_session="auto"`` evaluates simple WHEREs — matching
  in-session instances are updated in-memory without becoming dirty;
  ``False`` leaves them stale; ``"fetch"`` uses ``RETURNING id``. ORM
  delete moves the in-session instance to the deleted state.
- Lifecycle: ``close()`` rolls back pending work, detaches everything
  (pending → transient with values retained), and is idempotent. SA
  Sessions are REUSABLE after ``close()`` — Catalyst's permanent ``_closed``
  flag is a known gap (``test_close_then_reuse_session`` xfails until fixed).
  ``get(..., populate_existing=True)`` re-SELECTs the identity-map instance.
"""

from __future__ import annotations

import inspect as pyinspect
from typing import Any

import pytest
import sqlalchemy as sa
from catalyst import (
    DeclarativeBase,
    InvalidRequestError,
    Mapped,
    Session,
    create_engine,
    mapped_column,
)
from catalyst import update as catalyst_update
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column
from sqlalchemy.orm import relationship as sa_relationship

_LIFECYCLE_MISSING = pytest.mark.skipif(
    not hasattr(Session, "merge"),
    reason="catalyst Session merge/expunge/bulk/lifecycle slice is not implemented yet",
)

pytestmark = _LIFECYCLE_MISSING

_POPULATE_MISSING = pytest.mark.skipif(
    len(pyinspect.signature(Session.get).parameters) < 3,
    reason="catalyst Session.get does not support populate_existing yet",
)


# ---------------------------------------------------------------------------
# Models — flat entity + parent/child relationship graph
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "lc_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    score: Mapped[int] = mapped_column(default=0)


class SABase(SADeclarativeBase):
    pass


class SAUser(SABase):
    __tablename__ = "lc_users"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    score: SAMapped[int] = sa_mapped_column(default=0)


class SAParent(SABase):
    __tablename__ = "lc_parent"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    children: SAMapped[list[SAChild]] = sa_relationship(back_populates="parent")


class SAChild(SABase):
    __tablename__ = "lc_child"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    parent_id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("lc_parent.id"))
    parent: SAMapped[SAParent] = sa_relationship(back_populates="children")


def _catalyst_engine() -> Any:
    engine = create_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine


def _sa_engine() -> Any:
    engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(engine)
    return engine


def _seed_user(engine: Any, sa_side: bool) -> None:
    if sa_side:
        with SASession(engine) as session:
            session.add(SAUser(id=1, name="orig", score=1))
            session.commit()
    else:
        with Session(engine) as session:
            session.add(User(id=1, name="orig", score=1))
            session.commit()


def _db_names(engine: Any, table: str) -> list[tuple[Any, ...]]:
    with engine.connect() as connection:
        return [
            tuple(row)
            for row in connection.execute(sa.text(f"SELECT * FROM {table} ORDER BY id")).all()
        ]


# ---------------------------------------------------------------------------
# merge()
# ---------------------------------------------------------------------------


def test_merge_transient_existing_pk_copies_state() -> None:
    """Transient obj with an existing PK: merge SELECTs by PK, returns a NEW
    persistent instance, copies state; the source stays transient."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session:
            obj = User(id=1, name="merged-name", score=99)
            merged = session.merge(obj)
            assert merged is not obj
            assert session.get(User, 1) is merged
            assert merged.name == "merged-name"
            assert merged.score == 99
            assert obj not in session.new
            session.commit()
        with SASession(sa_engine) as sa_session:
            sa_obj = SAUser(id=1, name="merged-name", score=99)
            sa_merged = sa_session.merge(sa_obj)
            assert sa_merged is not sa_obj
            assert sa_session.get(SAUser, 1) is sa_merged
            assert sa_merged.name == "merged-name"
            assert sa_merged.score == 99
            assert sa_obj not in sa_session.new
            sa_session.commit()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "merged-name", 99)]
        assert _db_names(sa_engine, "lc_users") == [(1, "merged-name", 99)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_transient_unknown_pk_inserts_with_supplied_pk() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            merged = session.merge(User(id=999, name="ghost", score=5))
            assert merged in session.new
            session.commit()
            assert merged.id == 999
        with SASession(sa_engine) as sa_session:
            sa_merged = sa_session.merge(SAUser(id=999, name="ghost", score=5))
            assert sa_merged in sa_session.new
            sa_session.commit()
            assert sa_merged.id == 999
        assert _db_names(catalyst_engine, "lc_users") == [(999, "ghost", 5)]
        assert _db_names(sa_engine, "lc_users") == [(999, "ghost", 5)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_transient_none_pk_assigns_on_flush() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            merged = session.merge(User(name="nopk", score=2))
            assert merged in session.new
            session.flush()
            assert merged.id == 1
        with SASession(sa_engine) as sa_session:
            sa_merged = sa_session.merge(SAUser(name="nopk", score=2))
            assert sa_merged in sa_session.new
            sa_session.flush()
            assert sa_merged.id == 1
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_persistent_from_other_session() -> None:
    """A persistent object in session A merges into open session B: B gets its
    own instance carrying A's (dirty) state; A is untouched."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        s1 = Session(catalyst_engine)
        s2 = Session(catalyst_engine)
        sa_s1 = SASession(sa_engine)
        sa_s2 = SASession(sa_engine)
        try:
            a = s1.get(User, 1)
            a.name = "from-a"
            sa_a = sa_s1.get(SAUser, 1)
            sa_a.name = "from-a"
            m = s2.merge(a)
            sa_m = sa_s2.merge(sa_a)
            assert m is not a
            assert s2.get(User, 1) is m
            assert m.name == "from-a"
            assert m in s2.dirty
            assert sa_m is not sa_a
            assert sa_s2.get(SAUser, 1) is sa_m
            assert sa_m.name == "from-a"
            assert sa_m in sa_s2.dirty
            # Source sessions keep their own dirty state.
            assert a in s1.dirty
            assert sa_a in sa_s1.dirty
            s2.commit()
            sa_s2.commit()
        finally:
            s1.close()
            s2.close()
            sa_s1.close()
            sa_s2.close()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "from-a", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "from-a", 1)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_detached_object() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as s1, SASession(sa_engine) as sa_s1:
            d = s1.get(User, 1)
            sa_d = sa_s1.get(SAUser, 1)
            s1.expunge(d)
            sa_s1.expunge(sa_d)
            d.name = "detached-edit"
            sa_d.name = "detached-edit"
        with Session(catalyst_engine) as s2, SASession(sa_engine) as sa_s2:
            m = s2.merge(d)
            sa_m = sa_s2.merge(sa_d)
            assert m is not d
            assert m.name == "detached-edit"
            assert m in s2.dirty
            assert sa_m is not sa_d
            assert sa_m.name == "detached-edit"
            assert sa_m in sa_s2.dirty
            s2.commit()
            sa_s2.commit()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "detached-edit", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "detached-edit", 1)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_pending_object_autoflushes_and_returns_same() -> None:
    """merge() autoflushes pending work first (even with autoflush=False) and
    returns the SAME object once it is persistent."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            pending = User(name="pending-obj", score=1)
            sa_pending = SAUser(name="pending-obj", score=1)
            session.add(pending)
            sa_session.add(sa_pending)
            merged = session.merge(pending)
            sa_merged = sa_session.merge(sa_pending)
            assert merged is pending
            assert sa_merged is sa_pending
            assert pending.id is not None
            assert sa_pending.id is not None
            assert pending not in session.new
            assert sa_pending not in sa_session.new
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_autoflushes_even_when_autoflush_false() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with (
            Session(catalyst_engine, autoflush=False) as session,
            SASession(sa_engine, autoflush=False) as sa_session,
        ):
            pending = User(name="unflushed", score=1)
            sa_pending = SAUser(name="unflushed", score=1)
            session.add(pending)
            sa_session.add(sa_pending)
            merged = session.merge(User(id=1, name="merged-over", score=2))
            sa_merged = sa_session.merge(SAUser(id=1, name="merged-over", score=2))
            # Verified SQLAlchemy 2.1.8: merge's load path honors autoflush —
            # with autoflush=False the pending INSERT is NOT flushed.
            assert pending.id is None
            assert sa_pending.id is None
            assert merged.id == 1
            assert sa_merged.id == 1
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_load_false_rejects_transient_and_dirty() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            with pytest.raises(InvalidRequestError):
                session.merge(User(id=1, name="x", score=0), load=False)
            with pytest.raises(sa.exc.InvalidRequestError):
                sa_session.merge(SAUser(id=1, name="x", score=0), load=False)
            # Dirty objects are also rejected — but only when the identity is
            # not already in the session's map.  Verified SQLAlchemy 2.1.8:
            # a dirty object attached to a DIFFERENT session (or detached
            # dirty) raises InvalidRequestError; an object dirty in THIS
            # session merges onto itself without raising.
            s_other = Session(catalyst_engine)
            sa_other = SASession(sa_engine)
            try:
                u = s_other.get(User, 1)
                u.name = "dirty"
                sa_u = sa_other.get(SAUser, 1)
                sa_u.name = "dirty"
                with pytest.raises(InvalidRequestError):
                    session.merge(u, load=False)
                with pytest.raises(sa.exc.InvalidRequestError):
                    sa_session.merge(sa_u, load=False)
            finally:
                s_other.close()
                sa_other.close()
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_load_false_clean_detached() -> None:
    """Clean detached + load=False merges without loading; a ghost PK then
    fails at flush with ObjectDeletedError (SA) / equivalent."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as s1, SASession(sa_engine) as sa_s1:
            d = s1.get(User, 1)
            sa_d = sa_s1.get(SAUser, 1)
            s1.expunge(d)
            sa_s1.expunge(sa_d)
        with Session(catalyst_engine) as s2, SASession(sa_engine) as sa_s2:
            m = s2.merge(d, load=False)
            sa_m = sa_s2.merge(sa_d, load=False)
            assert m is not d
            assert m.name == "orig"
            assert sa_m is not sa_d
            assert sa_m.name == "orig"
            s2.commit()
            sa_s2.commit()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "orig", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "orig", 1)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_merge_unmapped_raises() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            with pytest.raises(Exception) as catalyst_error:
                session.merge(object())
            with pytest.raises(sa.orm.exc.UnmappedInstanceError) as sa_error:
                sa_session.merge(object())
            assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# merge() cascades over relationships
# ---------------------------------------------------------------------------


def _catalyst_graph_models() -> tuple[Any, Any]:
    """Define catalyst Parent/Child on a fresh base (lazy, like
    test_relationships, so the suite collects before the slice lands)."""
    from catalyst import ForeignKey, relationship

    class GBase(DeclarativeBase):
        pass

    class GParent(GBase):
        __tablename__ = "lc_gparent"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        children: Mapped[list[GChild]] = relationship(back_populates="parent")

    class GChild(GBase):
        __tablename__ = "lc_gchild"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        parent_id: Mapped[int] = mapped_column(ForeignKey("lc_gparent.id"))
        parent: Mapped[GParent] = relationship(back_populates="children")

    return GBase, (GParent, GChild)


def test_merge_cascades_to_relationship_graph() -> None:
    """merge(detached_parent) merges children by PK (UPDATE for changed) and
    INSERTs brand-new transient children with the FK populated."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        GBase, (GParent, GChild) = _catalyst_graph_models()
        GBase.metadata.create_all(catalyst_engine)

        # Seed identical graphs: parent 1 with children 1,2.
        with Session(catalyst_engine) as session:
            p = GParent(id=1, name="par")
            p.children = [GChild(id=1, name="kid1"), GChild(id=2, name="kid2")]
            session.add(p)
            session.commit()
        with SASession(sa_engine) as sa_session:
            sa_p = SAParent(id=1, name="par")
            sa_p.children = [SAChild(id=1, name="kid1"), SAChild(id=2, name="kid2")]
            sa_session.add(sa_p)
            sa_session.commit()

        # Detach both graphs with loaded state; edit child 1, add a new child.
        with Session(catalyst_engine) as s1:
            src = s1.get(GParent, 1)
            _ = src.children
            src.children[0].name = "kid1-edit"
            src.children.append(GChild(name="new-kid"))
            s1.expunge_all()
        with SASession(sa_engine) as sa_s1:
            sa_src = sa_s1.get(SAParent, 1)
            _ = sa_src.children
            sa_src.children[0].name = "kid1-edit"
            sa_src.children.append(SAChild(name="new-kid"))
            sa_s1.expunge_all()

        with Session(catalyst_engine) as s2:
            m = s2.merge(src)
            assert m is not src
            assert [c.name for c in m.children] == ["kid1-edit", "kid2", "new-kid"]
            s2.flush()
            # New child got an autoincrement PK and the parent FK.
            new_child = m.children[2]
            assert new_child.id is not None
            assert new_child.parent_id == 1
        with SASession(sa_engine) as sa_s2:
            sa_m = sa_s2.merge(sa_src)
            assert [c.name for c in sa_m.children] == ["kid1-edit", "kid2", "new-kid"]
            sa_s2.flush()
            sa_new = sa_m.children[2]
            assert sa_new.id is not None
            assert sa_new.parent_id == 1

        catalyst_children = _db_names(catalyst_engine, "lc_gchild")
        sa_children = _db_names(sa_engine, "lc_child")
        # Normalize: catalyst child ids may differ in count order only by PK.
        assert [(r[1], r[2]) for r in catalyst_children] == [(r[1], r[2]) for r in sa_children]
        assert _db_names(catalyst_engine, "lc_gparent") == _db_names(sa_engine, "lc_parent")
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# expunge() / expunge_all()
# ---------------------------------------------------------------------------


def test_expunge_persistent_returns_new_instance_on_get() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u = session.get(User, 1)
            sa_u = sa_session.get(SAUser, 1)
            assert session.expunge(u) is None
            assert sa_session.expunge(sa_u) is None
            # Attributes stay readable on the detached object.
            assert u.name == "orig" and u.id == 1
            assert sa_u.name == "orig" and sa_u.id == 1
            # get() now returns a different instance.
            assert session.get(User, 1) is not u
            assert sa_session.get(SAUser, 1) is not sa_u
            session.commit()
            sa_session.commit()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_expunge_pending_becomes_transient_never_inserted() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            p = User(name="pending-x", score=9)
            sa_p = SAUser(name="pending-x", score=9)
            session.add(p)
            sa_session.add(sa_p)
            session.expunge(p)
            sa_session.expunge(sa_p)
            assert p not in session.new
            assert sa_p not in sa_session.new
            session.flush()
            sa_session.flush()
            assert _db_names(catalyst_engine, "lc_users") == []
            assert _db_names(sa_engine, "lc_users") == []
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_expunge_dirty_keeps_values_and_readd_updates() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u = session.get(User, 1)
            sa_u = sa_session.get(SAUser, 1)
            u.name = "dirty-edit"
            sa_u.name = "dirty-edit"
            session.expunge(u)
            sa_session.expunge(sa_u)
            assert u.name == "dirty-edit"
            assert sa_u.name == "dirty-edit"
            # Re-adding resurrects as persistent; flush emits UPDATE.
            session.add(u)
            sa_session.add(sa_u)
            session.flush()
            sa_session.flush()
            session.commit()
            sa_session.commit()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "dirty-edit", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "dirty-edit", 1)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_expunge_absent_or_twice_raises() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            t = User(name="transient-x")
            sa_t = SAUser(name="transient-x")
            with pytest.raises(InvalidRequestError):
                session.expunge(t)
            with pytest.raises(sa.exc.InvalidRequestError):
                sa_session.expunge(sa_t)
            u = session.get(User, 1)
            sa_u = sa_session.get(SAUser, 1)
            session.expunge(u)
            sa_session.expunge(sa_u)
            with pytest.raises(InvalidRequestError):
                session.expunge(u)
            with pytest.raises(sa.exc.InvalidRequestError):
                sa_session.expunge(sa_u)
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_expunge_pending_delete_row_survives() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u = session.get(User, 1)
            sa_u = sa_session.get(SAUser, 1)
            session.delete(u)
            sa_session.delete(sa_u)
            session.expunge(u)
            sa_session.expunge(sa_u)
            session.flush()
            sa_session.flush()
            assert session.get(User, 1) is not None
            assert sa_session.get(SAUser, 1) is not None
            session.commit()
            sa_session.commit()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "orig", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "orig", 1)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_expunge_all_empties_identity_map() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.add(User(id=1, name="a", score=1))
            session.add(User(id=2, name="b", score=2))
            session.flush()
            a = session.get(User, 1)
            b = session.get(User, 2)
            p = User(name="pending-z")
            session.add(p)
            session.expunge_all()
            # Persistent objects detached; pending became transient.
            assert session.get(User, 1) is not a
            assert session.get(User, 2) is not b
            assert p not in session.new
            session.rollback()
        with SASession(sa_engine) as sa_session:
            sa_session.add(SAUser(id=1, name="a", score=1))
            sa_session.add(SAUser(id=2, name="b", score=2))
            sa_session.flush()
            sa_a = sa_session.get(SAUser, 1)
            sa_b = sa_session.get(SAUser, 2)
            sa_p = SAUser(name="pending-z")
            sa_session.add(sa_p)
            sa_session.expunge_all()
            assert sa_session.get(SAUser, 1) is not sa_a
            assert sa_session.get(SAUser, 2) is not sa_b
            assert sa_p not in sa_session.new
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_expunge_does_not_autoflush() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            victim = session.get(User, 1)
            sa_victim = sa_session.get(SAUser, 1)
            p = User(name="pend-no-flush")
            sa_p = SAUser(name="pend-no-flush")
            session.add(p)
            sa_session.add(sa_p)
            session.expunge(victim)
            sa_session.expunge(sa_victim)
            assert p.id is None
            assert sa_p.id is None
            assert p in session.new
            assert sa_p in sa_session.new
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# bulk operations
# ---------------------------------------------------------------------------


def test_bulk_insert_mappings_inserts_without_touching_session() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert (
                session.bulk_insert_mappings(
                    User,
                    [{"id": 10, "name": "b1", "score": 1}, {"id": 11, "name": "b2", "score": 2}],
                )
                is None
            )
            assert (
                sa_session.bulk_insert_mappings(
                    SAUser,
                    [{"id": 10, "name": "b1", "score": 1}, {"id": 11, "name": "b2", "score": 2}],
                )
                is None
            )
            assert list(session.new) == []
            assert list(sa_session.new) == []
            session.commit()
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 10).name == "b1"
            assert sa_session.get(SAUser, 10).name == "b1"
            assert session.get(User, 11).score == 2
            assert sa_session.get(SAUser, 11).score == 2
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_bulk_insert_mappings_materializes_client_defaults() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.bulk_insert_mappings(User, [{"id": 30, "name": "nodefault"}])
            sa_session.bulk_insert_mappings(SAUser, [{"id": 30, "name": "nodefault"}])
            session.commit()
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 30).score == 0
            assert sa_session.get(SAUser, 30).score == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_bulk_update_mappings_updates_but_does_not_sync_session() -> None:
    """bulk_update_mappings emits per-row UPDATEs; in-session instances keep
    STALE values (no synchronize_session) until expiry/refresh."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        # Seed via bulk path for both.
        with Session(catalyst_engine) as session:
            session.bulk_insert_mappings(User, [{"id": 1, "name": "u1", "score": 1}])
            session.commit()
        with SASession(sa_engine) as sa_session:
            sa_session.bulk_insert_mappings(SAUser, [{"id": 1, "name": "u1", "score": 1}])
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u1 = session.get(User, 1)
            sa_u1 = sa_session.get(SAUser, 1)
            assert (
                session.bulk_update_mappings(User, [{"id": 1, "name": "upd", "score": 50}]) is None
            )
            assert (
                sa_session.bulk_update_mappings(SAUser, [{"id": 1, "name": "upd", "score": 50}])
                is None
            )
            # Session NOT synchronized: stale in-memory values, not dirty.
            assert u1.name == "u1"
            assert u1.score == 1
            assert u1 not in session.dirty
            assert sa_u1.name == "u1"
            assert sa_u1.score == 1
            assert sa_u1 not in sa_session.dirty
            # get() returns the same stale identity-map instance.
            assert session.get(User, 1) is u1
            assert session.get(User, 1).name == "u1"
            assert sa_session.get(SAUser, 1) is sa_u1
            assert sa_session.get(SAUser, 1).name == "u1"
            session.commit()
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 1).name == "upd"
            assert session.get(User, 1).score == 50
            assert sa_session.get(SAUser, 1).name == "upd"
            assert sa_session.get(SAUser, 1).score == 50
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_bulk_update_mappings_requires_pk() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            with pytest.raises(InvalidRequestError):
                session.bulk_update_mappings(User, [{"name": "no-id", "score": 1}])
            with pytest.raises(sa.exc.InvalidRequestError):
                sa_session.bulk_update_mappings(SAUser, [{"name": "no-id", "score": 1}])
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_bulk_update_mappings_unmatched_pk_raises_stale_data() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            with pytest.raises(Exception) as catalyst_error:
                session.bulk_update_mappings(User, [{"id": 999, "name": "ghost"}])
            with pytest.raises(sa.orm.exc.StaleDataError) as sa_error:
                sa_session.bulk_update_mappings(SAUser, [{"id": 999, "name": "ghost"}])
            assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
            session.rollback()
            sa_session.rollback()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_bulk_save_objects_bypasses_unit_of_work() -> None:
    """bulk_save_objects inserts rows directly: objects never enter
    session.new, stay non-pending, and do NOT get PK write-back."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            objs = [User(name="legacy1"), User(name="legacy2")]
            sa_objs = [SAUser(name="legacy1"), SAUser(name="legacy2")]
            assert session.bulk_save_objects(objs) is None
            assert sa_session.bulk_save_objects(sa_objs) is None
            assert objs[0] not in session.new
            assert sa_objs[0] not in sa_session.new
            session.flush()
            sa_session.flush()
            # No PK write-back: ids stay None even though rows exist.
            assert objs[0].id is None
            assert sa_objs[0].id is None
            session.commit()
            sa_session.commit()
        catalyst_rows = [
            r for r in _db_names(catalyst_engine, "lc_users") if r[1].startswith("legacy")
        ]
        sa_rows = [r for r in _db_names(sa_engine, "lc_users") if r[1].startswith("legacy")]
        assert len(catalyst_rows) == len(sa_rows) == 2
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_bulk_operations_do_not_autoflush() -> None:
    """Bulk methods run their SQL without flushing pending/dirty session
    state; pending objects flush later at commit."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            p = User(name="pend-bulk", score=1)
            sa_p = SAUser(name="pend-bulk", score=1)
            session.add(p)
            sa_session.add(sa_p)
            session.bulk_insert_mappings(User, [{"id": 50, "name": "bi", "score": 1}])
            sa_session.bulk_insert_mappings(SAUser, [{"id": 50, "name": "bi", "score": 1}])
            assert p.id is None
            assert sa_p.id is None
            assert p in session.new
            assert sa_p in sa_session.new
            session.commit()
            sa_session.commit()
        assert sorted(_db_names(catalyst_engine, "lc_users")) == sorted(
            _db_names(sa_engine, "lc_users")
        )
        assert ("pend-bulk", 1) in [(r[1], r[2]) for r in _db_names(catalyst_engine, "lc_users")]
        assert ("bi", 1) in [(r[1], r[2]) for r in _db_names(catalyst_engine, "lc_users")]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# ORM-enabled session.execute(update/delete)
# ---------------------------------------------------------------------------


def _import_catalyst_dml() -> tuple[Any, Any]:
    from catalyst import delete as c_delete
    from catalyst import update as c_update

    return c_update, c_delete


def test_execute_orm_update_synchronizes_session() -> None:
    """session.execute(update(Model)...) with default synchronize_session
    updates matching in-session instances in-memory WITHOUT marking dirty."""
    c_update, _ = _import_catalyst_dml()
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.bulk_insert_mappings(User, [{"id": 1, "name": "u1", "score": 1}])
            session.commit()
        with SASession(sa_engine) as sa_session:
            sa_session.bulk_insert_mappings(SAUser, [{"id": 1, "name": "u1", "score": 1}])
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u1 = session.get(User, 1)
            sa_u1 = sa_session.get(SAUser, 1)
            session.execute(c_update(User).where(User.id == 1).values(name="ormupd"))
            sa_session.execute(sa_update(SAUser).where(SAUser.id == 1).values(name="ormupd"))
            assert u1.name == "ormupd"
            assert u1 not in session.dirty
            assert not session.is_modified(u1)
            assert sa_u1.name == "ormupd"
            assert sa_u1 not in sa_session.dirty
            assert not sa_session.is_modified(sa_u1)
            session.commit()
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 1).name == "ormupd"
            assert sa_session.get(SAUser, 1).name == "ormupd"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_execute_orm_update_synchronize_session_false_stays_stale() -> None:
    c_update, _ = _import_catalyst_dml()
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.bulk_insert_mappings(User, [{"id": 1, "name": "u1", "score": 1}])
            session.commit()
        with SASession(sa_engine) as sa_session:
            sa_session.bulk_insert_mappings(SAUser, [{"id": 1, "name": "u1", "score": 1}])
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u1 = session.get(User, 1)
            sa_u1 = sa_session.get(SAUser, 1)
            session.execute(
                c_update(User)
                .where(User.id == 1)
                .values(name="nosync")
                .execution_options(synchronize_session=False)
            )
            sa_session.execute(
                sa_update(SAUser)
                .where(SAUser.id == 1)
                .values(name="nosync")
                .execution_options(synchronize_session=False)
            )
            assert u1.name == "u1"
            assert sa_u1.name == "u1"
            session.commit()
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 1).name == "nosync"
            assert sa_session.get(SAUser, 1).name == "nosync"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_execute_orm_delete_moves_instance_to_deleted() -> None:
    _, c_delete = _import_catalyst_dml()
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.bulk_insert_mappings(User, [{"id": 1, "name": "u1", "score": 1}])
            session.commit()
        with SASession(sa_engine) as sa_session:
            sa_session.bulk_insert_mappings(SAUser, [{"id": 1, "name": "u1", "score": 1}])
            sa_session.commit()
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u1 = session.get(User, 1)
            sa_u1 = sa_session.get(SAUser, 1)
            session.execute(c_delete(User).where(User.id == 1))
            sa_session.execute(sa_delete(SAUser).where(SAUser.id == 1))
            assert u1 in session.deleted
            # SA marks the state deleted but drops it from session.deleted
            # (verified 2.1.8): the deleted-state check is inspect().deleted.
            assert sa.inspect(sa_u1).deleted
            assert session.get(User, 1) is None
            assert sa_session.get(SAUser, 1) is None
            session.commit()
            sa_session.commit()
        assert _db_names(catalyst_engine, "lc_users") == []
        assert _db_names(sa_engine, "lc_users") == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_close_rolls_back_and_detaches() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        session = Session(catalyst_engine)
        sa_session = SASession(sa_engine)
        u = session.get(User, 1)
        sa_u = sa_session.get(SAUser, 1)
        u.name = "dirty-before-close"
        sa_u.name = "dirty-before-close"
        session.add(User(name="will-rollback"))
        sa_session.add(SAUser(name="will-rollback"))
        session.close()
        sa_session.close()
        assert session.in_transaction() is False
        assert sa_session.in_transaction() is False
        # Pending work rolled back; dirty change discarded in the DB.
        assert _db_names(catalyst_engine, "lc_users") == [(1, "orig", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "orig", 1)]
        # Detached objects keep readable loaded values.
        assert u.name == "dirty-before-close"
        assert sa_u.name == "dirty-before-close"
        # close() is idempotent.
        session.close()
        sa_session.close()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_close_then_reuse_session() -> None:
    """SA Sessions are reusable after close(): add+commit autobegins a new
    transaction — Catalyst matches by lazily re-acquiring an engine checkout."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        session = Session(catalyst_engine)
        sa_session = SASession(sa_engine)
        session.close()
        sa_session.close()
        session.add(User(name="after-close", score=1))
        sa_session.add(SAUser(name="after-close", score=1))
        session.commit()
        sa_session.commit()
        assert _db_names(catalyst_engine, "lc_users") == [(1, "after-close", 1)]
        assert _db_names(sa_engine, "lc_users") == [(1, "after-close", 1)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_context_manager_exit_rolls_back_pending() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.add(User(name="ctx-rollback", score=1))
        with SASession(sa_engine) as sa_session:
            sa_session.add(SAUser(name="ctx-rollback", score=1))
        assert _db_names(catalyst_engine, "lc_users") == []
        assert _db_names(sa_engine, "lc_users") == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_POPULATE_MISSING
def test_get_populate_existing_refreshes_identity_map() -> None:
    """populate_existing=True re-SELECTs and refreshes the SAME identity-map
    instance; plain get returns the stale cached instance."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            u = session.get(User, 1)
            sa_u = sa_session.get(SAUser, 1)
            with catalyst_engine.begin() as connection:
                connection.execute(
                    catalyst_update(User.__table__).where(User.id == 1).values(name="external")
                )
            with sa_engine.begin() as connection:
                connection.execute(
                    sa_update(SAUser.__table__).where(SAUser.id == 1).values(name="external")
                )
            # Plain get: stale identity-map value, no refresh.
            assert session.get(User, 1).name == "orig"
            assert sa_session.get(SAUser, 1).name == "orig"
            # populate_existing refreshes in place.
            refreshed = session.get(User, 1, populate_existing=True)
            sa_refreshed = sa_session.get(SAUser, 1, populate_existing=True)
            assert refreshed is u
            assert sa_refreshed is sa_u
            assert refreshed.name == "external"
            assert sa_refreshed.name == "external"
            session.commit()
            sa_session.commit()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_in_transaction_transitions() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        _seed_user(catalyst_engine, sa_side=False)
        _seed_user(sa_engine, sa_side=True)
        session = Session(catalyst_engine)
        sa_session = SASession(sa_engine)
        assert session.in_transaction() is False
        assert sa_session.in_transaction() is False
        session.get(User, 1)
        sa_session.get(SAUser, 1)
        assert session.in_transaction() is True
        assert sa_session.in_transaction() is True
        session.commit()
        sa_session.commit()
        assert session.in_transaction() is False
        assert sa_session.in_transaction() is False
        session.close()
        sa_session.close()
        assert session.in_transaction() is False
        assert sa_session.in_transaction() is False
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()
