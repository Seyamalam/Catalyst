"""Many-to-many / ``secondary`` relationship parity against SQLAlchemy 2.1.

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3
(``docs/many-to-many-ground-truth.md``). The whole module skips until
``relationship()`` accepts ``secondary``; association-table ``Table`` DDL
already compiles identically today but is covered here for completeness.

Key verified semantics encoded below:

- Association table: composite ``PRIMARY KEY (a_id, b_id)`` plus one
  ``FOREIGN KEY(col) REFERENCES tbl (col)`` clause per FK column — identical
  to a hand-written ``Table``. A non-PK association table compiles the same
  way with no warning (ORM behavior with it is deferred).
- ``secondary`` accepts a ``Table`` object AND a string table name (resolved
  against the declarative metadata at mapper-configuration time). Presence of
  ``secondary`` forces ``direction=MANYTOMANY`` and collection ``uselist``;
  explicit ``uselist=False`` is allowed (scalar m2m).
- Lazy m2m load emits an IMPLICIT comma join, bind-first:
  ``SELECT b.id, b.name FROM b, assoc WHERE ? = assoc.a_id AND b.id =
  assoc.b_id`` — NOT a ``JOIN`` keyword. Empty association still emits the
  SELECT and returns ``[]``.
- Flush of ``a.bs = [b1, b2]`` inserts parents, then children, then ONE
  batched ``INSERT INTO assoc (a_id, b_id) VALUES (?, ?)`` (executemany,
  param order follows UOW dependency ordering — observed ``[(2,3),(1,1),
  (1,2)]``). Appending a transient ``b`` to a persistent ``a`` cascades
  save-update: ``b`` lands in ``session.new`` and is INSERTed before the
  assoc row.
- ``a.bs.remove(b)`` flushes ONLY ``DELETE FROM assoc WHERE assoc.a_id = ?
  AND assoc.b_id = ?`` — the ``b`` row survives. ``delete-orphan`` on a
  many-to-many relationship RAISES ``ArgumentError`` at mapper configure
  unless ``single_parent=True``; with it, removal emits the assoc DELETE
  then ``DELETE FROM b WHERE b.id = ?``, and deleting the parent deletes
  assoc rows, the parent, then children.
- Back-population is bidirectional at assignment time; no FK values are
  stored on either object — association rows materialize only at flush.
- ``selectinload(A.bs)`` emits a SECOND query
  ``SELECT assoc.a_id, b.id, b.name FROM assoc JOIN b ON b.id = assoc.b_id
  WHERE assoc.a_id IN (?, ?)`` (association table FIRST in FROM, grouping
  column first in SELECT list). ``joinedload`` emits ONE query
  ``FROM a LEFT OUTER JOIN (assoc AS assoc_1 JOIN b AS b_1 ON ...) ON
  a.id = assoc_1.a_id`` — parenthesized join — and still requires
  ``Result.unique()`` for collections.
- ``secondary`` with extra columns: plain relationship writes only the two
  FK columns; extras fall to ``server_default``/NULL. The association-object
  pattern (``secondary`` + mapped class) is deferred.
- Self-referential m2m REQUIRES ``primaryjoin``/``secondaryjoin`` (both FK
  paths ambiguous otherwise → ``AmbiguousForeignKeysError`` at configure).
  Verified working form uses ``left_id``/``right_id`` columns and a
  ``backref``.
- ``viewonly=True`` loads through the association but NEVER writes assoc
  rows (append is silently dropped at flush). Two relationships sharing one
  association table emit ``SAWarning`` "will copy column ... conflicts with
  relationship(s)" at configure but still work.

Catalyst-side models are defined lazily inside each test/factory because
``secondary`` does not exist yet; the whole module skips until it lands.
SQLAlchemy models are module-level (the negative self-referential probe
uses an isolated base + GC cleanup so it cannot poison the global mapper
registry for other suites).
"""

from __future__ import annotations

import gc
import logging
import warnings
from typing import Any

import catalyst
import catalyst.expression  # noqa: F401  (ensures submodule attribute exists)
import catalyst.orm  # noqa: F401
import pytest
import sqlalchemy as sa
from catalyst import (
    Column,
    DeclarativeBase,
    ForeignKey,
    Integer,
    Mapped,
    Session,
    String,
    Table,
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
_CATALYST_SELECTINLOAD = _catalyst_symbol("selectinload")
_CATALYST_JOINEDLOAD = _catalyst_symbol("joinedload")


def _relationship_accepts_secondary() -> bool:
    """Probe whether ``relationship()`` accepts the ``secondary`` kwarg."""
    if _CATALYST_RELATIONSHIP is None:
        return False
    try:
        _CATALYST_RELATIONSHIP(secondary="assoc")
    except Exception:
        return False
    return True


_M2M_MISSING = pytest.mark.skipif(
    not _relationship_accepts_secondary(),
    reason="catalyst relationship(secondary=...) is not implemented yet",
)

pytestmark = _M2M_MISSING


# ---------------------------------------------------------------------------
# SQLAlchemy reference models
# ---------------------------------------------------------------------------


class SABase(SADeclarativeBase):
    pass


M2MAssoc = sa.Table(
    "m2m_assoc",
    SABase.metadata,
    sa.Column("a_id", sa.Integer, sa.ForeignKey("m2m_a.id"), primary_key=True),
    sa.Column("b_id", sa.Integer, sa.ForeignKey("m2m_b.id"), primary_key=True),
)


class SAM2MA(SABase):
    __tablename__ = "m2m_a"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    # String form of secondary (resolved against metadata at configure time).
    bs: SAMapped[list[SAM2MB]] = sa_relationship(secondary="m2m_assoc", back_populates="as_")


class SAM2MB(SABase):
    __tablename__ = "m2m_b"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    # Table-object form of secondary.
    as_: SAMapped[list[SAM2MA]] = sa_relationship(secondary=M2MAssoc, back_populates="bs")


M2MScalarAssoc = sa.Table(
    "m2m_sassoc",
    SABase.metadata,
    sa.Column("sa_id", sa.Integer, sa.ForeignKey("m2m_sa.id"), primary_key=True),
    sa.Column("sb_id", sa.Integer, sa.ForeignKey("m2m_sb.id"), primary_key=True),
)


class SAM2MScalarA(SABase):
    __tablename__ = "m2m_sa"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    bs: SAMapped[SAM2MScalarB] = sa_relationship(secondary=M2MScalarAssoc, uselist=False)


class SAM2MScalarB(SABase):
    __tablename__ = "m2m_sb"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)


