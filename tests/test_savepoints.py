"""Session-scoped savepoints parity against SQLAlchemy 2.1 (verified 2.1.8).

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
via ``before_cursor_execute`` SQL capture; full write-up in
``docs/m4-tail-ground-truth.md``. The whole module skips until
``Session.begin_nested`` returns a session-scoped transaction with
SQLAlchemy's unit-of-work snapshot semantics — today it delegates to
``Connection.begin_nested`` and returns a bare ``SavepointTransaction``
with no ``nested`` attribute and no UOW rollback restoration.

Key verified semantics encoded below:

- ``session.begin_nested()`` returns a ``SessionTransaction`` with
  ``nested is True``. Names are ``sa_savepoint_N`` numbered per session
  transaction (``sa_savepoint_1``, ``sa_savepoint_2`` for a second nested
  level in the same transaction; the counter resets per transaction).
- ``txn.commit()`` emits ``RELEASE SAVEPOINT <name>``;
  ``txn.rollback()`` emits ``ROLLBACK TO SAVEPOINT <name>`` (no RELEASE
  follows — SQLite does not require it).
- ``session.begin_nested()`` inside ``begin_nested()`` stacks savepoints;
  ``with session.begin_nested():`` rolls back to the savepoint when the
  block raises.
- UOW snapshot on ``ROLLBACK TO SAVEPOINT``: the session reverts to its
  state at savepoint creation. Objects that became pending INSIDE the
  savepoint are expunged entirely (``session is None``, out of
  ``session.new``/identity map) — they do NOT return to pending. Objects
  flushed (INSERTed) inside the savepoint are likewise detached from the
  session; their rolled-back rows never come back on outer ``commit()``
  and re-``add`` works. Dirty persistent objects have their in-memory
  attribute values reverted to the pre-savepoint snapshot and leave
  ``session.dirty``; a DELETE flushed inside the savepoint is undone and
  the object returns to persistent.
- ``session.begin()`` / autobegin: ``begin_nested()`` autobegins the
  session transaction. ``session.begin()`` twice raises
  ``InvalidRequestError``.
- SQLITE GOTCHA (verified): the pysqlite driver only issues an implicit
  ``BEGIN`` before DML — never for ``SAVEPOINT``/``SELECT``/``BEGIN``
  itself. If ``begin_nested()`` is the FIRST database-touching operation
  (no prior INSERT/UPDATE/DELETE in the session transaction), the
  SAVEPOINT itself opens the SQLite transaction and ``RELEASE`` returns
  to autocommit — i.e. RELEASE COMMITS the work, and a later
  ``session.rollback()`` is a DBAPI no-op that cannot undo it. Once any
  DML has run (autobegin), the outer transaction is real and
  ``session.rollback()`` undoes everything including released-savepoint
  work. ``session.begin()`` does NOT change this (it emits no BEGIN on
  pysqlite). This is dialect-specific to sqlite3's legacy isolation
  handling.

Catalyst-side models are defined lazily inside each test because
session-scoped savepoint transactions do not exist yet; SA models are
module-level.
"""

from __future__ import annotations

from typing import Any

import catalyst
import catalyst.expression  # noqa: F401  (ensures submodule attribute exists)
import catalyst.orm  # noqa: F401
import pytest
import sqlalchemy as sa
from catalyst import (
    DeclarativeBase,
    Mapped,
    Session,
    create_engine,
    mapped_column,
)
from catalyst import text as catalyst_text
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column


def _catalyst_symbol(name: str) -> Any:
    """Find ``name`` on whichever catalyst submodule exposes it."""
    for module in (catalyst, catalyst.expression, catalyst.orm):
        symbol = getattr(module, name, None)
        if symbol is not None:
            return symbol
    return None


def _session_savepoints_supported() -> bool:
    """Probe whether ``Session.begin_nested`` returns a session-scoped
    transaction (``SessionTransaction`` with ``nested is True``).

    ``Session.begin_nested`` already exists but delegates to
    ``Connection.begin_nested`` and returns a bare ``SavepointTransaction``
    — no ``nested`` attribute, no unit-of-work snapshot on rollback. The
    probe stays False until that session-scoped type lands.
    """
    if not hasattr(Session, "begin_nested"):
        return False
    try:

        class _SpBase(DeclarativeBase):
            pass

        class _SpRow(_SpBase):
            __tablename__ = "catalyst_sp_probe"
            id: Mapped[int] = mapped_column(primary_key=True)
            name: Mapped[str]

        engine = create_engine(":memory:")
        try:
            _SpRow.metadata.create_all(engine)
            with Session(engine) as session:
                txn = session.begin_nested()
                if getattr(txn, "nested", None) is not True:
                    return False
                obj = _SpRow(name="x")
                session.add(obj)
                session.flush()
                txn.rollback()
                # SA expunges objects whose INSERT was rolled back; the
                # connection-level savepoint leaves them persistent.
                if session.get(_SpRow, obj.id) is not None:
                    return False
        finally:
            engine.dispose()
    except Exception:
        return False
    return True


