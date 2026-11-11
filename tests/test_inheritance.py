"""Declarative inheritance parity against SQLAlchemy 2.1 (STI + JTI).

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
(``docs/inheritance-ground-truth.md``). The whole module skips until
``__mapper_args__`` polymorphic inheritance is implemented; every test body
is written against the verified SA 2.1.8 semantics so it becomes the proving
suite when the feature lands.

Key verified semantics encoded below:

- STI: the subclass shares the parent's ``Table`` (``Sub.__table__ is
  Base.__table__``; no second ``CREATE TABLE``). ``polymorphic_on`` must name
  a MAPPED attribute — an unmapped name raises ``ArgumentError`` ("Can't
  determine polymorphic_on value"). ``polymorphic_identity`` has NO default
  in 2.1.8: omitting it yields ``None`` + ``SAWarning`` and INSERTs write
  NULL. INSERT auto-sets the discriminator column to the class's identity
  (``INSERT INTO person (name, type) VALUES (?, ?)`` → ``('boss',
  'manager')``); integer discriminators work identically.
- STI reads: ``session.get(Employee, id)`` emits ``WHERE id = ?`` only and
  hydrates the right subclass from the discriminator; ``get(Manager,
  employee_id)`` returns ``None`` via ``WHERE id = ? AND type IN (?)``.
  ``select(Manager)`` appends ``WHERE type IN (?)`` whose set includes all
  descendant identities (``('manager', 'senior_manager')``);
  ``select(Employee)`` has no filter and hydrates mixed classes. Instance
  UPDATE never touches ``type`` (``SET name=? WHERE id=?``); bulk
  ``update(Manager)``/``delete(Manager)`` DO append the ``type IN`` filter;
  instance DELETE is ``WHERE id = ?`` only; bulk ``delete(Employee)`` has
  no filter.
- JTI: two tables — child PK is also ``FOREIGN KEY(id) REFERENCES
  parent (id)``. ``polymorphic_on`` still lives on the parent. INSERT of a
  subclass emits TWO statements in order: parent INSERT (``RETURNING id``),
  then child INSERT with the parent-generated id. ``select(Manager)`` emits
  ``FROM parent JOIN child ON parent.id = child.id`` selecting child columns
  first (aliased ``id_1`` ...) with NO type filter. ``select(Employee)`` /
  ``get(Employee, id)`` hit ONLY the parent table — subclass resolution
  comes from the discriminator, and subclass-only columns load LAZILY via a
  second per-instance SELECT on the child table (``SELECT child.col FROM
  child WHERE ? = child.id``). Without ``polymorphic_on`` at all, JTI still
  works but parent queries return base-class instances. Instance delete
  removes child row THEN parent row; bulk ``delete(Parent)`` emits a single
  parent-table DELETE and ORPHANS child rows (verified surprise).
- Shared: ``__abstract__ = True`` mixins contribute columns to concrete
  children and stay unmapped; relationships FK-ing to an inheritance parent
  reach subclass rows and lazy-load the right subclass; ``MappedAsDataclass`
  works with the discriminator marked ``init=False`` (separate gate — likely
  its own slice). Concrete-table inheritance is deferred (each class needs
  its own full column set; awkward and rarely used).

Catalyst-side models are defined lazily inside each test because
``__mapper_args__``/polymorphic inheritance do not exist yet; the whole
module skips until it lands. SQLAlchemy models are module-level.
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
    ArgumentError,
    DeclarativeBase,
    ForeignKey,
    Mapped,
    Session,
    create_engine,
    delete,
    mapped_column,
    relationship,
    select,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text as sa_text
from sqlalchemy.dialects import sqlite as sa_sqlite
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column
from sqlalchemy.orm import relationship as sa_relationship
from sqlalchemy.schema import CreateTable as SACreateTable


def _catalyst_symbol(name: str) -> Any:
    """Find ``name`` on whichever catalyst submodule exposes it."""
    for module in (catalyst, catalyst.expression, catalyst.orm):
        symbol = getattr(module, name, None)
        if symbol is not None:
            return symbol
    return None


def _sti_supported() -> bool:
    """Probe whether ``__mapper_args__`` polymorphic STI is implemented."""
    try:

        class _InhBase(DeclarativeBase):
            pass

        class _InhEmp(_InhBase):
            __tablename__ = "catalyst_inh_probe"
            id: Mapped[int] = mapped_column(primary_key=True)
            type: Mapped[str]
            __mapper_args__ = {
                "polymorphic_on": "type",
                "polymorphic_identity": "employee",
            }

        class _InhMgr(_InhEmp):
            __mapper_args__ = {"polymorphic_identity": "manager"}

    except Exception:
        return False
    emp_table = getattr(_InhEmp, "__table__", None)
    mgr_mapper = getattr(_InhMgr, "__mapper__", None)
    return (
        emp_table is not None
        and getattr(_InhMgr, "__table__", None) is emp_table
        and mgr_mapper is not None
        and getattr(mgr_mapper, "polymorphic_identity", None) == "manager"
    )


_INHERITANCE_MISSING = pytest.mark.skipif(
    not _sti_supported(),
    reason="catalyst declarative inheritance (__mapper_args__/polymorphic_on) "
    "is not implemented yet",
)

pytestmark = _INHERITANCE_MISSING


def _jti_supported() -> bool:
    """Probe whether joined-table inheritance (distinct child table) works."""
    try:

        class _JtiBase(DeclarativeBase):
            pass

        class _JtiEmp(_JtiBase):
            __tablename__ = "catalyst_jti_probe_e"
            id: Mapped[int] = mapped_column(primary_key=True)
            type: Mapped[str]
            __mapper_args__ = {
                "polymorphic_on": "type",
                "polymorphic_identity": "employee",
            }

        class _JtiMgr(_JtiEmp):
            __tablename__ = "catalyst_jti_probe_m"
            id: Mapped[int] = mapped_column(ForeignKey("catalyst_jti_probe_e.id"), primary_key=True)
            dept: Mapped[str]
            __mapper_args__ = {"polymorphic_identity": "manager"}

    except Exception:
        return False
    mgr_table = getattr(_JtiMgr, "__table__", None)
    return mgr_table is not None and mgr_table is not _JtiEmp.__table__


_JTI_MISSING = pytest.mark.skipif(
    not _jti_supported(),
    reason="catalyst joined-table inheritance is not implemented yet",
)

_CATALYST_DATACLASS = _catalyst_symbol("MappedAsDataclass")

_DATACLASS_MISSING = pytest.mark.skipif(
    _CATALYST_DATACLASS is None,
    reason="catalyst MappedAsDataclass is not implemented yet",
)


# ---------------------------------------------------------------------------
# SQLAlchemy reference models (always defined; SA is a dev dependency)
# ---------------------------------------------------------------------------


class SABase(SADeclarativeBase):
    pass


# -- single-table inheritance ------------------------------------------------


class STIEmployee(SABase):
    __tablename__ = "sti_person"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    type: SAMapped[str] = sa_mapped_column()
    __mapper_args__ = {"polymorphic_on": "type", "polymorphic_identity": "employee"}


class STIManager(STIEmployee):
    __mapper_args__ = {"polymorphic_identity": "manager"}


class STISeniorManager(STIManager):
    __mapper_args__ = {"polymorphic_identity": "senior_manager"}


# -- joined-table inheritance -------------------------------------------------


class JTIEmployee(SABase):
    __tablename__ = "jti_employee"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    type: SAMapped[str] = sa_mapped_column()
    __mapper_args__ = {"polymorphic_on": "type", "polymorphic_identity": "employee"}


class JTIManager(JTIEmployee):
    __tablename__ = "jti_manager"
    id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("jti_employee.id"), primary_key=True)
    level: SAMapped[int]
    __mapper_args__ = {"polymorphic_identity": "manager"}


# -- non-polymorphic joined-table inheritance ---------------------------------


class NPJEmployee(SABase):
    __tablename__ = "npj_employee"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]


class NPJManager(NPJEmployee):
    __tablename__ = "npj_manager"
    id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("npj_employee.id"), primary_key=True)
    level: SAMapped[int]


# -- integer discriminator ----------------------------------------------------


class IntAnimal(SABase):
    __tablename__ = "int_animal"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    kind: SAMapped[int] = sa_mapped_column()
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": 1}


class IntDog(IntAnimal):
    __mapper_args__ = {"polymorphic_identity": 2}


# -- relationship to an inheritance parent ------------------------------------


class RelDept(SABase):
    __tablename__ = "rel_dept"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    head_id: SAMapped[int] = sa_mapped_column(sa.ForeignKey("sti_person.id"))
    head: SAMapped[STIEmployee] = sa_relationship()


# -- __abstract__ mixin ---------------------------------------------------------


class AbsBase(SABase):
    __abstract__ = True
    stamp: SAMapped[str] = sa_mapped_column()


class AbsChild(AbsBase):
    __tablename__ = "abs_child"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)


# ---------------------------------------------------------------------------
# Catalyst model factories (defined lazily; inheritance unsupported today)
# ---------------------------------------------------------------------------


def _catalyst_sti_models() -> tuple[type[Any], type[Any], type[Any]]:
    class CBase(DeclarativeBase):
        pass

    class CEmployee(CBase):
        __tablename__ = "sti_person"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        type: Mapped[str] = mapped_column()
        __mapper_args__ = {
            "polymorphic_on": "type",
            "polymorphic_identity": "employee",
        }

    class CManager(CEmployee):
        __mapper_args__ = {"polymorphic_identity": "manager"}

    class CSeniorManager(CManager):
        __mapper_args__ = {"polymorphic_identity": "senior_manager"}

    return CEmployee, CManager, CSeniorManager


def _catalyst_jti_models() -> tuple[type[Any], type[Any]]:
    class CBase(DeclarativeBase):
        pass

    class CEmployee(CBase):
        __tablename__ = "jti_employee"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        type: Mapped[str] = mapped_column()
        __mapper_args__ = {
            "polymorphic_on": "type",
            "polymorphic_identity": "employee",
        }

    class CManager(CEmployee):
        __tablename__ = "jti_manager"
        id: Mapped[int] = mapped_column(ForeignKey("jti_employee.id"), primary_key=True)
        level: Mapped[int]
        __mapper_args__ = {"polymorphic_identity": "manager"}

    return CEmployee, CManager


def _catalyst_np_jti_models() -> tuple[type[Any], type[Any]]:
    class CBase(DeclarativeBase):
        pass

    class CEmployee(CBase):
        __tablename__ = "npj_employee"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]

    class CManager(CEmployee):
        __tablename__ = "npj_manager"
        id: Mapped[int] = mapped_column(ForeignKey("npj_employee.id"), primary_key=True)
        level: Mapped[int]

    return CEmployee, CManager


def _catalyst_int_models() -> tuple[type[Any], type[Any]]:
    class CBase(DeclarativeBase):
        pass

    class CAnimal(CBase):
        __tablename__ = "int_animal"
        id: Mapped[int] = mapped_column(primary_key=True)
        kind: Mapped[int] = mapped_column()
        __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": 1}

    class CDog(CAnimal):
        __mapper_args__ = {"polymorphic_identity": 2}

    return CAnimal, CDog


def _catalyst_rel_models() -> tuple[type[Any], type[Any], type[Any]]:
    """(CEmployee STI pair, CManager, CDept) sharing one declarative base."""

    class CBase(DeclarativeBase):
        pass

    class CEmployee(CBase):
        __tablename__ = "sti_person"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        type: Mapped[str] = mapped_column()
        __mapper_args__ = {
            "polymorphic_on": "type",
            "polymorphic_identity": "employee",
        }

    class CManager(CEmployee):
        __mapper_args__ = {"polymorphic_identity": "manager"}

    class CDept(CBase):
        __tablename__ = "rel_dept"
        id: Mapped[int] = mapped_column(primary_key=True)
        head_id: Mapped[int] = mapped_column(ForeignKey("sti_person.id"))
        head: Mapped[CEmployee] = relationship()

    return CEmployee, CManager, CDept


def _catalyst_abstract_models() -> tuple[type[Any], type[Any]]:
    class CBase(DeclarativeBase):
        pass

    class CAbsBase(CBase):
        __abstract__ = True
        stamp: Mapped[str] = mapped_column()

    class CAbsChild(CAbsBase):
        __tablename__ = "abs_child"
        id: Mapped[int] = mapped_column(primary_key=True)

    return CAbsBase, CAbsChild


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sa_statements(engine: Any) -> list[tuple[str, Any]]:
    statements: list[tuple[str, Any]] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append((statement, parameters))

    return statements


def _sa_selects(statements: list[tuple[str, Any]]) -> list[str]:
    return [" ".join(s.split()) for s, _ in statements if s.lstrip().upper().startswith("SELECT")]


def _catalyst_selects(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        " ".join(record.getMessage().split())
        for record in caplog.records
        if record.name == "catalyst.engine"
        and record.getMessage().lstrip().upper().startswith("SELECT")
    ]


def _norm(sql: str) -> str:
    return " ".join(sql.split()).upper()


# ---------------------------------------------------------------------------
# STI: DDL / mapping shape
# ---------------------------------------------------------------------------


def test_sti_subclass_shares_parent_table() -> None:
    """STI subclass has no ``__tablename__`` of its own; ``__table__`` is the
    parent's and exactly ONE ``CREATE TABLE`` is emitted."""
    CEmployee, CManager, _ = _catalyst_sti_models()
    assert CManager.__table__ is CEmployee.__table__
    assert CEmployee.__mapper__.polymorphic_on is not None
    assert CManager.__mapper__.polymorphic_identity == "manager"

    catalyst_engine = create_engine(":memory:")
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        sa_engine = sa_create_engine("sqlite://")
        try:
            SABase.metadata.create_all(sa_engine)
            catalyst_ddl = str(
                SACreateTable(CEmployee.__table__).compile(dialect=sa_sqlite.dialect())
            )
            sa_ddl = str(SACreateTable(STIEmployee.__table__).compile(dialect=sa_sqlite.dialect()))
            assert catalyst_ddl == sa_ddl
            assert "type VARCHAR NOT NULL" in catalyst_ddl
            assert len(CEmployee.metadata.tables) == 1
        finally:
            sa_engine.dispose()
    finally:
        catalyst_engine.dispose()