M2MOrphanAssoc = sa.Table(
    "m2m_oassoc",
    SABase.metadata,
    sa.Column("oa_id", sa.Integer, sa.ForeignKey("m2m_oa.id"), primary_key=True),
    sa.Column("ob_id", sa.Integer, sa.ForeignKey("m2m_ob.id"), primary_key=True),
)


class SAM2MOrphanA(SABase):
    __tablename__ = "m2m_oa"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    # delete-orphan on m2m REQUIRES single_parent=True (verified ArgumentError).
    bs: SAMapped[list[SAM2MOrphanB]] = sa_relationship(
        secondary=M2MOrphanAssoc,
        back_populates="as_",
        cascade="all, delete-orphan",
        single_parent=True,
    )


class SAM2MOrphanB(SABase):
    __tablename__ = "m2m_ob"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    as_: SAMapped[list[SAM2MOrphanA]] = sa_relationship(
        secondary=M2MOrphanAssoc, back_populates="bs"
    )


M2MExtraAssoc = sa.Table(
    "m2m_eassoc",
    SABase.metadata,
    sa.Column("ea_id", sa.Integer, sa.ForeignKey("m2m_ea.id"), primary_key=True),
    sa.Column("eb_id", sa.Integer, sa.ForeignKey("m2m_eb.id"), primary_key=True),
    sa.Column("extra", sa.String, nullable=False, server_default="dflt"),
)


class SAM2MExtraA(SABase):
    __tablename__ = "m2m_ea"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    bs: SAMapped[list[SAM2MExtraB]] = sa_relationship(secondary=M2MExtraAssoc, back_populates="as_")


class SAM2MExtraB(SABase):
    __tablename__ = "m2m_eb"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    as_: SAMapped[list[SAM2MExtraA]] = sa_relationship(secondary=M2MExtraAssoc, back_populates="bs")


M2MFriendship = sa.Table(
    "m2m_friendship",
    SABase.metadata,
    sa.Column("left_id", sa.Integer, sa.ForeignKey("m2m_person.id"), primary_key=True),
    sa.Column("right_id", sa.Integer, sa.ForeignKey("m2m_person.id"), primary_key=True),
)


class SAM2MPerson(SABase):
    __tablename__ = "m2m_person"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    friends: SAMapped[list[SAM2MPerson]] = sa_relationship(
        secondary=M2MFriendship,
        primaryjoin=id == M2MFriendship.c.left_id,
        secondaryjoin=id == M2MFriendship.c.right_id,
        backref="friended_by",
    )


M2MViewonlyAssoc = sa.Table(
    "m2m_vassoc",
    SABase.metadata,
    sa.Column("va_id", sa.Integer, sa.ForeignKey("m2m_va.id"), primary_key=True),
    sa.Column("vb_id", sa.Integer, sa.ForeignKey("m2m_vb.id"), primary_key=True),
)


class SAM2MViewonlyA(SABase):
    __tablename__ = "m2m_va"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    bs: SAMapped[list[SAM2MViewonlyB]] = sa_relationship(secondary=M2MViewonlyAssoc, viewonly=True)


class SAM2MViewonlyB(SABase):
    __tablename__ = "m2m_vb"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)


# Negative/edge-case probe models on ISOLATED bases: their mappers only
# configure when instantiated inside the tests below (declarative configure
# is lazy and per-registry), so merely defining them here is inert. The
# delete-orphan and ambiguous-self-ref cases raise at configure; the tests
# drop the module globals + gc.collect() afterwards so a global
# ``configure_mappers()`` can never trip over the broken registry.


class SABadBase(SADeclarativeBase):
    pass


M2MBadAssoc = sa.Table(
    "m2m_badassoc",
    SABadBase.metadata,
    sa.Column("xa_id", sa.Integer, sa.ForeignKey("m2m_xa.id"), primary_key=True),
    sa.Column("xb_id", sa.Integer, sa.ForeignKey("m2m_xb.id"), primary_key=True),
)


class SABadA(SABadBase):
    __tablename__ = "m2m_xa"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    bs: SAMapped[list[SABadB]] = sa_relationship(
        secondary=M2MBadAssoc, cascade="all, delete-orphan", back_populates="as_"
    )


class SABadB(SABadBase):
    __tablename__ = "m2m_xb"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    as_: SAMapped[list[SABadA]] = sa_relationship(secondary=M2MBadAssoc, back_populates="bs")


class SABadPersonBase(SADeclarativeBase):
    pass