_SAVEPOINTS_MISSING = pytest.mark.skipif(
    not _session_savepoints_supported(),
    reason="catalyst session-scoped savepoint transactions "
    "(SessionTransaction UOW semantics) are not implemented yet",
)

pytestmark = _SAVEPOINTS_MISSING


# ---------------------------------------------------------------------------
# SQLAlchemy reference model (always defined; SA is a dev dependency)
# ---------------------------------------------------------------------------


class SABase(SADeclarativeBase):
    pass


class SARow(SABase):
    __tablename__ = "sp_rows"

    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]


def _catalyst_models() -> tuple[type[Any], Any]:
    """Lazily define the catalyst savepoint model (feature-gated)."""

    class _CBase(DeclarativeBase):
        pass

    class CRow(_CBase):
        __tablename__ = "sp_rows"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

    return CRow, _CBase


def _sa_statements(engine: Any) -> list[str]:
    statements: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append(statement)

    return statements


def _sa_inspect(obj: Any) -> Any:
    from sqlalchemy import inspect as _inspect

    return _inspect(obj)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_begin_nested_returns_session_transaction() -> None:
    """``begin_nested`` returns a ``SessionTransaction`` with
    ``nested is True`` (not a bare connection savepoint handle)."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            txn = session.begin_nested()
            assert getattr(txn, "nested", None) is True
            assert type(txn).__name__ == "SessionTransaction"
            txn.commit()
            session.commit()
    finally:
        sa_engine.dispose()

    CRow, _ = _catalyst_models()
    catalyst_engine = create_engine(":memory:")
    try:
        CRow.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            txn = session.begin_nested()
            assert getattr(txn, "nested", None) is True
            txn.commit()
            session.commit()
    finally:
        catalyst_engine.dispose()


def test_savepoint_sql_shape_and_nesting_names() -> None:
    """SQL shape: ``SAVEPOINT sa_savepoint_N`` per level, ``RELEASE`` on
    commit, ``ROLLBACK TO SAVEPOINT`` on rollback. Names number per
    session transaction. (Catalyst names its Core savepoints
    ``catalyst_sp_N`` — a documented difference; this test asserts the
    SA contract.)"""
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            # prior DML so the outer transaction is real (see gotcha test)
            session.add(SARow(name="seed"))
            session.flush()
            statements.clear()
            outer = session.begin_nested()
            session.add(SARow(name="outer"))
            session.flush()
            inner = session.begin_nested()
            session.add(SARow(name="inner"))
            session.flush()
            inner.rollback()
            outer.commit()
            session.commit()
        sp_statements = [s for s in statements if "SAVEPOINT" in s.upper()]
        assert sp_statements == [
            "SAVEPOINT sa_savepoint_1",
            "SAVEPOINT sa_savepoint_2",
            "ROLLBACK TO SAVEPOINT sa_savepoint_2",
            "RELEASE SAVEPOINT sa_savepoint_1",
        ]
    finally:
        sa_engine.dispose()


def test_rollback_to_savepoint_expunges_pending_object() -> None:
    """A pending (never-flushed) object added INSIDE the savepoint is
    expunged by ``ROLLBACK TO`` — it does not return to ``session.new`` and
    its row never appears."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="seed"))
            session.flush()
            sp = session.begin_nested()
            pending = SARow(name="pend")
            session.add(pending)
            assert pending in session.new
            sp.rollback()
            assert pending not in session.new
            assert _sa_inspect(pending).pending is False
            session.commit()
            rows = session.execute(sa_text("SELECT name FROM sp_rows")).scalars().all()
            assert "pend" not in rows
    finally:
        sa_engine.dispose()

    CRow, _ = _catalyst_models()
    catalyst_engine = create_engine(":memory:")
    try:
        CRow.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            session.add(CRow(name="seed"))
            session.flush()
            sp = session.begin_nested()
            pending = CRow(name="pend")
            session.add(pending)
            assert pending in session.new
            sp.rollback()
            assert pending not in session.new
            session.commit()
            rows = session.execute(catalyst_text("SELECT name FROM sp_rows")).scalars().all()
            assert "pend" not in rows
    finally:
        catalyst_engine.dispose()