def test_sti_polymorphic_on_requires_mapped_column() -> None:
    """``polymorphic_on`` naming an unmapped attribute raises ``ArgumentError``
    ("Can't determine polymorphic_on value 'dtype'")."""

    class CBase(DeclarativeBase):
        pass

    with pytest.raises(ArgumentError, match="polymorphic_on"):

        class CBad(CBase):
            __tablename__ = "sti_bad"
            id: Mapped[int] = mapped_column(primary_key=True)
            __mapper_args__ = {
                "polymorphic_on": "dtype",
                "polymorphic_identity": "x",
            }


def test_sti_missing_polymorphic_identity_defaults_to_none() -> None:
    """SA 2.1.8 does NOT default the identity to the class name: a subclass
    without ``polymorphic_identity`` gets ``None`` (with ``SAWarning``) and
    INSERTs write NULL into the discriminator."""

    class CBase(DeclarativeBase):
        pass

    class CNoIdent(CBase):
        __tablename__ = "sti_noident"
        id: Mapped[int] = mapped_column(primary_key=True)
        kind: Mapped[str] = mapped_column(nullable=True)
        __mapper_args__ = {"polymorphic_on": "kind"}

    class CChild(CNoIdent):
        pass

    assert CChild.__mapper__.polymorphic_identity is None
    assert CNoIdent.__mapper__.polymorphic_identity is None