M2MBadFriendship = sa.Table(
    "m2m_badfriend",
    SABadPersonBase.metadata,
    sa.Column("l", sa.Integer, sa.ForeignKey("m2m_badperson.id"), primary_key=True),
    sa.Column("r", sa.Integer, sa.ForeignKey("m2m_badperson.id"), primary_key=True),
)


class SABadPerson(SABadPersonBase):
    __tablename__ = "m2m_badperson"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    friends: SAMapped[list[SABadPerson]] = sa_relationship(secondary=M2MBadFriendship)


class SAOvBase(SADeclarativeBase):
    pass


M2MOvAssoc = sa.Table(
    "m2m_ovassoc",
    SAOvBase.metadata,
    sa.Column("xa_id", sa.Integer, sa.ForeignKey("m2m_oxa.id"), primary_key=True),
    sa.Column("xb_id", sa.Integer, sa.ForeignKey("m2m_oxb.id"), primary_key=True),
)


class SAOvA(SAOvBase):
    __tablename__ = "m2m_oxa"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    bs: SAMapped[list[SAOvB]] = sa_relationship(secondary=M2MOvAssoc)
    cs: SAMapped[list[SAOvB]] = sa_relationship(secondary=M2MOvAssoc)


class SAOvB(SAOvBase):
    __tablename__ = "m2m_oxb"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)


# ---------------------------------------------------------------------------
# Catalyst model factories (defined lazily so collection stays clean)
# ---------------------------------------------------------------------------


def _catalyst_m2m_models() -> tuple[type[Any], type[Any]]:
    """Fresh A/B pair with an association table mirroring the SA models."""
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    class MBBase(DeclarativeBase):
        pass

    assoc = Table(
        "m2m_assoc",
        MBBase.metadata,
        Column("a_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_a.id")),
        Column("b_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_b.id")),
    )

    class CA(MBBase):
        __tablename__ = "m2m_a"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        bs: Mapped[list[CB]] = relationship(secondary=assoc, back_populates="as_")

    class CB(MBBase):
        __tablename__ = "m2m_b"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        as_: Mapped[list[CA]] = relationship(secondary="m2m_assoc", back_populates="bs")

    return CA, CB


def _catalyst_scalar_m2m_models() -> tuple[type[Any], type[Any]]:
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    class SBBase(DeclarativeBase):
        pass

    assoc = Table(
        "m2m_sassoc",
        SBBase.metadata,
        Column("sa_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_sa.id")),
        Column("sb_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_sb.id")),
    )

    class CSA(SBBase):
        __tablename__ = "m2m_sa"
        id: Mapped[int] = mapped_column(primary_key=True)
        bs: Mapped[CSB] = relationship(secondary=assoc, uselist=False)

    class CSB(SBBase):
        __tablename__ = "m2m_sb"
        id: Mapped[int] = mapped_column(primary_key=True)

    return CSA, CSB


def _catalyst_orphan_m2m_models() -> tuple[type[Any], type[Any]]:
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    class OBBase(DeclarativeBase):
        pass

    assoc = Table(
        "m2m_oassoc",
        OBBase.metadata,
        Column("oa_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_oa.id")),
        Column("ob_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_ob.id")),
    )

    class COA(OBBase):
        __tablename__ = "m2m_oa"
        id: Mapped[int] = mapped_column(primary_key=True)
        bs: Mapped[list[COB]] = relationship(
            secondary=assoc,
            back_populates="as_",
            cascade="all, delete-orphan",
            single_parent=True,
        )

    class COB(OBBase):
        __tablename__ = "m2m_ob"
        id: Mapped[int] = mapped_column(primary_key=True)
        as_: Mapped[list[COA]] = relationship(secondary=assoc, back_populates="bs")

    return COA, COB


def _catalyst_extra_m2m_models() -> tuple[type[Any], type[Any]]:
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    class EBBase(DeclarativeBase):
        pass

    assoc = Table(
        "m2m_eassoc",
        EBBase.metadata,
        Column("ea_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_ea.id")),
        Column("eb_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_eb.id")),
        Column("extra", String, nullable=False, server_default="dflt"),
    )

    class CEA(EBBase):
        __tablename__ = "m2m_ea"
        id: Mapped[int] = mapped_column(primary_key=True)
        bs: Mapped[list[CEB]] = relationship(secondary=assoc, back_populates="as_")

    class CEB(EBBase):
        __tablename__ = "m2m_eb"
        id: Mapped[int] = mapped_column(primary_key=True)
        as_: Mapped[list[CEA]] = relationship(secondary=assoc, back_populates="bs")

    return CEA, CEB


def _catalyst_selfref_m2m_model() -> type[Any]:
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    class SRBase(DeclarativeBase):
        pass

    friendship = Table(
        "m2m_friendship",
        SRBase.metadata,
        Column("left_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_person.id")),
        Column("right_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_person.id")),
    )

    class CPerson(SRBase):
        __tablename__ = "m2m_person"
        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        friends: Mapped[list[CPerson]] = relationship(
            secondary=friendship,
            primaryjoin=id == friendship.c.left_id,
            secondaryjoin=id == friendship.c.right_id,
            backref="friended_by",
        )

    return CPerson


