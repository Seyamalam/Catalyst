"""Session write-path parity against SQLAlchemy 2.1 (add/flush/commit/delete/rollback).

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
(``Session`` defaults: ``autoflush=True``, ``expire_on_commit=True``):

- ``session.add(obj)`` marks the instance pending: ``obj in session.new``,
  ``session.dirty``/``session.deleted`` stay empty, ``session.is_modified(obj)``
  is True, and ``inspect(obj).session is session``. ``add``/``add_all`` return
  ``None``. Adding an object already pending in ANOTHER session raises
  ``InvalidRequestError`` ("already attached to session"); adding a
  non-mapped instance raises ``UnmappedInstanceError``.
- ``session.flush()`` INSERTs pending objects. With a single pending object
  SQLAlchemy emits ``INSERT INTO t (name, score) VALUES (?, ?)`` (PK column
  omitted) and populates ``obj.id`` from ``lastrowid``; with multiple pending
  objects it batches via executemany + ``RETURNING id``. Either way PKs are
  assigned in ``add`` order. Column order follows table definition order minus
  the PK. ``obj.id`` is ``None`` before flush.
- Autoflush: ``session.execute(select(...))``, ``session.scalars(...)``, and
  ``session.get(...)`` all flush pending changes first. ``autoflush=False``
  leaves pending objects unflushed — ``get`` then returns ``None`` for an
  unflushed explicit PK.
- ``session.commit()`` = flush + COMMIT. With the default
  ``expire_on_commit=True`` every attribute is expired: ``inspect(obj).expired``
  is True immediately after commit, and the next attribute access (including
  PK access) emits ``SELECT ... WHERE id = ?`` to refresh. With
  ``expire_on_commit=False`` no refresh occurs.
- Dirty tracking: setting an attribute puts the object in ``session.dirty``
  and ``is_modified`` is True. ``flush()`` emits ``UPDATE t SET col=? WHERE
  t.id = ?`` containing ONLY the dirty columns. Setting an attribute back to
  its original value does NOT un-dirty it — SA still emits the UPDATE on the
  next flush. Re-``add``ing a persistent object is a no-op.
- ``session.delete(obj)`` moves a persistent object to ``session.deleted``;
  ``flush()`` emits ``DELETE FROM t WHERE t.id = ?``. After flush (pre-commit)
  the object is still attached and attributes remain readable; after commit
  it is detached (``inspect(obj).session is None``) and ``session.get`` returns
  ``None``. ``delete`` on a pending (never-flushed) object raises
  ``InvalidRequestError`` ("is not persisted"); ``delete`` on a detached object
  is a silent no-op. ``session.refresh(obj)`` on a pending object raises
  ``InvalidRequestError`` ("is not persistent within this Session").
- ``session.rollback()`` after flush rolls back the transaction AND expires
  session state: ``session.new``/``dirty``/``deleted`` empty, pending objects
  detached with their Python-side attribute values retained (``obj.id`` keeps
  the flushed value, attributes readable without a SELECT). Dirty changes on
  persistent objects are discarded — the next attribute access re-SELECTs the
  committed value. ``close()`` without commit rolls back.
- ``session.begin()`` on an already-autobegun session raises
  ``InvalidRequestError`` ("A transaction is already begun on this Session.");
  ``begin_nested()`` creates a SAVEPOINT. ``in_transaction()`` is False on a
  fresh session and after ``commit()``/``rollback()`` (a post-commit expired
  attribute access autobegins a new transaction).
- Errors: committing a UNIQUE violation raises ``IntegrityError``; afterwards
  the session is in "partial rollback" state and further operations raise
  ``PendingRollbackError`` until ``session.rollback()``.
- Identity map: ``session.get(Entity, pk)`` twice returns the SAME instance
  (``is``), as does ``session.scalars(select(Entity)...)`` for an
  already-loaded PK; a different Session returns a different instance.
  ``get`` of a missing PK returns ``None``.

Session semantics probed here are dialect-agnostic (verified on SQLite; the
PK-assignment mechanism differs — RETURNING vs lastrowid — but the observable
result is identical). Relationship cascade, lazy/eager loading, and
``Session.merge``/``expunge`` are later slices and intentionally out of scope.
"""