# ---------------------------------------------------------------------------
# STI: INSERT / SELECT / UPDATE / DELETE
# ---------------------------------------------------------------------------


def test_sti_insert_writes_polymorphic_identity() -> None:
    """``session.add(Manager(...))`` auto-sets ``type='manager'``; the INSERT
    column list includes the discriminator."""
    CEmployee, CManager, _ = _catalyst_sti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session:
            session.add(CManager(name="boss"))
            session.add(CEmployee(name="worker"))
            session.commit()
        with Session(catalyst_engine) as session:
            rows = [
                tuple(r)
                for r in session.execute(
                    sa_text("SELECT name, type FROM sti_person ORDER BY id")
                ).all()
            ]
        assert rows == [("boss", "manager"), ("worker", "employee")]

        with SASession(sa_engine) as sa_session:
            sa_session.add(STIManager(name="boss"))
            sa_session.add(STIEmployee(name="worker"))
            sa_session.commit()
        sa_inserts = [_norm(s) for s, _ in statements if s.lstrip().upper().startswith("INSERT")]
        assert any("INSERT INTO STI_PERSON (NAME, TYPE) VALUES (?, ?)" in s for s in sa_inserts)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_sti_get_and_select_polymorphic_resolution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``get(Employee, mgr_id)`` returns a ``Manager`` with an unfiltered
    ``WHERE id = ?``; ``get(Manager, emp_id)`` returns ``None`` via
    ``AND type IN (?)``; ``select(Manager)`` appends ``type IN`` while
    ``select(Employee)`` has no filter and hydrates mixed classes."""
    CEmployee, CManager, _ = _catalyst_sti_models()
    catalyst_engine = create_engine(":memory:", echo=True)
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss"))
            session.add(CEmployee(name="worker"))
            session.commit()
            sa_session.add(STIManager(name="boss"))
            sa_session.add(STIEmployee(name="worker"))
            sa_session.commit()

            mgr_id = session.execute(
                sa_text("SELECT id FROM sti_person WHERE type = 'manager'")
            ).scalar_one()
            emp_id = session.execute(
                sa_text("SELECT id FROM sti_person WHERE type = 'employee'")
            ).scalar_one()

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()

            assert isinstance(session.get(CEmployee, mgr_id), CManager)
            assert session.get(CManager, emp_id) is None
            assert isinstance(session.get(CManager, mgr_id), CManager)
            catalyst_sql = _catalyst_selects(caplog)

            mixed = list(session.scalars(select(CEmployee)))
            assert [type(o).__name__ for o in mixed] == ["CManager", "CEmployee"]
            managers = list(session.scalars(select(CManager)))
            assert [type(o).__name__ for o in managers] == ["CManager"]

            # SA reference: same resolutions and filter shapes.
            statements.clear()
            sa_mixed = list(sa_session.scalars(select(STIEmployee)))
            assert [type(o).__name__ for o in sa_mixed] == ["STIManager", "STIEmployee"]
            sa_managers = list(sa_session.scalars(select(STIManager)))
            assert [type(o).__name__ for o in sa_managers] == ["STIManager"]
            sa_selects = _sa_selects(statements)
            manager_select = next(s for s in sa_selects if "TYPE IN" in s)
            assert "WHERE STI_PERSON.TYPE IN (?)" in manager_select
            parent_select = next(s for s in sa_selects if "TYPE IN" not in s)
            assert parent_select.endswith("FROM STI_PERSON")

            # Catalyst emitted the same logical shapes.
            assert any("TYPE IN" in s for s in catalyst_sql)
            assert any("FROM STI_PERSON" in s and "TYPE IN" not in s for s in catalyst_sql)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_sti_multilevel_identity_includes_descendants() -> None:
    """``select(Manager)`` filters ``type IN ('manager', 'senior_manager')`` —
    the identity set includes every descendant identity."""
    CEmployee, CManager, CSenior = _catalyst_sti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="m"))
            session.add(CSenior(name="sm"))
            session.commit()
            sa_session.add(STIManager(name="m"))
            sa_session.add(STISeniorManager(name="sm"))
            sa_session.commit()

            got = list(session.scalars(select(CManager)))
            assert sorted(type(o).__name__ for o in got) == ["CManager", "CSeniorManager"]

            statements.clear()
            sa_got = list(sa_session.scalars(select(STIManager)))
            assert sorted(type(o).__name__ for o in sa_got) == [
                "STISeniorManager",
                "STIManager",
            ]
            sa_selects = _sa_selects(statements)
            assert any("TYPE IN (?, ?)" in s for s in sa_selects)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_sti_update_and_delete_filters() -> None:
    """Instance flush UPDATE is ``SET name=? WHERE id=?`` (no ``type``);
    ORM ``delete(Manager)`` is ``DELETE FROM person WHERE type IN (?)``;
    instance ``session.delete`` is ``WHERE id = ?`` only."""
    CEmployee, CManager, _ = _catalyst_sti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss"))
            session.add(CManager(name="mgr2"))
            session.commit()
            sa_session.add(STIManager(name="boss"))
            sa_session.add(STIManager(name="mgr2"))
            sa_session.commit()

            # Instance UPDATE: no discriminator in SET or WHERE.
            mgr = session.scalars(select(CManager)).first()
            mgr.name = "boss2"
            session.commit()

            sa_mgr = sa_session.scalars(select(STIManager)).first()
            sa_mgr.name = "boss2"
            sa_session.commit()
            update_sql = next(
                _norm(s) for s, _ in statements if s.lstrip().upper().startswith("UPDATE")
            )
            assert update_sql == "UPDATE STI_PERSON SET NAME=? WHERE STI_PERSON.ID = ?"

            # ORM bulk delete on the subclass appends the identity filter.
            statements.clear()
            session.execute(delete(CManager))
            session.commit()
            sa_session.execute(sa.delete(STIManager))
            sa_session.commit()
            sa_delete = next(
                _norm(s) for s, _ in statements if s.lstrip().upper().startswith("DELETE")
            )
            assert sa_delete == "DELETE FROM STI_PERSON WHERE STI_PERSON.TYPE IN (?)"
            assert session.execute(sa_text("SELECT count(*) FROM sti_person")).scalar_one() == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_sti_int_discriminator() -> None:
    """Non-string discriminators work: ``kind INTEGER`` with int identities
    round-trips and hydrates the subclass."""
    CAnimal, CDog = _catalyst_int_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    try:
        CAnimal.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CDog())
            session.add(CAnimal())
            session.commit()
            sa_session.add(IntDog())
            sa_session.add(IntAnimal())
            sa_session.commit()

            kinds = session.execute(sa_text("SELECT kind FROM int_animal ORDER BY id")).all()
            assert kinds == [(2,), (1,)]
            dogs = list(session.scalars(select(CDog)))
            assert [type(o).__name__ for o in dogs] == ["CDog"]
            animals = list(session.scalars(select(CAnimal)))
            assert sorted(type(o).__name__ for o in animals) == ["CAnimal", "CDog"]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# JTI: DDL / INSERT / SELECT / DELETE
# ---------------------------------------------------------------------------


@_JTI_MISSING
def test_jti_ddl_two_tables_child_pk_is_fk() -> None:
    """JTI emits two ``CREATE TABLE``s; the child PK column is also
    ``FOREIGN KEY(id) REFERENCES jti_employee (id)``."""
    CEmployee, CManager = _catalyst_jti_models()
    assert CManager.__table__ is not CEmployee.__table__
    assert len(CEmployee.metadata.tables) == 2

    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        catalyst_parent = str(
            SACreateTable(CEmployee.__table__).compile(dialect=sa_sqlite.dialect())
        )
        sa_parent = str(SACreateTable(JTIEmployee.__table__).compile(dialect=sa_sqlite.dialect()))
        catalyst_child = str(SACreateTable(CManager.__table__).compile(dialect=sa_sqlite.dialect()))
        sa_child = str(SACreateTable(JTIManager.__table__).compile(dialect=sa_sqlite.dialect()))
        assert catalyst_parent == sa_parent
        assert catalyst_child == sa_child
        assert "FOREIGN KEY(id) REFERENCES jti_employee (id)" in catalyst_child
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_insert_parent_then_child() -> None:
    """Flushing a ``Manager`` emits the parent INSERT first, then the child
    INSERT carrying the parent-generated id."""
    CEmployee, CManager = _catalyst_jti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.commit()
            sa_session.add(JTIManager(name="boss", level=3))
            sa_session.commit()

            parent_row = tuple(
                session.execute(sa_text("SELECT id, name, type FROM jti_employee")).one()
            )
            child_row = tuple(session.execute(sa_text("SELECT id, level FROM jti_manager")).one())
            assert parent_row == (1, "boss", "manager")
            assert child_row == (1, 3)

            dml = [
                _norm(s)
                for s, _ in statements
                if s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            ]
            inserts = [s for s in dml if s.startswith("INSERT")]
            assert inserts[0].startswith("INSERT INTO JTI_EMPLOYEE")
            assert inserts[1].startswith("INSERT INTO JTI_MANAGER")
            assert "TYPE" in inserts[0]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_select_subclass_inner_join() -> None:
    """``select(Manager)`` emits ``FROM jti_employee JOIN jti_manager`` with
    child columns first and NO discriminator filter."""
    CEmployee, CManager = _catalyst_jti_models()
    catalyst_engine = create_engine(":memory:", echo=True)
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.commit()
            sa_session.add(JTIManager(name="boss", level=3))
            sa_session.commit()

            caplog_managers = list(session.scalars(select(CManager)))
            assert [(o.name, o.level) for o in caplog_managers] == [("boss", 3)]

            statements.clear()
            sa_managers = list(sa_session.scalars(select(JTIManager)))
            assert [(o.name, o.level) for o in sa_managers] == [("boss", 3)]
            sa_selects = _sa_selects(statements)
            join_select = next(s for s in sa_selects if "JOIN" in s)
            assert (
                "FROM JTI_EMPLOYEE JOIN JTI_MANAGER "
                "ON JTI_EMPLOYEE.ID = JTI_MANAGER.ID" in join_select
            )
            assert "TYPE IN" not in join_select
            # Child-table columns come first, parent columns aliased id_1 etc.
            assert join_select.index("JTI_MANAGER.ID") < join_select.index(
                "JTI_EMPLOYEE.ID AS ID_1"
            )
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_parent_query_defers_subclass_columns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``select(Employee)``/``get(Employee, id)`` hit ONLY the parent table;
    the discriminator resolves the Python class and subclass-only columns
    load lazily via a second per-instance SELECT on the child table."""
    CEmployee, CManager = _catalyst_jti_models()
    catalyst_engine = create_engine(":memory:", echo=True)
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.add(CEmployee(name="worker"))
            session.commit()
            sa_session.add(JTIManager(name="boss", level=3))
            sa_session.add(JTIEmployee(name="worker"))
            sa_session.commit()

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            rows = list(session.scalars(select(CEmployee)))
            assert sorted(type(o).__name__ for o in rows) == ["CEmployee", "CManager"]
            parent_selects = [s for s in _catalyst_selects(caplog) if "JTI_MANAGER" not in s]
            assert len(parent_selects) == 1
            # Subclass column access triggers the deferred child-table load.
            caplog.clear()
            mgr = next(o for o in rows if type(o).__name__ == "CManager")
            assert mgr.level == 3
            child_selects = [s for s in _catalyst_selects(caplog) if "JTI_MANAGER" in s]
            assert len(child_selects) == 1

            statements.clear()
            sa_rows = list(sa_session.scalars(select(JTIEmployee)))
            assert sorted(type(o).__name__ for o in sa_rows) == [
                "JTIEmployee",
                "JTIManager",
            ]
            sa_selects = _sa_selects(statements)
            assert len(sa_selects) == 1
            assert "JTI_MANAGER" not in sa_selects[0]
            mgr_sa = next(o for o in sa_rows if type(o).__name__ == "JTIManager")
            statements.clear()
            assert mgr_sa.level == 3
            deferred = _sa_selects(statements)
            assert len(deferred) == 1
            assert "JTI_MANAGER" in deferred[0]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_get_resolves_subclass() -> None:
    """``get(Employee, mgr_id)`` returns the ``Manager`` instance; only the
    parent table is queried until a subclass attribute is touched."""
    CEmployee, CManager = _catalyst_jti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.commit()
            sa_session.add(JTIManager(name="boss", level=3))
            sa_session.commit()

            mgr_id = session.execute(sa_text("SELECT id FROM jti_employee")).scalar_one()
            got = session.get(CEmployee, mgr_id)
            assert type(got).__name__ == "CManager"
            assert got.level == 3
            sa_got = sa_session.get(JTIEmployee, mgr_id)
            assert type(sa_got).__name__ == "JTIManager"
            assert sa_got.level == 3
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_instance_delete_child_then_parent() -> None:
    """``session.delete(manager)`` flushes ``DELETE FROM jti_manager`` then
    ``DELETE FROM jti_employee`` — child row first."""
    CEmployee, CManager = _catalyst_jti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.commit()
            sa_session.add(JTIManager(name="boss", level=3))
            sa_session.commit()

            obj = session.scalars(select(CManager)).one()
            session.delete(obj)
            session.commit()
            assert session.execute(sa_text("SELECT count(*) FROM jti_employee")).scalar_one() == 0
            assert session.execute(sa_text("SELECT count(*) FROM jti_manager")).scalar_one() == 0

            sa_obj = sa_session.scalars(select(JTIManager)).one()
            sa_session.delete(sa_obj)
            sa_session.commit()
            deletes = [_norm(s) for s, _ in statements if s.lstrip().upper().startswith("DELETE")]
            assert deletes == [
                "DELETE FROM JTI_MANAGER WHERE JTI_MANAGER.ID = ?",
                "DELETE FROM JTI_EMPLOYEE WHERE JTI_EMPLOYEE.ID = ?",
            ]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_bulk_delete_parent_leaves_child_rows() -> None:
    """ORM ``delete(Employee)`` emits a single parent-table DELETE and
    ORPHANS the child rows — a verified SQLAlchemy 2.1.8 surprise."""
    CEmployee, CManager = _catalyst_jti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    statements = _sa_statements(sa_engine)
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.commit()
            sa_session.add(JTIManager(name="boss", level=3))
            sa_session.commit()

            session.execute(delete(CEmployee))
            session.commit()
            assert session.execute(sa_text("SELECT count(*) FROM jti_manager")).scalar_one() == 1

            sa_session.execute(sa.delete(JTIEmployee))
            sa_session.commit()
            deletes = [_norm(s) for s, _ in statements if s.lstrip().upper().startswith("DELETE")]
            assert deletes == ["DELETE FROM JTI_EMPLOYEE"]
            assert sa_session.execute(sa_text("SELECT count(*) FROM jti_manager")).scalar_one() == 1
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_JTI_MISSING
def test_jti_without_polymorphic_on_returns_base_instances() -> None:
    """Joined-table inheritance works with NO ``__mapper_args__`` at all —
    but parent queries return base-class instances (no discriminator to
    resolve the subclass)."""
    CEmployee, CManager = _catalyst_np_jti_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            session.add(CManager(name="boss", level=3))
            session.commit()
            sa_session.add(NPJManager(name="boss", level=3))
            sa_session.commit()

            parents = list(session.scalars(select(CEmployee)))
            assert [type(o).__name__ for o in parents] == ["CEmployee"]
            children = list(session.scalars(select(CManager)))
            assert [type(o).__name__ for o in children] == ["CManager"]

            sa_parents = list(sa_session.scalars(select(NPJEmployee)))
            assert [type(o).__name__ for o in sa_parents] == ["NPJEmployee"]
            sa_children = list(sa_session.scalars(select(NPJManager)))
            assert [type(o).__name__ for o in sa_children] == ["NPJManager"]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# Shared behavior