def test_rollback_to_savepoint_detaches_flushed_insert() -> None:
    """An object INSERTed inside the savepoint is detached from the session
    by ``ROLLBACK TO``; outer ``commit()`` does not resurrect the row, and
    re-``add`` inserts cleanly."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="seed"))
            session.flush()
            sp = session.begin_nested()
            obj = SARow(name="rolled")
            session.add(obj)
            session.flush()
            assert obj.id is not None
            sp.rollback()
            # SA detaches the object entirely: no session, out of identity map.
            assert _sa_inspect(obj).session is None
            session.commit()
            assert session.get(SARow, obj.id) is None
            # re-add works and inserts exactly once
            session.add(obj)
            session.commit()
            assert session.get(SARow, obj.id) is not None
            count = session.execute(
                sa_text("SELECT COUNT(*) FROM sp_rows WHERE name='rolled'")
            ).scalar_one()
            assert count == 1
    finally:
        sa_engine.dispose()


def test_rollback_to_savepoint_restores_dirty_snapshot() -> None:
    """Dirty changes made INSIDE the savepoint revert to the pre-savepoint
    in-memory snapshot and leave ``session.dirty``; the DB keeps the
    pre-savepoint value."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            row = SARow(name="base")
            session.add(row)
            session.commit()
            session.flush()
            sp = session.begin_nested()
            row.name = "in-sp"
            session.flush()
            sp.rollback()
            assert row.name == "base"
            assert row not in session.dirty
            session.commit()
            fresh = session.execute(
                sa_text("SELECT name FROM sp_rows WHERE id=:i"), {"i": row.id}
            ).scalar_one()
            assert fresh == "base"
    finally:
        sa_engine.dispose()


def test_rollback_to_savepoint_undoes_delete() -> None:
    """A DELETE flushed inside the savepoint is undone: the row survives
    and the object returns to persistent (not ``session.deleted``)."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            row = SARow(name="todelete")
            session.add(row)
            session.commit()
            sp = session.begin_nested()
            session.delete(row)
            session.flush()
            sp.rollback()
            assert row not in session.deleted
            session.commit()
            count = session.execute(
                sa_text("SELECT COUNT(*) FROM sp_rows WHERE id=:i"), {"i": row.id}
            ).scalar_one()
            assert count == 1
    finally:
        sa_engine.dispose()


def test_nested_savepoints_and_outer_rollback() -> None:
    """Two levels of savepoints stack; rolling back the inner keeps outer
    work; rolling back the session undoes everything once a real outer
    transaction exists."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="seed"))
            session.flush()
            outer = session.begin_nested()
            session.add(SARow(name="outer-work"))
            session.flush()
            inner = session.begin_nested()
            session.add(SARow(name="inner-work"))
            session.flush()
            inner.rollback()
            outer.commit()
            session.commit()
            names = session.execute(sa_text("SELECT name FROM sp_rows ORDER BY id")).scalars().all()
            assert names == ["seed", "outer-work"]
    finally:
        sa_engine.dispose()


def test_begin_nested_context_manager_rolls_back_on_exception() -> None:
    """``with session.begin_nested():`` releases on clean exit and rolls
    back to the savepoint when the block raises; the session stays usable."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="seed"))
            session.flush()
            with session.begin_nested():
                session.add(SARow(name="kept"))
            try:
                with session.begin_nested():
                    session.add(SARow(name="dropped"))
                    raise ValueError("boom")
            except ValueError:
                pass
            session.commit()
            names = session.execute(sa_text("SELECT name FROM sp_rows ORDER BY id")).scalars().all()
            assert names == ["seed", "kept"]
    finally:
        sa_engine.dispose()


def test_session_rollback_undoes_released_savepoint_work() -> None:
    """With a real outer transaction (prior DML autobegan it), releasing a
    savepoint then ``session.rollback()`` undoes ALL the work."""
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="seed"))
            session.flush()
            sp = session.begin_nested()
            session.add(SARow(name="doomed"))
            session.flush()
            sp.commit()  # RELEASE — merges into outer txn
            session.rollback()
            names = session.execute(sa_text("SELECT name FROM sp_rows")).scalars().all()
            assert names == []
    finally:
        sa_engine.dispose()


def test_savepoint_first_operation_release_commits_sqlite_gotcha() -> None:
    """SQLITE GOTCHA: when ``begin_nested()`` is the FIRST database op of
    the session transaction, the SAVEPOINT itself opens the SQLite txn and
    ``RELEASE`` returns to autocommit — the work is COMMITTED and a later
    ``session.rollback()`` cannot undo it. (pysqlite issues implicit BEGIN
    only before DML; ``session.begin()`` emits no BEGIN either.)"""
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            sp = session.begin_nested()  # no prior DML!
            session.add(SARow(name="solo"))
            session.flush()
            sp.commit()
            session.rollback()  # DBAPI no-op: already autocommitted
            assert not any("BEGIN" in s.upper() for s in statements)
        # fresh session to observe committed state
        with SASession(sa_engine) as check:
            names = check.execute(sa_text("SELECT name FROM sp_rows")).scalars().all()
            assert names == ["solo"]
    finally:
        sa_engine.dispose()