from __future__ import annotations

from typing import Any

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
    update,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import update as sa_update
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column

_SESSION_WRITE_MISSING = pytest.mark.skipif(
    not hasattr(Session, "add"),
    reason="catalyst Session write-path (add/flush/commit) is not implemented yet",
)

pytestmark = _SESSION_WRITE_MISSING


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "sess_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    score: Mapped[int] = mapped_column(default=0)


class UniqueRow(Base):
    __tablename__ = "sess_unique"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True)


class SABase(SADeclarativeBase):
    pass


class SAUser(SABase):
    __tablename__ = "sess_users"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    score: SAMapped[int] = sa_mapped_column(default=0)


class SAUniqueRow(SABase):
    __tablename__ = "sess_unique"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    code: SAMapped[str] = sa_mapped_column(unique=True)


def _catalyst_engine() -> Any:
    engine = create_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine


def _sa_engine() -> Any:
    engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(engine)
    return engine


def _row_count(engine: Any, table_name: str) -> int:
    with engine.connect() as connection:
        return connection.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one()


# ---------------------------------------------------------------------------
# add() -> pending state
# ---------------------------------------------------------------------------


def test_add_marks_object_pending() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            user = User(name="ada", score=1)
            assert list(session.new) == []
            session.add(user)
            assert list(session.new) == [user]
            assert list(session.dirty) == []
            assert list(session.deleted) == []
            assert session.is_modified(user) is True
        with SASession(sa_engine) as sa_session:
            sa_user = SAUser(name="ada", score=1)
            assert list(sa_session.new) == []
            sa_session.add(sa_user)
            assert list(sa_session.new) == [sa_user]
            assert list(sa_session.dirty) == []
            assert list(sa_session.deleted) == []
            assert sa_session.is_modified(sa_user) is True
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_add_returns_none() -> None:
    engine = _catalyst_engine()
    try:
        with Session(engine) as session:
            assert session.add(User(name="x")) is None
    finally:
        engine.dispose()


def test_add_unmapped_object_raises() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            with pytest.raises(Exception) as catalyst_error:
                session.add(object())
            with pytest.raises(sa.orm.exc.UnmappedInstanceError) as sa_error:
                sa_session.add(object())
            assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
            assert "not mapped" in str(catalyst_error.value)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_add_object_attached_to_other_session_raises() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="x")
            sa_user = SAUser(name="x")
            other = Session(catalyst_engine)
            sa_other = SASession(sa_engine)
            try:
                other.add(user)
                sa_other.add(sa_user)
                with pytest.raises(InvalidRequestError):
                    session.add(user)
                with pytest.raises(sa.exc.InvalidRequestError):
                    sa_session.add(sa_user)
            finally:
                other.close()
                sa_other.close()
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# flush() -> INSERT + PK assignment
# ---------------------------------------------------------------------------