def _catalyst_viewonly_m2m_models() -> tuple[type[Any], type[Any]]:
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    class VBBase(DeclarativeBase):
        pass

    assoc = Table(
        "m2m_vassoc",
        VBBase.metadata,
        Column("va_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_va.id")),
        Column("vb_id", Integer, primary_key=True, foreign_key=ForeignKey("m2m_vb.id")),
    )

    class CVA(VBBase):
        __tablename__ = "m2m_va"
        id: Mapped[int] = mapped_column(primary_key=True)
        bs: Mapped[list[CVB]] = relationship(secondary=assoc, viewonly=True)

    class CVB(VBBase):
        __tablename__ = "m2m_vb"
        id: Mapped[int] = mapped_column(primary_key=True)

    return CVA, CVB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _engines() -> tuple[Any, Any, type[Any], type[Any]]:
    """(catalyst_engine, sa_engine, CA, CB) with both schemas created."""
    CA, CB = _catalyst_m2m_models()
    catalyst_engine = create_engine(":memory:", echo=True)
    CA.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    return catalyst_engine, sa_engine, CA, CB


def _seed_m2m(session: Any, A: type[Any], B: type[Any], prefix: str = "") -> None:
    """a1->b1,b2 ; a2->b3 ; a3 empty (explicit ids for stable assertions)."""
    a1, a2, a3 = (
        A(id=1, name=f"{prefix}a1"),
        A(id=2, name=f"{prefix}a2"),
        A(id=3, name=f"{prefix}a3"),
    )
    b1, b2, b3 = (
        B(id=1, name=f"{prefix}b1"),
        B(id=2, name=f"{prefix}b2"),
        B(id=3, name=f"{prefix}b3"),
    )
    a1.bs = [b1, b2]
    a2.bs.append(b3)
    for obj in (a1, a2, a3, b1, b2, b3):
        session.add(obj)
    session.commit()


def _seed_sa_m2m(session: SASession, prefix: str = "") -> None:
    a1, a2, a3 = (
        SAM2MA(id=1, name=f"{prefix}a1"),
        SAM2MA(id=2, name=f"{prefix}a2"),
        SAM2MA(id=3, name=f"{prefix}a3"),
    )
    b1, b2, b3 = (
        SAM2MB(id=1, name=f"{prefix}b1"),
        SAM2MB(id=2, name=f"{prefix}b2"),
        SAM2MB(id=3, name=f"{prefix}b3"),
    )
    a1.bs = [b1, b2]
    a2.bs.append(b3)
    for obj in (a1, a2, a3, b1, b2, b3):
        session.add(obj)
    session.commit()


def _sa_statements(engine: Any) -> list[tuple[str, Any]]:
    statements: list[tuple[str, Any]] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        statements.append((statement, parameters))

    return statements


def _catalyst_selects(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "catalyst.engine" and "SELECT" in record.getMessage().upper()
    ]


def _sa_selects(statements: list[tuple[str, Any]]) -> list[str]:
    return [s for s, _ in statements if s.lstrip().upper().startswith("SELECT")]


# ---------------------------------------------------------------------------
# Association table DDL
# ---------------------------------------------------------------------------


def test_association_table_ddl_matches_sqlalchemy() -> None:
    """Composite-PK association ``Table`` compiles to the same DDL SA emits:
    ``PRIMARY KEY (a_id, b_id)`` plus one ``FOREIGN KEY`` clause per column.
    A non-PK variant compiles identically (no warning either side)."""
    CA, _CB = _catalyst_m2m_models()
    # The factory's assoc table is reachable via the class metadata.
    catalyst_assoc = CA.metadata.tables["m2m_assoc"]
    compiled = catalyst_assoc.compile_ddl()
    sa_compiled = SACreateTable(M2MAssoc).compile(dialect=sa_sqlite.dialect())
    assert compiled.string == str(sa_compiled)
    assert "PRIMARY KEY (a_id, b_id)" in compiled.string
    assert "FOREIGN KEY(a_id) REFERENCES m2m_a (id)" in compiled.string
    assert "FOREIGN KEY(b_id) REFERENCES m2m_b (id)" in compiled.string

    # Non-PK association table: same shape minus the PRIMARY KEY clause.
    md = catalyst_assoc.metadata
    plain = Table(
        "m2m_plain",
        md,
        Column("a_id", Integer, foreign_key=ForeignKey("m2m_a.id")),
        Column("b_id", Integer, foreign_key=ForeignKey("m2m_b.id")),
    )
    sa_plain = sa.Table(
        "m2m_plain",
        SABase.metadata,
        sa.Column("a_id", sa.Integer, sa.ForeignKey("m2m_a.id")),
        sa.Column("b_id", sa.Integer, sa.ForeignKey("m2m_b.id")),
    )
    assert plain.compile_ddl().string == str(
        SACreateTable(sa_plain).compile(dialect=sa_sqlite.dialect())
    )


# ---------------------------------------------------------------------------
# secondary resolution / direction inference
# ---------------------------------------------------------------------------


def test_secondary_table_forms_and_direction() -> None:
    """``secondary`` accepts a ``Table`` object (CB.as_) and a string table
    name (CA.bs); both configure as ``MANYTOMANY`` collections."""
    CA, CB = _catalyst_m2m_models()
    catalyst_engine = create_engine(":memory:")
    CA.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_m2m(session, CA, CB)
            _seed_sa_m2m(sa_session)

            a = session.get(CA, 1)
            assert sorted(b.name for b in a.bs) == ["b1", "b2"]
            b = session.get(CB, 1)
            assert [x.name for x in b.as_] == ["a1"]

            sa_a = sa_session.get(SAM2MA, 1)
            assert sorted(x.name for x in sa_a.bs) == ["b1", "b2"]
            sa_b = sa_session.get(SAM2MB, 1)
            assert [x.name for x in sa_b.as_] == ["a1"]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_scalar_many_to_many_uselist_false() -> None:
    """``uselist=False`` + ``secondary`` configures a scalar m2m relationship
    (verified: ``direction=MANYTOMANY, uselist=False``)."""
    CSA, CSB = _catalyst_scalar_m2m_models()
    catalyst_engine = create_engine(":memory:")
    CSA.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            csa = CSA()
            csb = CSB()
            csa.bs = csb
            session.add(csa)
            session.commit()
            assert csa.bs.id == csb.id

            ssa = SAM2MScalarA()
            ssb = SAM2MScalarB()
            ssa.bs = ssb
            sa_session.add(ssa)
            sa_session.commit()
            assert ssa.bs.id == ssb.id
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# lazy loading
# ---------------------------------------------------------------------------


