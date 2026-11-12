"""``MappedAsDataclass`` parity against SQLAlchemy 2.1 (verified 2.1.8).

Ground truth gathered empirically against ``sqlalchemy==2.1.8`` + sqlite3;
full write-up in ``docs/m4-tail-ground-truth.md``. The whole module skips
until ``MappedAsDataclass`` exists on the catalyst side; every test body is
written against the verified SA 2.1.8 semantics so it becomes the proving
suite when the feature lands.

Key verified semantics encoded below:

- ``class U(MappedAsDataclass, Base)`` synthesizes a real
  ``dataclasses.dataclass`` (``__dataclass_params__`` = init/repr/eq True,
  order/frozen/kw_only False). ``MappedAsDataclass`` MUST precede the
  declarative base in the bases list — ``class U(Base, MappedAsDataclass)``
  raises ``ArgumentError`` ("does not specify SQLAlchemy native dataclass
  configuration").
- ``__init__`` fields are POSITIONAL_OR_KEYWORD by default;
  ``mapped_column(kw_only=True)`` makes a field KEYWORD_ONLY.
  ``init=False`` fields are excluded from ``__init__``.
- ``mapped_column(default=...)`` / ``default_factory=...`` materialize AT
  CONSTRUCTION (``U(name="a").age == 0`` before any flush); ``init=False``
  columns with defaults also materialize (``hidden == 7``), while an
  ``init=False`` PK with no default stays ``None``. The dataclass field
  default for a column default is the ``LoaderCallableStatus.DONT_SET``
  sentinel resolved by the generated ``__init__``.
- ``eq=True`` (default): ``__eq__`` compares ALL dataclass fields including
  ``init=False`` and ``repr=False`` ones (``compare=True`` everywhere),
  requires identical class, and leaves instances unhashable.
- ``repr=False`` on ``mapped_column`` omits the field from ``__repr__``;
  ``init=False`` fields still appear (``U(id=None, name='a', ..., hidden=7)``).
- ``__post_init__`` runs after generated ``__init__`` assignment.
- Field ordering follows dataclass rules: a non-default field after a
  defaulted one raises ``InvalidRequestError`` wrapping
  ``TypeError("non-default argument 'b' follows default argument")``;
  ``kw_only=True`` on the later field rescues the ordering.
- ``relationship()`` fields join ``__init__`` too: a uselist collection
  with no default is a REQUIRED argument (``P(name="x")`` → ``TypeError``),
  ``relationship(init=False)`` opts out, and scalar relationships/columns
  with defaults are optional (``DONT_SET`` → resolve to ``None``/default).
  Passing collection members through ``__init__`` wires ``back_populates``
  both ways at construction.
- Inheritance: a discriminator column marked ``init=False`` stays out of
  ``__init__``; the mapper still stamps the ``polymorphic_identity`` at
  construction (``Mgr(name="boss").type == "m"`` before flush) and INSERT.

Catalyst-side models are defined lazily inside each test because
``MappedAsDataclass`` does not exist yet; SA models are module-level.
"""

from __future__ import annotations

import dataclasses
import inspect
from typing import Any