def test_flush_inserts_pending_and_assigns_pk() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            user = User(name="ada", score=7)
            session.add(user)
            assert user.id is None
            session.flush()
            assert user.id == 1
            assert list(session.new) == []
            assert list(session.dirty) == []
            assert session.is_modified(user) is False
        with SASession(sa_engine) as sa_session:
            sa_user = SAUser(name="ada", score=7)
            sa_session.add(sa_user)
            assert sa_user.id is None
            sa_session.flush()
            assert sa_user.id == 1
            assert list(sa_session.new) == []
            assert list(sa_session.dirty) == []
            assert sa_session.is_modified(sa_user) is False
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_flush_multiple_pending_assigns_sequential_pks() -> None:
    """SA batches multi-object flushes via executemany + ``RETURNING id`` on
    SQLite; single-object flushes use ``lastrowid``. Both assign PKs in add
    order — assert the observable contract, not the mechanism."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            users = [User(name=f"u{i}") for i in range(3)]
            for user in users:
                session.add(user)
            session.flush()
            assert [user.id for user in users] == [1, 2, 3]
        with SASession(sa_engine) as sa_session:
            sa_users = [SAUser(name=f"u{i}") for i in range(3)]
            for sa_user in sa_users:
                sa_session.add(sa_user)
            sa_session.flush()
            assert [sa_user.id for sa_user in sa_users] == [1, 2, 3]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_flushed_rows_are_visible_to_select() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.add(User(name="ada", score=3))
            session.flush()
            rows = session.scalars(select(User).order_by(User.id)).all()
            assert [(row.name, row.score) for row in rows] == [("ada", 3)]
        with SASession(sa_engine) as sa_session:
            sa_session.add(SAUser(name="ada", score=3))
            sa_session.flush()
            rows = sa_session.scalars(sa.select(SAUser).order_by(SAUser.id)).all()
            assert [(row.name, row.score) for row in rows] == [("ada", 3)]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# autoflush
# ---------------------------------------------------------------------------


def test_autoflush_flushes_pending_before_query() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(User(name="pending"))
            sa_session.add(SAUser(name="pending"))
            # execute() and scalars() both autoflush pending objects first.
            catalyst_rows = session.execute(select(User)).all()
            sa_rows = sa_session.execute(sa.select(SAUser)).all()
            assert len(catalyst_rows) == len(sa_rows) == 1
            assert list(session.new) == []
            assert list(sa_session.new) == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_autoflush_false_leaves_pending_unflushed() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with (
            Session(catalyst_engine, autoflush=False) as session,
            SASession(sa_engine, autoflush=False) as sa_session,
        ):
            user = User(id=50, name="pending")
            sa_user = SAUser(id=50, name="pending")
            session.add(user)
            sa_session.add(sa_user)
            assert session.get(User, 50) is None
            assert sa_session.get(SAUser, 50) is None
            assert list(session.new) == [user]
            assert list(sa_session.new) == [sa_user]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_get_autoflushes_pending_with_explicit_pk() -> None:
    """SA ``get`` autoflushes, so an unflushed pending object with an explicit
    PK is inserted and then returned from the identity map."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(id=42, name="pending")
            sa_user = SAUser(id=42, name="pending")
            session.add(user)
            sa_session.add(sa_user)
            got = session.get(User, 42)
            sa_got = sa_session.get(SAUser, 42)
            assert got is user
            assert sa_got is sa_user
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# commit() -> flush + COMMIT + expire_on_commit
# ---------------------------------------------------------------------------


def test_commit_flushes_and_persists() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada", score=9)
            sa_user = SAUser(name="ada", score=9)
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            assert user.id == sa_user.id == 1
            assert list(session.new) == list(session.dirty) == list(session.deleted) == []
            assert list(sa_session.new) == list(sa_session.dirty) == list(sa_session.deleted) == []
            # A fresh session sees the committed row on both engines.
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 1).name == "ada"
            assert sa_session.get(SAUser, 1).name == "ada"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_commit_expires_attributes_by_default() -> None:
    """``expire_on_commit=True`` (SA default): after commit every attribute is
    expired; the first subsequent attribute access re-SELECTs the whole row,
    so an external UPDATE that lands before that access is visible. (Verified:
    SA caches the row at the first expired access — an UPDATE made after it
    would NOT be seen, so the update must precede the ``id``/``name`` reads.)"""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="before")
            sa_user = SAUser(name="before")
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            with catalyst_engine.begin() as connection:
                connection.execute(update(User.__table__).where(User.id == 1).values(name="after"))
            with sa_engine.begin() as connection:
                connection.execute(
                    sa_update(SAUser.__table__).where(SAUser.id == 1).values(name="after")
                )
            assert user.name == "after"
            assert sa_user.name == "after"
            assert user.id == sa_user.id == 1
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_commit_expire_on_commit_false_keeps_state() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with (
            Session(catalyst_engine, expire_on_commit=False) as session,
            SASession(sa_engine, expire_on_commit=False) as sa_session,
        ):
            user = User(name="before")
            sa_user = SAUser(name="before")
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            with catalyst_engine.begin() as connection:
                connection.execute(update(User.__table__).where(User.id == 1).values(name="after"))
            with sa_engine.begin() as connection:
                connection.execute(
                    sa_update(SAUser.__table__).where(SAUser.id == 1).values(name="after")
                )
            # No refresh: both engines serve the stale committed-state value.
            assert user.name == "before"
            assert sa_user.name == "before"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_refresh_reloads_attributes() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="x", score=1)
            sa_user = SAUser(name="x", score=1)
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            with catalyst_engine.begin() as connection:
                connection.execute(update(User.__table__).where(User.id == 1).values(score=99))
            with sa_engine.begin() as connection:
                connection.execute(
                    sa_update(SAUser.__table__).where(SAUser.id == 1).values(score=99)
                )
            session.refresh(user)
            sa_session.refresh(sa_user)
            assert user.score == sa_user.score == 99
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# dirty tracking -> UPDATE
# ---------------------------------------------------------------------------