def test_lazy_m2m_load_sql_shape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``a.bs`` lazy load emits ONE SELECT with an IMPLICIT comma join —
    ``FROM b, assoc WHERE ? = assoc.a_id AND b.id = assoc.b_id`` (bind-first,
    no ``JOIN`` keyword). Reverse direction mirrors it. Empty association
    still emits the SELECT and returns ``[]``."""
    catalyst_engine, sa_engine, CA, _CB = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_m2m(session, CA, _CB)
            _seed_sa_m2m(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            a = session.get(CA, 1)
            assert sorted(b.name for b in a.bs) == ["b1", "b2"]
            catalyst_sql = _catalyst_selects(caplog)
            b_selects = [s for s in catalyst_sql if "m2m_b" in s]
            assert len(b_selects) == 1

            statements.clear()
            sa_a = sa_session.get(SAM2MA, 1)
            assert sorted(b.name for b in sa_a.bs) == ["b1", "b2"]
            sa_selects = _sa_selects(statements)
            m2m_selects = [s for s in sa_selects if "m2m_b" in s and "m2m_assoc" in s]
            assert len(m2m_selects) == 1
            assert "FROM m2m_b, m2m_assoc" in m2m_selects[0]
            assert "? = m2m_assoc.a_id" in m2m_selects[0]
            assert "m2m_b.id = m2m_assoc.b_id" in m2m_selects[0]

            # Empty association: SQL still emitted, result [].
            statements.clear()
            sa_a3 = sa_session.get(SAM2MA, 3)
            assert list(sa_a3.bs) == []
            sa_selects = _sa_selects(statements)
            assert any("m2m_assoc" in s for s in sa_selects)

            # Reverse direction lazy load.
            statements.clear()
            sa_b = sa_session.get(SAM2MB, 1)
            assert [a.name for a in sa_b.as_] == ["a1"]
            sa_selects = _sa_selects(statements)
            rev = [s for s in sa_selects if "m2m_a" in s and "m2m_assoc" in s]
            assert len(rev) == 1
            assert "FROM m2m_a, m2m_assoc" in rev[0]
            assert "? = m2m_assoc.b_id" in rev[0]
            assert "m2m_a.id = m2m_assoc.a_id" in rev[0]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# flush ordering / cascades
# ---------------------------------------------------------------------------


def test_m2m_append_flush_ordering(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Flush inserts parents, then children, then ONE batched
    ``INSERT INTO m2m_assoc``; appending a transient ``b`` cascades
    save-update (``session.new`` grows, INSERT precedes the assoc row)."""
    catalyst_engine, sa_engine, CA, CB = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            # Fresh pending graph: a1->b1,b2 and a2->b3 in one flush.
            ca1, ca2 = CA(name="a1"), CA(name="a2")
            cb1, cb2, cb3 = CB(name="b1"), CB(name="b2"), CB(name="b3")
            ca1.bs = [cb1, cb2]
            ca2.bs.append(cb3)
            session.add(ca1)
            session.add(ca2)
            session.flush()

            sa_a1, sa_a2 = SAM2MA(name="a1"), SAM2MA(name="a2")
            sa_b1, sa_b2, sa_b3 = SAM2MB(name="b1"), SAM2MB(name="b2"), SAM2MB(name="b3")
            sa_a1.bs = [sa_b1, sa_b2]
            sa_a2.bs.append(sa_b3)
            sa_session.add(sa_a1)
            sa_session.add(sa_a2)

            statements.clear()
            sa_session.flush()
            sa_inserts = [s for s, _ in statements if s.lstrip().upper().startswith("INSERT")]
            # Parent INSERTs, child INSERTs, then association INSERTs last.
            assert sum("m2m_a " in s for s in sa_inserts) == 2
            assert sum("m2m_b " in s for s in sa_inserts) == 3
            assoc_inserts = [i for i, s in enumerate(sa_inserts) if "m2m_assoc" in s]
            assert len(assoc_inserts) == 1
            assert assoc_inserts[0] == len(sa_inserts) - 1
            # Single batched multi-row INSERT (executemany parameter list).
            assoc_stmt, assoc_params = next((s, p) for s, p in statements if "m2m_assoc" in s)
            assert "INSERT INTO m2m_assoc (a_id, b_id) VALUES (?, ?)" in assoc_stmt
            assert isinstance(assoc_params, list)
            assert len(assoc_params) == 3

            # Transient child appended to persistent parent cascades.
            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            pa = session.get(CA, ca1.id)
            transient_b = CB(name="btransient")
            pa.bs.append(transient_b)
            assert transient_b in list(session.new)
            statements.clear()
            session.flush()

            sa_pa = sa_session.get(SAM2MA, sa_a1.id)
            sa_transient = SAM2MB(name="btransient")
            sa_pa.bs.append(sa_transient)
            assert sa_transient in sa_session.new
            statements.clear()
            sa_session.flush()
            sa_tail = [s for s, _ in statements if s.lstrip().upper().startswith("INSERT")]
            assert any("m2m_b " in s for s in sa_tail)
            assert any("m2m_assoc" in s for s in sa_tail)
            # Child INSERT precedes the association INSERT.
            assert next(i for i, s in enumerate(sa_tail) if "m2m_b " in s) < next(
                i for i, s in enumerate(sa_tail) if "m2m_assoc" in s
            )
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# remove / delete-orphan semantics
# ---------------------------------------------------------------------------