import catalyst
import catalyst.expression  # noqa: F401  (ensures submodule attribute exists)
import catalyst.orm  # noqa: F401
import pytest
import sqlalchemy as sa
from catalyst import (
    DeclarativeBase,
    ForeignKey,
    Mapped,
    Session,
    create_engine,
    mapped_column,
    relationship,
    select,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.exc import ArgumentError as SAArgumentError
from sqlalchemy.exc import InvalidRequestError as SAInvalidRequestError
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import MappedAsDataclass
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column
from sqlalchemy.orm import relationship as sa_relationship


def _catalyst_symbol(name: str) -> Any:
    """Find ``name`` on whichever catalyst submodule exposes it."""
    for module in (catalyst, catalyst.expression, catalyst.orm):
        symbol = getattr(module, name, None)
        if symbol is not None:
            return symbol
    return None


_CATALYST_DATACLASS = _catalyst_symbol("MappedAsDataclass")

_DATACLASS_MISSING = pytest.mark.skipif(
    _CATALYST_DATACLASS is None,
    reason="catalyst MappedAsDataclass is not implemented yet",
)

pytestmark = _DATACLASS_MISSING


# ---------------------------------------------------------------------------
# SQLAlchemy reference models (always defined; SA is a dev dependency)
# ---------------------------------------------------------------------------


class SABase(SADeclarativeBase):
    pass


class SAUser(MappedAsDataclass, SABase):
    __tablename__ = "dc_users"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    name: SAMapped[str]
    age: SAMapped[int] = sa_mapped_column(default=0)
    nick: SAMapped[str | None] = sa_mapped_column(default_factory=lambda: "n", nullable=True)
    score: SAMapped[float] = sa_mapped_column(default=1.5, kw_only=True)
    hidden: SAMapped[int] = sa_mapped_column(default=7, init=False)
    descr: SAMapped[str] = sa_mapped_column(default="d", repr=False)


class SAP(MappedAsDataclass, SABase):
    __tablename__ = "dc_p"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    name: SAMapped[str]
    children: SAMapped[list[SAC]] = sa_relationship(back_populates="parent")


class SAC(MappedAsDataclass, SABase):
    __tablename__ = "dc_c"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    val: SAMapped[int]
    parent_id: SAMapped[int | None] = sa_mapped_column(
        sa.ForeignKey("dc_p.id"), default=None, nullable=True
    )
    parent: SAMapped[SAP | None] = sa_relationship(back_populates="children", default=None)


class SAPNoInit(MappedAsDataclass, SABase):
    __tablename__ = "dc_p2"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    name: SAMapped[str]
    children: SAMapped[list[SACNoInit]] = sa_relationship(back_populates="parent", init=False)


class SACNoInit(MappedAsDataclass, SABase):
    __tablename__ = "dc_c2"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    val: SAMapped[int]
    parent_id: SAMapped[int | None] = sa_mapped_column(
        sa.ForeignKey("dc_p2.id"), default=None, nullable=True
    )
    parent: SAMapped[SAPNoInit | None] = sa_relationship(back_populates="children", init=False)


class SAEmp(MappedAsDataclass, SADeclarativeBase):
    """Dataclass declarative base for the STI discriminator test."""


class SAEmployee(SAEmp):
    __tablename__ = "dc_emp"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    name: SAMapped[str]
    type: SAMapped[str] = sa_mapped_column(init=False)
    __mapper_args__ = {
        "polymorphic_on": "type",
        "polymorphic_identity": "employee",
    }


class SAManager(SAEmployee):
    __mapper_args__ = {"polymorphic_identity": "manager"}


class SAPostInit(MappedAsDataclass, SABase):
    __tablename__ = "dc_post"

    id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
    name: SAMapped[str]
    upper: SAMapped[str] = sa_mapped_column(init=False)

    def __post_init__(self) -> None:
        self.upper = self.name.upper()


def _catalyst_engine() -> Any:
    return create_engine(":memory:")


def _sa_engine() -> Any:
    return sa_create_engine("sqlite://")


def _catalyst_models() -> tuple[type[Any], type[Any]]:
    """Lazily define the catalyst dataclass models (feature-gated)."""
    DCBase = type("DCBase", (_CATALYST_DATACLASS, DeclarativeBase), {})

    class CUser(DCBase):
        __tablename__ = "dc_users"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        name: Mapped[str]
        age: Mapped[int] = mapped_column(default=0)
        nick: Mapped[str | None] = mapped_column(default_factory=lambda: "n", nullable=True)
        score: Mapped[float] = mapped_column(default=1.5, kw_only=True)
        hidden: Mapped[int] = mapped_column(default=7, init=False)
        descr: Mapped[str] = mapped_column(default="d", repr=False)

    class CP(DCBase):
        __tablename__ = "dc_p"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        name: Mapped[str]
        children: Mapped[list[CC]] = relationship(back_populates="parent")

    class CC(DCBase):
        __tablename__ = "dc_c"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        val: Mapped[int]
        parent_id: Mapped[int | None] = mapped_column(
            ForeignKey("dc_p.id"), default=None, nullable=True
        )
        parent: Mapped[CP | None] = relationship(back_populates="children", default=None)

    return CUser, (CP, CC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_dataclass_is_real_dataclass() -> None:
    """``MappedAsDataclass`` produces a stdlib dataclass with SA defaults."""
    assert dataclasses.is_dataclass(SAUser)
    params = SAUser.__dataclass_params__
    assert params.init and params.repr and params.eq
    assert not params.order and not params.frozen and not params.kw_only

    CUser, _ = _catalyst_models()
    assert _CATALYST_DATACLASS is not None
    assert dataclasses.is_dataclass(CUser)
    cparams = CUser.__dataclass_params__
    assert cparams.init and cparams.repr and cparams.eq
    assert not cparams.order and not cparams.frozen and not cparams.kw_only


def test_init_signature_and_construction_defaults() -> None:
    """``__init__`` is positional-or-keyword; defaults materialize at
    construction; ``init=False`` fields are excluded but still defaulted."""
    sig = inspect.signature(SAUser.__init__)
    kinds = {name: p.kind for name, p in sig.parameters.items() if name != "self"}
    assert kinds["name"] == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["age"] == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert kinds["score"] == inspect.Parameter.KEYWORD_ONLY
    assert "id" not in kinds and "hidden" not in kinds

    u = SAUser(name="a")
    assert u.id is None
    assert u.age == 0  # default= materialized before flush
    assert u.nick == "n"  # default_factory= materialized
    assert u.score == 1.5
    assert u.hidden == 7  # init=False default still applied
    assert u.descr == "d"

    CUser, _ = _catalyst_models()
    csig = inspect.signature(CUser.__init__)
    ckinds = {n: p.kind for n, p in csig.parameters.items() if n != "self"}
    assert ckinds["name"] == inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert ckinds["score"] == inspect.Parameter.KEYWORD_ONLY
    assert "id" not in ckinds and "hidden" not in ckinds
    cu = CUser(name="a")
    assert cu.id is None and cu.age == 0 and cu.nick == "n"
    assert cu.score == 1.5 and cu.hidden == 7 and cu.descr == "d"


def test_positional_name_and_kw_only_enforcement() -> None:
    """The first field accepts positional args; ``kw_only`` fields reject
    positional passing (dataclass-generated ``TypeError``)."""
    assert SAUser("a").name == "a"
    # Positional capacity is name/age/nick/descr (``score`` is keyword-only);
    # one argument past it must raise. (Verified: SA 2.1.8 accepts four
    # positional arguments — descr is positional-or-keyword.)
    with pytest.raises(TypeError):
        SAUser("a", 1, "x", "d", 2.0)  # type: ignore[misc]

    CUser, _ = _catalyst_models()
    assert CUser("a").name == "a"
    with pytest.raises(TypeError):
        CUser("a", 1, "x", "d", 2.0)  # type: ignore[misc]


def test_eq_compares_all_fields_and_unhashable() -> None:
    """``eq=True`` compares every field (incl. ``init=False``/``repr=False``)
    within the same class; instances are unhashable."""
    a1, a2 = SAUser(name="x"), SAUser(name="x")
    assert a1 == a2
    assert a1 != SAUser(name="x", age=1)
    assert a1 != object()
    other = SAUser(name="x")
    other.hidden = 42  # init=False field participates in eq
    assert a1 != other
    hidden_eq = SAUser(name="x")
    assert hidden_eq == a1
    with pytest.raises(TypeError):
        hash(a1)

    CUser, _ = _catalyst_models()
    c1, c2 = CUser(name="x"), CUser(name="x")
    assert c1 == c2 and c1 != CUser(name="x", age=1)
    with pytest.raises(TypeError):
        hash(c1)


def test_repr_omits_repr_false_but_keeps_init_false() -> None:
    """``repr=False`` drops the field; ``init=False`` fields still render."""
    r = repr(SAUser(name="r"))
    assert "hidden=7" in r
    assert "descr=" not in r
    assert "id=None" in r

    CUser, _ = _catalyst_models()
    cr = repr(CUser(name="r"))
    assert "hidden=7" in cr
    assert "descr=" not in cr
    assert "id=None" in cr


def test_post_init_hook() -> None:
    """``__post_init__`` runs after field assignment."""
    assert SAPostInit(name="bob").upper == "BOB"

    DCBase = type("DCBase2", (_CATALYST_DATACLASS, DeclarativeBase), {})

    class CPost(DCBase):
        __tablename__ = "dc_post"
        id: Mapped[int] = mapped_column(primary_key=True, init=False)
        name: Mapped[str]
        upper: Mapped[str] = mapped_column(init=False)

        def __post_init__(self) -> None:
            self.upper = self.name.upper()

    assert CPost(name="bob").upper == "BOB"


def test_non_default_after_default_raises() -> None:
    """Dataclass ordering rules propagate: a required field after a
    defaulted one raises ``InvalidRequestError`` wrapping ``TypeError``."""
    with pytest.raises(SAInvalidRequestError, match="non-default argument"):

        class _Bad(MappedAsDataclass, SABase):
            __tablename__ = "dc_bad"
            id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
            a: SAMapped[int] = sa_mapped_column(default=1)
            b: SAMapped[str]

    # ``kw_only=True`` on the later field rescues the ordering.
    class _Ok(MappedAsDataclass, SABase):
        __tablename__ = "dc_ok"
        id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
        a: SAMapped[int] = sa_mapped_column(default=1)
        b: SAMapped[str] = sa_mapped_column(kw_only=True)

    assert _Ok(b="x").a == 1


def test_relationship_fields_join_init() -> None:
    """A uselist ``relationship`` with no default is a REQUIRED ``__init__``
    arg; ``init=False`` opts out; back_populates wires at construction."""
    sig = inspect.signature(SAP.__init__)
    params = {n: p for n, p in sig.parameters.items() if n != "self"}
    assert "children" in params
    assert params["children"].default is inspect.Parameter.empty

    with pytest.raises(TypeError):
        SAP(name="only")  # type: ignore[call-arg]

    p = SAP(name="p1", children=[SAC(val=1)])
    assert p.children[0].parent is p  # back_populates at construction

    c = SAC(val=2, parent=SAP(name="p2", children=[]))
    assert c.parent.name == "p2"
    assert c.parent.children == [c]

    # init=False relationship drops out of __init__ entirely.
    sig2 = inspect.signature(SAPNoInit.__init__)
    assert "children" not in sig2.parameters
    p2 = SAPNoInit(name="x")
    assert p2.children == []


def test_discriminator_init_false_still_stamps_identity() -> None:
    """STI discriminator marked ``init=False`` stays out of ``__init__``;
    the mapper stamps ``polymorphic_identity`` at construction and flush."""
    sig = inspect.signature(SAEmployee.__init__)
    assert "type" not in sig.parameters
    m = SAManager(name="boss")
    assert m.type == "manager"  # stamped before flush

    catalyst_engine = _catalyst_engine()
    try:
        DCBase = type("DCBase3", (_CATALYST_DATACLASS, DeclarativeBase), {})

        class CEmp(DCBase):
            __tablename__ = "dc_emp"
            id: Mapped[int] = mapped_column(primary_key=True, init=False)
            name: Mapped[str]
            type: Mapped[str] = mapped_column(init=False)
            __mapper_args__ = {
                "polymorphic_on": "type",
                "polymorphic_identity": "employee",
            }

        class CMgr(CEmp):
            __mapper_args__ = {"polymorphic_identity": "manager"}

        CEmp.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            csig = inspect.signature(CMgr.__init__)
            assert "type" not in csig.parameters
            mgr = CMgr(name="boss")
            session.add(mgr)
            session.commit()
            row = session.execute(sa.text("SELECT name, type FROM dc_emp")).one()
            assert row == ("boss", "manager")
    finally:
        catalyst_engine.dispose()


def test_flush_commit_roundtrip_and_loaded_eq() -> None:
    """Dataclass instances flush/commit like normal mapped instances; a
    re-loaded row compares equal by dataclass ``__eq__``."""
    sa_engine = _sa_engine()
    try:
        SABase.metadata.create_all(sa_engine)
        with SASession(sa_engine) as session:
            session.add(SAUser(name="ada", age=36, score=9.0))
            session.commit()
            row = session.execute(sa.select(SAUser).where(SAUser.name == "ada")).scalar_one()
            # ``id`` participates in dataclass ``__eq__`` (compare=True), so
            # the expected instance needs the flushed primary key.
            # (Verified: SA 2.1.8 — a bare ``SAUser(...)`` has ``id=None``
            # and does NOT equal the loaded row.)
            sa_expected = SAUser(name="ada", age=36, score=9.0)
            sa_expected.id = row.id
            assert row == sa_expected
            assert row.id == 1
    finally:
        sa_engine.dispose()

    catalyst_engine = _catalyst_engine()
    try:
        CUser, _ = _catalyst_models()
        CUser.metadata.create_all(catalyst_engine)
        with Session(catalyst_engine) as session:
            session.add(CUser(name="ada", age=36, score=9.0))
            session.commit()
            row = session.execute(select(CUser).where(CUser.name == "ada")).scalar_one()
            c_expected = CUser(name="ada", age=36, score=9.0)
            c_expected.id = row.id
            assert row == c_expected
            assert row.id == 1
    finally:
        catalyst_engine.dispose()


def test_dataclass_must_precede_declarative_base() -> None:
    """``class U(Base, MappedAsDataclass)`` is an ``ArgumentError`` — the
    mixin must come first in the bases list."""
    with pytest.raises(SAArgumentError, match="dataclass"):

        class _Wrong(SABase, MappedAsDataclass):  # type: ignore[misc]
            __tablename__ = "dc_wrong"
            id: SAMapped[int] = sa_mapped_column(primary_key=True, init=False)
            name: SAMapped[str]


def test_unexpected_kwarg_raises_type_error() -> None:
    """Generated ``__init__`` rejects unknown keywords like a dataclass."""
    with pytest.raises(TypeError):
        SAUser(name="a", bogus=1)  # type: ignore[call-arg]

    CUser, _ = _catalyst_models()
    with pytest.raises(TypeError):
        CUser(name="a", bogus=1)  # type: ignore[call-arg]