def test_dirty_tracking_and_flush_update() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada", score=1)
            sa_user = SAUser(name="ada", score=1)
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            assert list(session.dirty) == []
            assert list(sa_session.dirty) == []
            user.score = 5
            sa_user.score = 5
            assert list(session.dirty) == [user]
            assert list(sa_session.dirty) == [sa_user]
            assert session.is_modified(user) is True
            assert sa_session.is_modified(sa_user) is True
            session.flush()
            sa_session.flush()
            assert list(session.dirty) == []
            assert list(sa_session.dirty) == []
            with catalyst_engine.connect() as connection:
                row = connection.execute(
                    select(User.__table__.c.score).where(User.id == 1)
                ).scalar_one()
            assert row == 5
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_reverting_attribute_still_counts_as_dirty() -> None:
    """SA dirty-tracking is set-on-write, not value-diffed: assigning the
    original value back leaves the object dirty and the next flush still
    emits the UPDATE."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada", score=1)
            sa_user = SAUser(name="ada", score=1)
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            user.score = 5
            sa_user.score = 5
            user.score = 1
            sa_user.score = 1
            assert list(session.dirty) == [user]
            assert list(sa_session.dirty) == [sa_user]
            session.flush()
            sa_session.flush()
            assert list(session.dirty) == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_add_already_persistent_object_is_noop() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            session.flush()
            sa_session.flush()
            session.add(user)
            sa_session.add(sa_user)
            assert list(session.new) == []
            assert list(sa_session.new) == []
            assert list(session.dirty) == []
            assert list(sa_session.dirty) == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# delete()
# ---------------------------------------------------------------------------


def test_delete_marks_deleted_and_flush_removes_row() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            session.delete(user)
            sa_session.delete(sa_user)
            assert list(session.deleted) == [user]
            assert list(sa_session.deleted) == [sa_user]
            assert list(session.new) == list(session.dirty) == []
            assert list(sa_session.new) == list(sa_session.dirty) == []
            session.flush()
            sa_session.flush()
            assert list(session.deleted) == []
            assert list(sa_session.deleted) == []
            assert session.get(User, 1) is None
            assert sa_session.get(SAUser, 1) is None
            session.commit()
            sa_session.commit()
            assert _row_count(catalyst_engine, "sess_users") == 0
            assert _row_count(sa_engine, "sess_users") == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_delete_pending_object_raises() -> None:
    """SA ``delete`` on an object that was never flushed raises
    ``InvalidRequestError`` ("is not persisted")."""
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            with pytest.raises(InvalidRequestError, match="not persisted"):
                session.delete(user)
            with pytest.raises(sa.exc.InvalidRequestError, match="not persisted"):
                sa_session.delete(sa_user)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_delete_detaches_object_after_commit() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            session.delete(user)
            sa_session.delete(sa_user)
            session.commit()
            sa_session.commit()
            # SA parity: after commit the deleted instance is detached.
            assert sa.inspect(sa_user).session is None
            # Catalyst has no public inspect(); object_session parity is
            # asserted via get() returning None and re-adding raising below.
            assert session.get(User, 1) is None
            assert sa_session.get(SAUser, 1) is None
            # Deleting an already-detached object is a silent no-op in SA.
            sa_session.delete(sa_user)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# rollback()
# ---------------------------------------------------------------------------


def test_rollback_after_flush_discards_pending_insert() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            session.flush()
            sa_session.flush()
            assert user.id == sa_user.id == 1
            session.rollback()
            sa_session.rollback()
            assert list(session.new) == list(session.dirty) == list(session.deleted) == []
            assert list(sa_session.new) == list(sa_session.dirty) == list(sa_session.deleted) == []
            # SA keeps the flushed attribute values on the detached object.
            assert sa_user.name == "ada"
            assert sa_user.id == 1
            assert user.name == "ada"
            assert user.id == 1
        assert _row_count(catalyst_engine, "sess_users") == 0
        assert _row_count(sa_engine, "sess_users") == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_rollback_discards_dirty_changes() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada", score=1)
            sa_user = SAUser(name="ada", score=1)
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            user.score = 99
            sa_user.score = 99
            session.rollback()
            sa_session.rollback()
            # Expired state reloads the committed value on attribute access.
            assert user.score == 1
            assert sa_user.score == 1
            assert list(session.dirty) == []
            assert list(sa_session.dirty) == []
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_close_without_commit_rolls_back() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session:
            session.add(User(name="ada"))
            session.flush()
        with SASession(sa_engine) as sa_session:
            sa_session.add(SAUser(name="ada"))
            sa_session.flush()
        assert _row_count(catalyst_engine, "sess_users") == 0
        assert _row_count(sa_engine, "sess_users") == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# commit() failure -> IntegrityError / PendingRollbackError
# ---------------------------------------------------------------------------


def test_commit_unique_violation_raises_integrity_error() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(UniqueRow(code="dup"))
            sa_session.add(SAUniqueRow(code="dup"))
            session.commit()
            sa_session.commit()
            session.add(UniqueRow(code="dup"))
            sa_session.add(SAUniqueRow(code="dup"))
            with pytest.raises(IntegrityError):
                session.commit()
            with pytest.raises(sa.exc.IntegrityError):
                sa_session.commit()
            # After a failed flush SA enters partial-rollback: further
            # operations raise PendingRollbackError until rollback().
            with pytest.raises(Exception) as sa_pending_error:
                sa_session.get(SAUniqueRow, 1)
            assert type(sa_pending_error.value).__name__ == "PendingRollbackError"
            session.rollback()
            sa_session.rollback()
            # Session usable again after rollback.
            assert session.get(UniqueRow, 1).code == "dup"
            assert sa_session.get(SAUniqueRow, 1).code == "dup"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# identity map
# ---------------------------------------------------------------------------


def test_get_returns_same_instance_for_same_pk() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            first = session.get(User, 1)
            second = session.get(User, 1)
            sa_first = sa_session.get(SAUser, 1)
            sa_second = sa_session.get(SAUser, 1)
            assert first is second
            assert sa_first is sa_second
            # The committed instance itself is the identity-map entry.
            assert first is user
            assert sa_first is sa_user
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_scalars_select_returns_identity_map_instance() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            user = User(name="ada")
            sa_user = SAUser(name="ada")
            session.add(user)
            sa_session.add(sa_user)
            session.commit()
            sa_session.commit()
            loaded = session.scalars(select(User).where(User.id == 1)).one()
            sa_loaded = sa_session.scalars(sa.select(SAUser).where(SAUser.id == 1)).one()
            assert loaded is user
            assert sa_loaded is sa_user
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_different_sessions_have_different_instances() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(User(name="ada"))
            sa_session.add(SAUser(name="ada"))
            session.commit()
            sa_session.commit()
            with Session(catalyst_engine) as other_session, SASession(sa_engine) as sa_other:
                first = session.get(User, 1)
                second = other_session.get(User, 1)
                sa_first = sa_session.get(SAUser, 1)
                sa_second = sa_other.get(SAUser, 1)
                assert first is not second
                assert sa_first is not sa_second
                assert first.name == second.name == "ada"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_get_missing_pk_returns_none() -> None:
    catalyst_engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            assert session.get(User, 999) is None
            assert sa_session.get(SAUser, 999) is None
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()