def test_m2m_remove_deletes_only_assoc_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``a.bs.remove(b)`` flushes ONLY ``DELETE FROM m2m_assoc WHERE
    a_id = ? AND b_id = ?`` — the ``b`` row itself survives, and both sides
    of the back-population clear at assignment time."""
    catalyst_engine, sa_engine, CA, CB = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_m2m(session, CA, CB)
            _seed_sa_m2m(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            a = session.get(CA, 1)
            b = session.get(CB, 1)
            assert len(a.bs) == 2 and len(b.as_) == 1
            a.bs.remove(b)
            assert b not in a.bs
            assert a not in b.as_
            session.flush()
            catalyst_deletes = [
                r.getMessage().upper()
                for r in caplog.records
                if r.name == "catalyst.engine" and "DELETE" in r.getMessage().upper()
            ]
            assert all("DELETE FROM M2M_B " not in d for d in catalyst_deletes)

            statements.clear()
            sa_a = sa_session.get(SAM2MA, 1)
            sa_b = sa_session.get(SAM2MB, 1)
            sa_a.bs.remove(sa_b)
            sa_session.flush()
            sa_deletes = [(s, p) for s, p in statements if s.lstrip().upper().startswith("DELETE")]
            assert len(sa_deletes) == 1
            stmt, params = sa_deletes[0]
            assert stmt == "DELETE FROM m2m_assoc WHERE m2m_assoc.a_id = ? AND m2m_assoc.b_id = ?"
            assert tuple(params) == (1, 1)
            # The b row is untouched.
            assert sa_session.get(SAM2MB, 1) is not None
            assert (
                sa_session.execute(
                    sa_text("SELECT count(*) FROM m2m_assoc WHERE a_id = 1 AND b_id = 1")
                ).scalar_one()
                == 0
            )
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_m2m_delete_orphan_requires_single_parent() -> None:
    """SA-verified: ``delete-orphan`` on a many-to-many relationship raises
    ``ArgumentError`` at mapper configure UNLESS ``single_parent=True``.
    With it, collection removal deletes the assoc row AND the child row;
    deleting the parent deletes assoc rows, parent, then children."""
    relationship = _CATALYST_RELATIONSHIP
    assert relationship is not None

    # --- SQLAlchemy side: the error itself ---
    try:
        with pytest.raises(sa.exc.ArgumentError, match="delete-orphan"):
            SABadA(id=1)  # triggers lazy mapper configuration
    finally:
        # Drop the broken registry so a global configure_mappers() can never
        # trip over it for other suites in the session.
        for name in ("SABadA", "SABadB", "SABadBase", "M2MBadAssoc"):
            globals().pop(name, None)
        gc.collect()

    # --- SQLAlchemy side: single_parent=True semantics ---
    COA, COB = _catalyst_orphan_m2m_models()
    catalyst_engine = create_engine(":memory:")
    COA.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            coa = COA()
            cob1, cob2 = COB(), COB()
            coa.bs = [cob1, cob2]
            session.add(coa)
            session.commit()

            soa = SAM2MOrphanA()
            sob1, sob2 = SAM2MOrphanB(), SAM2MOrphanB()
            soa.bs = [sob1, sob2]
            sa_session.add(soa)
            sa_session.commit()
            soa_id = soa.id

            # Removal: assoc DELETE then child DELETE (verified order).
            statements.clear()
            sa_oa = sa_session.get(SAM2MOrphanA, soa_id)
            sa_oa.bs.pop()
            sa_session.flush()
            sa_writes = [
                (s, p)
                for s, p in statements
                if s.lstrip().upper().startswith(("DELETE", "INSERT", "UPDATE"))
            ]
            assert [s.split()[0] for s, _ in sa_writes] == ["DELETE", "DELETE"]
            assert "m2m_oassoc" in sa_writes[0][0]
            assert "m2m_ob " in sa_writes[1][0]
            assert sa_session.execute(sa_text("SELECT count(*) FROM m2m_ob")).scalar_one() == 1
            assert sa_session.execute(sa_text("SELECT count(*) FROM m2m_oassoc")).scalar_one() == 1

            # Parent delete: assoc rows, parent, then children.
            statements.clear()
            sa_session.delete(sa_oa)
            sa_session.flush()
            sa_writes = [(s, p) for s, p in statements if s.lstrip().upper().startswith("DELETE")]
            kinds = [s.split("FROM")[1].strip().split()[0] for s, _ in sa_writes]
            assert kinds == ["m2m_oassoc", "m2m_oa", "m2m_ob"]
            assert sa_session.execute(sa_text("SELECT count(*) FROM m2m_ob")).scalar_one() == 0
            assert sa_session.execute(sa_text("SELECT count(*) FROM m2m_oassoc")).scalar_one() == 0
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# back-population
# ---------------------------------------------------------------------------


def test_m2m_back_populates_both_directions() -> None:
    """``a.bs.append(b)`` updates ``b.as_`` at ASSIGNMENT time (and vice
    versa); no FK values live on either object — association rows only
    materialize at flush."""
    catalyst_engine, sa_engine, CA, CB = _engines()
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            _seed_m2m(session, CA, CB)
            _seed_sa_m2m(sa_session)

            ca = CA(name="ax")
            cb = CB(name="bx")
            session.add(ca)
            session.add(cb)
            session.flush()
            ca.bs.append(cb)
            assert cb in ca.bs
            assert ca in cb.as_
            session.commit()
            # Association row exists; objects carry no FK attributes.
            assert (
                session.execute(
                    sa_text(
                        f"SELECT count(*) FROM m2m_assoc WHERE a_id = {ca.id} AND b_id = {cb.id}"
                    )
                ).scalar_one()
                == 1
            )
            assert not hasattr(ca, "b_id") and not hasattr(cb, "a_id")

            sa_a = SAM2MA(name="ax")
            sa_b = SAM2MB(name="bx")
            sa_session.add(sa_a)
            sa_session.add(sa_b)
            sa_session.flush()
            sa_a.bs.append(sa_b)
            assert sa_b in sa_a.bs
            assert sa_a in sa_b.as_
            sa_session.commit()
            assert (
                sa_session.execute(
                    sa_text(
                        "SELECT count(*) FROM m2m_assoc "
                        f"WHERE a_id = {sa_a.id} AND b_id = {sa_b.id}"
                    )
                ).scalar_one()
                == 1
            )
            assert not hasattr(sa_a, "b_id") and not hasattr(sa_b, "a_id")
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# eager loading
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    _CATALYST_SELECTINLOAD is None or _CATALYST_JOINEDLOAD is None,
    reason="catalyst selectinload/joinedload loader options are not implemented yet",
)
def test_selectinload_m2m_batch_query(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``selectinload(A.bs)`` emits a second query with the ASSOCIATION table
    first: ``SELECT assoc.a_id, b.id, b.name FROM assoc JOIN b ON ... WHERE
    assoc.a_id IN (?, ?)`` — grouping column leads the SELECT list."""
    selectinload = _CATALYST_SELECTINLOAD
    assert selectinload is not None
    catalyst_engine, sa_engine, CA, _CB = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with (
            Session(catalyst_engine, expire_on_commit=False) as session,
            SASession(sa_engine, expire_on_commit=False) as sa_session,
        ):
            _seed_m2m(session, CA, _CB)
            _seed_sa_m2m(sa_session)
            # Detach the seeded graph so the loader strategies actually run.
            session.expunge_all()
            sa_session.expunge_all()

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            parents = session.scalars(select(CA).options(selectinload(CA.bs))).all()
            by_name = {p.name: sorted(b.name for b in p.bs) for p in parents}
            assert by_name == {"a1": ["b1", "b2"], "a2": ["b3"], "a3": []}
            catalyst_sql = _catalyst_selects(caplog)
            assert len(catalyst_sql) == 2
            assert "m2m_assoc" in catalyst_sql[1] and " IN " in catalyst_sql[1].upper()

            statements.clear()
            sa_parents = sa_session.scalars(
                sa.select(SAM2MA).options(sa_selectinload(SAM2MA.bs))
            ).all()
            sa_by_name = {p.name: sorted(b.name for b in p.bs) for p in sa_parents}
            assert sa_by_name == by_name
            sa_selects = _sa_selects(statements)
            assert len(sa_selects) == 2
            second = sa_selects[1]
            assert "FROM m2m_assoc JOIN m2m_b" in second
            assert "m2m_b.id = m2m_assoc.b_id" in second
            assert "m2m_assoc.a_id IN" in second
            # Association FK column leads the SELECT list for client grouping.
            assert second.lstrip().upper().startswith("SELECT M2M_ASSOC.A_ID")
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


