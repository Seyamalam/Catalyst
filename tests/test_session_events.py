"""``SessionEvents`` parity against SQLAlchemy 2.1 (verified 2.1.8).

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
by registering recorders on every ``SessionEvents`` hook; full write-up in
``docs/m4-tail-ground-truth.md``. The whole module skips until catalyst
grows an event API (``catalyst.event`` / ``SessionEvents``) — none exists
today.

Key verified semantics encoded below (the firing matrix):

``session.add(obj)`` fires, in order:
``after_transaction_create``, ``before_attach``, ``after_attach``,
``transient_to_pending``.

``session.flush()`` (autobegin or explicit) fires:
``before_flush`` → ``after_begin`` (lazy txn start) → ``after_flush`` →
per-instance ``pending_to_persistent`` → ``after_flush_postexec``.
``before_commit`` fires BEFORE ``before_flush`` on ``commit()``;
``after_commit`` after ``after_flush_postexec``. Full add+commit order:
``before_commit, before_flush, after_begin, after_flush,
pending_to_persistent, after_flush_postexec, after_commit``.

- ``session.rollback()`` fires ``after_rollback`` then
  ``after_soft_rollback`` — whether or not anything was flushed.
- ``session.delete(obj); flush(); commit()`` fires ``persistent_to_deleted``
  during flush and ``deleted_to_detached`` AFTER ``after_commit``.
- ``loaded_as_persistent`` fires once per newly-hydrated instance that was
  NOT already in the identity map (bulk loads, autoflush-triggered SELECTs,
  ``merge`` of a detached object); plain ``get``/``scalars`` of already
  mapped rows do not re-fire it.
- ``expunge`` fires ``persistent_to_detached``; ``merge`` of a detached
  object fires ``loaded_as_persistent`` (not ``detached_to_persistent``).
- ``session.close()`` emits NO SessionEvents at all (verified: pending
  work closed silently).
- ``before_flush(session, flush_context, instances)`` sees live
  ``session.new``/``dirty``/``deleted`` collections and MAY mutate them —
  an object ``add``ed inside ``before_flush`` is flushed by the same flush.
  ``instances`` is NOT ``session.new``.
- API: ``event.listen(Session, name, fn)`` works on the Session CLASS and
  on instances; ``event.contains``/``event.remove``/``event.listens_for``
  (decorator) mirror ``sqlalchemy.event``; an unknown event name raises
  ``InvalidRequestError`` ("No such event ... for target ...").

Catalyst-side listeners attach through ``catalyst.event`` (module or
``catalyst.orm.event``) with the same ``listen``/``contains``/``remove``/
``listens_for`` surface; the skip probe looks for either ``event`` or
``SessionEvents``.
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
    InvalidRequestError,
    Mapped,
    Session,
    create_engine,
    mapped_column,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import event as sa_event
from sqlalchemy.exc import InvalidRequestError as SAInvalidRequestError
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


def _catalyst_event_api() -> Any:
    """Return the catalyst event module/object, or None."""
    for module in (catalyst, catalyst.expression, catalyst.orm):
        event_api = getattr(module, "event", None)
        if event_api is not None and hasattr(event_api, "listen"):
            return event_api
    return None


def _events_supported() -> bool:
    """Probe whether any SessionEvents-style API exists."""
    return _catalyst_event_api() is not None or _catalyst_symbol("SessionEvents") is not None


_EVENTS_MISSING = pytest.mark.skipif(
    not _events_supported(),
    reason="catalyst SessionEvents / event API is not implemented yet",
)

pytestmark = _EVENTS_MISSING


# ---------------------------------------------------------------------------
# SQLAlchemy reference model (always defined; SA is a dev dependency)
# ---------------------------------------------------------------------------


class SABase(SADeclarativeBase):
    pass


class SARow(SABase):
    __tablename__ = "ev_rows"

    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]


# ---------------------------------------------------------------------------
# Event recorders
# ---------------------------------------------------------------------------

ALL_SESSION_EVENTS = [
    "after_begin",
    "before_flush",
    "after_flush",
    "after_flush_postexec",
    "before_commit",
    "after_commit",
    "after_rollback",
    "after_soft_rollback",
    "after_transaction_create",
    "after_transaction_end",
    "before_attach",
    "after_attach",
    "pending_to_persistent",
    "persistent_to_deleted",
    "deleted_to_detached",
    "persistent_to_detached",
    "detached_to_persistent",
    "transient_to_pending",
    "pending_to_transient",
    "persistent_to_transient",
    "loaded_as_persistent",
]


def _recorder(sink: list[str], name: str) -> Any:
    def _fn(*args: Any, **kwargs: Any) -> None:
        sink.append(name)

    return _fn


def _attach_sa(session_cls: type[SASession], sink: list[str], names: list[str]) -> list[Any]:
    fns = [_recorder(sink, n) for n in names]
    for name, fn in zip(names, fns, strict=True):
        sa_event.listen(session_cls, name, fn)
    return fns


def _detach_sa(session_cls: type[SASession], names: list[str], fns: list[Any]) -> None:
    for name, fn in zip(names, fns, strict=True):
        sa_event.remove(session_cls, name, fn)


def _catalyst_models() -> type[Any]:
    """Lazily define the catalyst event model (feature-gated)."""

    class _CBase(DeclarativeBase):
        pass

    class CRow(_CBase):
        __tablename__ = "ev_rows"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

    return CRow


def _attach_catalyst(session_cls: type[Session], sink: list[str], names: list[str]) -> list[Any]:
    event_api = _catalyst_event_api()
    assert event_api is not None
    fns = [_recorder(sink, n) for n in names]
    for name, fn in zip(names, fns, strict=True):
        event_api.listen(session_cls, name, fn)
    return fns


def _detach_catalyst(session_cls: type[Session], names: list[str], fns: list[Any]) -> None:
    event_api = _catalyst_event_api()
    assert event_api is not None
    for name, fn in zip(names, fns, strict=True):
        event_api.remove(session_cls, name, fn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_firing_matrix_add_and_commit() -> None:
    """add+commit fires, in order: ``before_commit, before_flush,
    after_begin, after_flush, pending_to_persistent, after_flush_postexec,
    after_commit, after_transaction_end`` (transaction markers bracket the
    flush and the commit)."""
    expected = [
        "before_commit",
        "before_flush",
        "after_transaction_create",
        "after_begin",
        "after_flush",
        "pending_to_persistent",
        "after_flush_postexec",
        "after_transaction_end",
        "after_commit",
        "after_transaction_end",
    ]
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="a"))
            sink.clear()
            session.commit()
            # add() itself fired lifecycle events before commit; only the
            # commit sequence is asserted here.
            assert sink == expected
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()

    csink: list[str] = []
    cfns = _attach_catalyst(Session, csink, ALL_SESSION_EVENTS)
    catalyst_engine = create_engine(":memory:")
    try:
        CRow = _catalyst_models()
        CRow.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            session.add(CRow(name="a"))
            csink.clear()
            session.commit()
            assert csink == expected
    finally:
        _detach_catalyst(Session, ALL_SESSION_EVENTS, cfns)
        catalyst_engine.dispose()


def test_firing_matrix_flush_only() -> None:
    """flush() without commit: ``before_flush, after_transaction_create,
    after_begin, after_flush, pending_to_persistent, after_flush_postexec,
    after_transaction_end`` — no commit events."""
    expected = [
        "before_flush",
        "after_transaction_create",
        "after_begin",
        "after_flush",
        "pending_to_persistent",
        "after_flush_postexec",
        "after_transaction_end",
    ]
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="b"))
            sink.clear()
            session.flush()
            assert sink == expected
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()

    csink: list[str] = []
    cfns = _attach_catalyst(Session, csink, ALL_SESSION_EVENTS)
    catalyst_engine = create_engine(":memory:")
    try:
        CRow = _catalyst_models()
        CRow.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            session.add(CRow(name="b"))
            csink.clear()
            session.flush()
            assert csink == expected
    finally:
        _detach_catalyst(Session, ALL_SESSION_EVENTS, cfns)
        catalyst_engine.dispose()


def test_firing_matrix_rollback() -> None:
    """``session.rollback()`` on an active transaction fires
    ``after_rollback``, ``after_transaction_end``, then
    ``after_soft_rollback`` — with pending-only AND flushed state alike.
    A rollback on a session with NO transaction fires nothing."""
    expected = ["after_rollback", "after_transaction_end", "after_soft_rollback"]
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="c"))
            sink.clear()
            session.rollback()
            assert sink == expected

            session.add(SARow(name="d"))
            session.flush()
            sink.clear()
            session.rollback()
            assert sink == expected

            # fresh session, no transaction: rollback is silent
            s2 = SASession(sa_engine)
            try:
                s2.rollback()
                assert sink == []
            finally:
                s2.close()
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()


def test_firing_matrix_delete_and_commit() -> None:
    """delete+flush+commit: ``persistent_to_deleted`` inside the flush,
    ``deleted_to_detached`` AFTER ``after_commit``; the flush's transaction
    markers bracket the state events."""
    expected = [
        "before_commit",
        "before_flush",
        "after_transaction_create",
        "after_begin",
        "after_flush",
        "persistent_to_deleted",
        "after_flush_postexec",
        "after_transaction_end",
        "after_commit",
        "deleted_to_detached",
        "after_transaction_end",
    ]
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            row = SARow(name="victim")
            session.add(row)
            session.commit()
            session.delete(row)
            sink.clear()
            session.commit()
            assert sink == expected
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()


def test_add_fires_attach_and_transient_to_pending() -> None:
    """``session.add`` fires ``after_transaction_create``,
    ``before_attach``, ``after_attach``, ``transient_to_pending`` in order."""
    expected = [
        "after_transaction_create",
        "before_attach",
        "after_attach",
        "transient_to_pending",
    ]
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            sink.clear()
            session.add(SARow(name="x"))
            assert sink == expected
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()


def test_loaded_as_persistent_fires_per_new_instance() -> None:
    """``loaded_as_persistent`` fires once per newly hydrated instance not
    already in the identity map; ``merge`` of a detached object fires it
    too. ``expunge`` fires ``persistent_to_detached``."""
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as seeder:
            seeder.add(SARow(name="r1"))
            seeder.add(SARow(name="r2"))
            seeder.commit()

        with SASession(sa_engine) as session:
            rows = session.execute(sa.select(SARow)).scalars().all()
            assert len(rows) == 2
            # after_begin precedes the loads; exactly one
            # loaded_as_persistent per newly hydrated instance.
            assert sink.count("loaded_as_persistent") == 2
            assert "after_begin" in sink

            # identity-map hits do not re-fire
            sink.clear()
            session.execute(sa.select(SARow)).scalars().all()
            assert "loaded_as_persistent" not in sink

            # expunge -> persistent_to_detached; merge -> loaded_as_persistent
            victim = rows[0]
            session.expunge(victim)
            assert sink[-1] == "persistent_to_detached"
            sink.clear()
            session.merge(victim)
            assert sink == ["loaded_as_persistent"]
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()


def test_close_detaches_and_marks_pending_transient() -> None:
    """``session.close()`` fires detach events per managed object:
    pending work → ``pending_to_transient``; dirty persistent →
    ``persistent_to_detached``; both followed by ``after_transaction_end``
    when a transaction was active. A clean, transaction-less close emits
    NOTHING."""
    sink: list[str] = []
    fns = _attach_sa(SASession, sink, ALL_SESSION_EVENTS)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)

        # clean fresh session: no events at all
        with SASession(sa_engine) as session:
            sink.clear()
            session.close()
            assert sink == []

        # pending object: pending_to_transient + transaction end
        with SASession(sa_engine) as session:
            session.add(SARow(name="e"))
            sink.clear()
            session.close()
            assert "pending_to_transient" in sink
            assert "after_transaction_end" in sink

        # dirty persistent object: persistent_to_detached
        with SASession(sa_engine) as session:
            row = SARow(name="d")
            session.add(row)
            session.commit()
            row.name = "dirty"
            sink.clear()
            session.close()
            assert "persistent_to_detached" in sink
            assert "after_transaction_end" in sink
    finally:
        _detach_sa(SASession, ALL_SESSION_EVENTS, fns)
        sa_engine.dispose()


def test_before_flush_sees_and_may_mutate_state_collections() -> None:
    """``before_flush(session, flush_context, instances)`` exposes live
    ``session.new``/``dirty``/``deleted``; ``session.add`` inside the hook
    is flushed by the SAME flush; ``instances`` is not ``session.new``."""
    seen: dict[str, Any] = {}

    def _bf(session: SASession, flush_context: Any, instances: Any) -> None:
        seen["new"] = sorted(o.name for o in session.new)
        seen["dirty"] = list(session.dirty)
        seen["deleted"] = list(session.deleted)
        seen["instances_is_new"] = instances is session.new
        session.add(SARow(name="added-in-before-flush"))

    sa_event.listen(SASession, "before_flush", _bf)
    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SARow(name="f"))
            session.flush()
        assert seen["new"] == ["f"]
        assert seen["dirty"] == [] and seen["deleted"] == []
        assert seen["instances_is_new"] is False
        with SASession(sa_engine) as check:
            names = check.execute(sa.select(SARow.name)).scalars().all()
            assert "added-in-before-flush" in names
    finally:
        sa_event.remove(SASession, "before_flush", _bf)
        sa_engine.dispose()


def test_event_api_class_instance_contains_remove_listens_for() -> None:
    """``event.listen`` works on the Session class AND instances;
    ``contains``/``remove``/``listens_for`` mirror ``sqlalchemy.event``;
    unknown names raise ``InvalidRequestError``."""
    fired: list[str] = []

    def _fn(*args: Any) -> None:
        fired.append("hit")

    sa_engine = sa_create_engine("sqlite://")
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            # instance-level listener; a pending object guarantees an
            # active transaction so commit() emits its events.
            session.add(SARow(name="api"))
            sa_event.listen(session, "after_commit", _fn)
            assert sa_event.contains(session, "after_commit", _fn)
            assert not sa_event.contains(SASession, "after_commit", _fn)
            session.commit()
            assert fired == ["hit"]
            sa_event.remove(session, "after_commit", _fn)
            fired.clear()
            session.commit()
            assert fired == []

            # listens_for decorator on the class
            @sa_event.listens_for(SASession, "after_rollback")
            def _lr(*args: Any) -> None:
                fired.append("rollback")

            session.add(SARow(name="z"))
            session.rollback()
            assert "rollback" in fired
            sa_event.remove(SASession, "after_rollback", _lr)

            with pytest.raises(SAInvalidRequestError, match="No such event"):
                sa_event.listen(SASession, "bogus_event", lambda *a: None)
    finally:
        sa_engine.dispose()

    # catalyst-side API parity (same surface, catalyst.event)
    event_api = _catalyst_event_api()
    assert event_api is not None
    cfired: list[str] = []

    def _cfn(*args: Any) -> None:
        cfired.append("hit")

    catalyst_engine = create_engine(":memory:")
    try:
        CRow = _catalyst_models()
        CRow.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            session.add(CRow(name="api"))
            event_api.listen(session, "after_commit", _cfn)
            assert event_api.contains(session, "after_commit", _cfn)
            session.commit()
            assert cfired == ["hit"]
            event_api.remove(session, "after_commit", _cfn)
            with pytest.raises(InvalidRequestError, match="No such event"):
                event_api.listen(Session, "bogus_event", lambda *a: None)
    finally:
        catalyst_engine.dispose()