# ---------------------------------------------------------------------------


def test_abstract_mixin_columns_land_on_concrete_child() -> None:
    """``__abstract__ = True`` intermediate bases contribute their columns to
    concrete subclasses and are themselves unmapped (no ``__table__``)."""
    CAbsBase, CAbsChild = _catalyst_abstract_models()
    assert getattr(CAbsBase, "__table__", None) is None
    assert {c.name for c in CAbsChild.__table__.columns} == {"id", "stamp"}

    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    try:
        CAbsChild.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        catalyst_ddl = str(SACreateTable(CAbsChild.__table__).compile(dialect=sa_sqlite.dialect()))
        sa_ddl = str(SACreateTable(AbsChild.__table__).compile(dialect=sa_sqlite.dialect()))
        assert "stamp" in catalyst_ddl and "stamp" in sa_ddl
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_relationship_to_inheritance_parent() -> None:
    """A ``ForeignKey`` to the STI parent table reaches subclass rows; the
    lazy load resolves the right subclass via the discriminator."""
    CEmployee, CManager, CDept = _catalyst_rel_models()
    catalyst_engine = create_engine(":memory:")
    sa_engine = sa_create_engine("sqlite://")
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        SABase.metadata.create_all(sa_engine)
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            mgr = CManager(name="boss")
            session.add(CDept(head=mgr))
            session.commit()
            sa_mgr = STIManager(name="boss")
            sa_session.add(RelDept(head=sa_mgr))
            sa_session.commit()

            head = session.scalars(select(CDept)).one().head
            assert type(head).__name__ == "CManager"
            sa_head = sa_session.scalars(select(RelDept)).one().head
            assert type(sa_head).__name__ == "STIManager"
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@_DATACLASS_MISSING
def test_mapped_as_dataclass_discriminator_init_false() -> None:
    """``MappedAsDataclass`` + STI: the discriminator must be ``init=False``
    (otherwise ``__init__`` requires it); flush still auto-sets the identity."""
    assert _CATALYST_DATACLASS is not None

    class CBase(_CATALYST_DATACLASS, DeclarativeBase):  # type: ignore[misc]
        pass

    class CEmployee(CBase):
        __tablename__ = "dc_person"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        name: Mapped[str]
        type: Mapped[str] = mapped_column(init=False)
        __mapper_args__ = {
            "polymorphic_on": "type",
            "polymorphic_identity": "employee",
        }

    class CManager(CEmployee):
        __mapper_args__ = {"polymorphic_identity": "manager"}

    catalyst_engine = create_engine(":memory:")
    try:
        CEmployee.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            session.add(CManager(name="boss"))
            session.commit()
            row = session.execute(sa_text("SELECT name, type FROM dc_person")).one()
            assert row == ("boss", "manager")
    finally:
        catalyst_engine.dispose()