@pytest.mark.skipif(
    _CATALYST_JOINEDLOAD is None,
    reason="catalyst joinedload loader option is not implemented yet",
)
def test_joinedload_m2m_parenthesized_join(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``joinedload(A.bs)`` emits ONE query joining through a PARENTHESIZED
    ``(assoc JOIN b)`` chain and requires ``.unique()`` for collections."""
    joinedload = _CATALYST_JOINEDLOAD
    assert joinedload is not None
    catalyst_engine, sa_engine, CA, _CB = _engines()
    statements = _sa_statements(sa_engine)
    try:
        with (
            Session(catalyst_engine, expire_on_commit=False) as session,
            SASession(sa_engine, expire_on_commit=False) as sa_session,
        ):
            _seed_m2m(session, CA, _CB)
            _seed_sa_m2m(sa_session)

            caplog.set_level(logging.INFO, logger="catalyst.engine")
            caplog.clear()
            parents = session.scalars(select(CA).options(joinedload(CA.bs))).unique().all()
            by_name = {p.name: sorted(b.name for b in p.bs) for p in parents}
            assert by_name == {"a1": ["b1", "b2"], "a2": ["b3"], "a3": []}
            catalyst_sql = _catalyst_selects(caplog)
            assert len(catalyst_sql) == 1
            assert "JOIN" in catalyst_sql[0].upper()

            statements.clear()
            sa_parents = (
                sa_session.scalars(sa.select(SAM2MA).options(sa_joinedload(SAM2MA.bs)))
                .unique()
                .all()
            )
            sa_by_name = {p.name: sorted(b.name for b in p.bs) for p in sa_parents}
            assert sa_by_name == by_name
            sa_selects = _sa_selects(statements)
            assert len(sa_selects) == 1
            stmt = sa_selects[0]
            # Verified SA 2.1.8 shape: LEFT OUTER JOIN of a parenthesized
            # association+target join.
            assert "LEFT OUTER JOIN (m2m_assoc" in stmt
            assert "JOIN m2m_b" in stmt
            assert "ON m2m_a.id = m2m_assoc_1.a_id" in stmt
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


# ---------------------------------------------------------------------------
# secondary edge cases
# ---------------------------------------------------------------------------


def test_secondary_with_extra_columns() -> None:
    """Plain ``secondary`` ignores extra association columns at write time:
    the assoc INSERT carries only the two FK columns; ``extra`` falls to its
    ``server_default``. (Association-object pattern deferred.)"""
    CEA, CEB = _catalyst_extra_m2m_models()
    catalyst_engine = create_engine(":memory:")
    CEA.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cea = CEA()
            ceb = CEB()
            cea.bs = [ceb]
            session.add(cea)
            session.commit()
            assert (
                session.execute(sa_text("SELECT extra FROM m2m_eassoc LIMIT 1")).scalar_one()
                == "dflt"
            )

            sea = SAM2MExtraA()
            seb = SAM2MExtraB()
            sea.bs = [seb]
            sa_session.add(sea)
            statements.clear()
            sa_session.flush()
            assoc_stmt, assoc_params = next((s, p) for s, p in statements if "m2m_eassoc" in s)
            assert "INSERT INTO m2m_eassoc (ea_id, eb_id) VALUES (?, ?)" in assoc_stmt
            assert "extra" not in assoc_stmt
            sa_session.commit()
            assert (
                sa_session.execute(sa_text("SELECT extra FROM m2m_eassoc LIMIT 1")).scalar_one()
                == "dflt"
            )
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_self_referential_m2m_requires_joins() -> None:
    """Self-referential m2m through a symmetric assoc table REQUIRES
    ``primaryjoin``/``secondaryjoin`` — without them SA raises
    ``AmbiguousForeignKeysError`` at configure. The broken classes live on an
    isolated base and are GC'd so the global mapper registry stays clean."""

    # --- SQLAlchemy side ---
    try:
        with pytest.raises(sa.exc.AmbiguousForeignKeysError):
            SABadPerson(id=1)
    finally:
        for name in ("SABadPerson", "SABadPersonBase", "M2MBadFriendship"):
            globals().pop(name, None)
        gc.collect()

    # --- Working form: explicit joins + backref ---
    CPerson = _catalyst_selfref_m2m_model()
    catalyst_engine = create_engine(":memory:")
    CPerson.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    try:
        with Session(catalyst_engine) as session, SASession(sa_engine) as sa_session:
            cp1, cp2 = CPerson(name="p1"), CPerson(name="p2")
            cp1.friends.append(cp2)
            session.add(cp1)
            session.commit()

            sp1, sp2 = SAM2MPerson(name="p1"), SAM2MPerson(name="p2")
            sp1.friends.append(sp2)
            sa_session.add(sp1)
            sa_session.commit()

            # Lazy self-ref load: implicit join, left_id binds first.
            statements = _sa_statements(sa_engine)
            sp1 = sa_session.get(SAM2MPerson, sp1.id)
            sa_session.expire(sp1, ["friends"])
            assert [f.name for f in sp1.friends] == ["p2"]
            friend_selects = [s for s, _ in statements if "m2m_friendship" in s]
            assert len(friend_selects) == 1
            assert "FROM m2m_person, m2m_friendship" in friend_selects[0]
            assert "? = m2m_friendship.left_id" in friend_selects[0]
            assert "m2m_person.id = m2m_friendship.right_id" in friend_selects[0]

            # Backref side sees the inverse link.
            sp2 = sa_session.get(SAM2MPerson, sp2.id)
            assert [f.name for f in sp2.friended_by] == ["p1"]
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_viewonly_secondary_never_writes() -> None:
    """``viewonly=True`` relationships load through the association but never
    INSERT/DELETE assoc rows — appends are silently dropped at flush."""
    CVA, CVB = _catalyst_viewonly_m2m_models()
    catalyst_engine = create_engine(":memory:")
    CVA.metadata.create_all(catalyst_engine)
    sa_engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(sa_engine)
    statements = _sa_statements(sa_engine)
    try:
        with Session(catalyst_engine), SASession(sa_engine) as sa_session:
            sva = SAM2MViewonlyA()
            svb = SAM2MViewonlyB()
            sva.bs.append(svb)
            sa_session.add(sva)
            statements.clear()
            sa_session.flush()
            assert not any("m2m_vassoc" in s for s, _ in statements)
            sa_session.commit()
            assert sa_session.execute(sa_text("SELECT count(*) FROM m2m_vassoc")).scalar_one() == 0

            # Lazy viewonly load still queries through the association.
            statements.clear()
            sva = sa_session.get(SAM2MViewonlyA, sva.id)
            assert list(sva.bs) == []
            assert any("m2m_vassoc" in s for s, _ in statements)
    finally:
        catalyst_engine.dispose()
        sa_engine.dispose()


def test_overlapping_secondary_relationships_warn() -> None:
    """Two relationships sharing one association table emit ``SAWarning``
    "will copy column ... conflicts with relationship(s)" at configure but
    still configure successfully."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        SAOvA(id=1)  # triggers mapper configuration -> warnings

    overlap_warnings = [w for w in caught if "conflicts with relationship" in str(w.message)]
    assert overlap_warnings, "expected SAWarning about overlapping secondary writes"
