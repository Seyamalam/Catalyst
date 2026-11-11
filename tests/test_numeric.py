"""Numeric/Decimal parity against SQLAlchemy 2.1 across SQLite, MySQL, and PostgreSQL.

Ground truth gathered empirically against SQLAlchemy 2.1.8 + sqlite3/asyncmy/psycopg:

- DDL: every dialect renders ``NUMERIC`` — ``NUMERIC(10)``, ``NUMERIC(10, 2)``.
  MySQL accepts NUMERIC as a DECIMAL alias; unbounded ``Numeric`` becomes
  ``DECIMAL(10, 0)`` server-side (lossy!), PostgreSQL stays unbounded ``numeric``.
- SQLite stores Numeric affinity values as REAL/INTEGER (``typeof()``) and the
  result processor formats them as ``Decimal("%.<scale>f" % value)`` where scale
  is ``decimal_return_scale`` or ``scale`` or the default 10 — so reads are
  float-precision-lossy and scale-padded (``Decimal('123456789.123457')``).
- MySQL/PostgreSQL return native ``decimal.Decimal``; both round half-up to the
  column scale (``12.345`` -> ``12.35``, ``12.355`` -> ``12.36`` on NUMERIC(10,2)).
- SQLite does NOT enforce precision/scale: ``Numeric(5,2)`` happily stores
  ``1234.50`` and even ``1000.00``; ``Decimal('NaN')`` binds as float NaN which
  SQLite stores as NULL (reads back ``None``); ``Infinity`` round-trips.
- PostgreSQL numeric accepts ``NaN``/``Infinity``/``-Infinity`` on unbounded
  columns but rejects ``Infinity`` on ``NUMERIC(5,2)`` (numeric field overflow)
  and overflows ``1234.5`` into ``NUMERIC(5,2)`` with DataError.
- MySQL rejects ``Decimal('NaN')``/``Infinity`` binds: asyncmy renders them as
  bare identifiers -> error 1054 "Unknown column 'NaN' in 'field list'".
- ``text("SELECT :n")`` binds: untyped Decimal fails on SQLite (driver cannot
  bind Decimal -> ProgrammingError on both), returns ``Decimal`` on MySQL/PG.
"""

from __future__ import annotations

import decimal
import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from catalyst import (
    Column,
    DeclarativeBase,
    Integer,
    Mapped,
    MetaData,
    Session,
    Table,
    create_async_engine,
    create_engine,
    insert,
    mapped_column,
    select,
    text,
)
from sqlalchemy.dialects import mysql as sa_mysql_dialect
from sqlalchemy.dialects import postgresql as sa_pg_dialect
from sqlalchemy.dialects import sqlite as sa_sqlite_dialect
from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column

try:
    from catalyst import Numeric
except ImportError:  # pragma: no cover - exercised until Numeric lands
    Numeric = None  # type: ignore[assignment]

D = decimal.Decimal

_NUMERIC_MISSING = pytest.mark.skipif(
    Numeric is None, reason="catalyst.Numeric is not implemented yet"
)

MYSQL_URL = os.environ.get("CATALYST_MYSQL_URL")
POSTGRES_URL = os.environ.get("CATALYST_POSTGRES_URL")


def _sa_mysql_url() -> str:
    assert MYSQL_URL is not None
    # ``sslmode=disable`` is a Catalyst-only option; asyncmy defaults to
    # plaintext, so the query string is dropped for SQLAlchemy.
    base = MYSQL_URL.split("?", 1)[0]
    return re.sub(r"^mysql(?:\+[^:]+)?://", "mysql+asyncmy://", base)


def _sa_pg_url() -> str:
    assert POSTGRES_URL is not None
    return re.sub(r"^postgres(?:ql)?(?:\+[^:]+)?://", "postgresql+psycopg://", POSTGRES_URL)


def _numeric_variants() -> list[tuple[str, Any, Any]]:
    """(column name, catalyst type spec, sqlalchemy type spec) variants."""
    assert Numeric is not None
    return [
        ("n_class", Numeric, sa.Numeric),
        ("n_p10", Numeric(10), sa.Numeric(10)),
        ("n_p10s2", Numeric(10, 2), sa.Numeric(10, 2)),
        ("n_kw", Numeric(precision=10, scale=2), sa.Numeric(precision=10, scale=2)),
        (
            "n_drs",
            Numeric(10, 2, decimal_return_scale=4),
            sa.Numeric(10, 2, decimal_return_scale=4),
        ),
        ("n_no_dec", Numeric(10, 2, asdecimal=False), sa.Numeric(10, 2, asdecimal=False)),
    ]


def _catalyst_numeric_table(metadata: MetaData, name: str = "num_t") -> Table:
    assert Numeric is not None
    columns = [Column("id", Integer, primary_key=True)]
    columns += [Column(col, spec) for col, spec, _ in _numeric_variants()]
    return Table(name, metadata, *columns)


def _sa_numeric_table(metadata: sa.MetaData, name: str = "num_t") -> sa.Table:
    columns = [sa.Column("id", sa.Integer, primary_key=True)]
    columns += [sa.Column(col, spec) for col, _, spec in _numeric_variants()]
    return sa.Table(name, metadata, *columns)


# ---------------------------------------------------------------------------
# Non-live: SQLite parity (sync engines, :memory:)
# ---------------------------------------------------------------------------


@_NUMERIC_MISSING
@pytest.mark.parametrize(
    ("dialect", "sa_dialect"),
    [
        ("sqlite", sa_sqlite_dialect.dialect()),
        ("postgresql", sa_pg_dialect.dialect()),
        ("mysql", sa_mysql_dialect.pymysql.dialect()),
    ],
)
def test_numeric_ddl_matches_sqlalchemy(dialect: str, sa_dialect: Any) -> None:
    catalyst_table = _catalyst_numeric_table(MetaData())
    sa_table = _sa_numeric_table(sa.MetaData())
    assert catalyst_table.compile_ddl(dialect=dialect).string == str(
        sa.schema.CreateTable(sa_table).compile(dialect=sa_dialect)
    )


@_NUMERIC_MISSING
def test_numeric_roundtrip_matches_sqlalchemy() -> None:
    """SQLite stores Numeric affinity as REAL/INTEGER; reads come back as
    ``Decimal("%.<scale>f" % value)`` — float-lossy and scale-padded."""
    assert Numeric is not None
    catalyst_metadata = MetaData()
    catalyst_table = Table(
        "num_rt",
        catalyst_metadata,
        Column("id", Integer, primary_key=True),
        Column("u", Numeric),
        Column("p", Numeric(20, 6)),
        Column("s", Numeric(10, 2)),
    )
    sa_metadata = sa.MetaData()
    sa_table = sa.Table(
        "num_rt",
        sa_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("u", sa.Numeric),
        sa.Column("p", sa.Numeric(20, 6)),
        sa.Column("s", sa.Numeric(10, 2)),
    )
    values = [
        D("12.34"),
        D("0.01"),
        D("-7.5"),
        D("123456789.123456789"),
        D("1E+3"),
        D("0.000001"),
    ]
    # Verified ground truth on sqlite3: REAL storage loses precision, then the
    # result processor pads/truncates to the column's effective return scale
    # (scale or 10 when unset).
    expected_p = [
        D("12.340000"),
        D("0.010000"),
        D("-7.500000"),
        D("123456789.123457"),
        D("1000.000000"),
        D("0.000001"),
    ]
    expected_u = [
        D("12.3400000000"),
        D("0.0100000000"),
        D("-7.5000000000"),
        D("123456789.1234567910"),
        D("1000.0000000000"),
        D("0.0000010000"),
    ]
    expected_s = [D(v).quantize(D("0.01")) for v in expected_p]
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            for i, value in enumerate(values, start=1):
                connection.execute(insert(catalyst_table).values(id=i, u=value, p=value, s=value))
            catalyst_rows = connection.execute(
                select(catalyst_table.c.u, catalyst_table.c.p, catalyst_table.c.s).order_by(
                    catalyst_table.c.id
                )
            ).all()
        with sqlalchemy.begin() as connection:
            connection.execute(
                sa.insert(sa_table),
                [{"id": i, "u": v, "p": v, "s": v} for i, v in enumerate(values, start=1)],
            )
            sa_rows = connection.execute(
                sa.select(sa_table.c.u, sa_table.c.p, sa_table.c.s).order_by(sa_table.c.id)
            ).all()
        assert catalyst_rows == sa_rows
        for row, eu, ep, es in zip(catalyst_rows, expected_u, expected_p, expected_s, strict=True):
            assert row == (eu, ep, es)
            assert all(type(item) is D for item in row)
        # NUMERIC affinity stores REAL/INTEGER, not TEXT.
        with catalyst.connect() as connection:
            stored = connection.execute(
                text("SELECT typeof(u), typeof(p) FROM num_rt WHERE id = 1")
            ).one()
        assert stored == ("real", "real")
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (D("12.345"), D("12.35")),
        (D("12.344"), D("12.34")),
        (D("12.355"), D("12.36")),
        (D("-12.345"), D("-12.35")),
        (12.345, D("12.35")),
        (12.355, D("12.36")),
        ("12.345", D("12.35")),
        (7, D("7.00")),
    ],
    ids=[
        "decimal-half-up",
        "decimal-down",
        "decimal-half-up-355",
        "decimal-negative",
        "float-345",
        "float-355",
        "str-345",
        "int",
    ],
)
def test_numeric_scale_rounding_matches_sqlalchemy(value: object, expected: D) -> None:
    """Numeric(10,2) rounds the stored REAL via ``%.2f`` formatting: half-up on
    the double's shortest repr — identical outcomes to SQLAlchemy on SQLite."""
    assert Numeric is not None
    catalyst_table = Table(
        "num_scale",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("p", Numeric(10, 2)),
    )
    sa_table = sa.Table(
        "num_scale",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("p", sa.Numeric(10, 2)),
    )
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            connection.execute(insert(catalyst_table).values(id=1, p=value))
            actual = connection.execute(select(catalyst_table.c.p)).scalar()
        with sqlalchemy.begin() as connection:
            connection.execute(sa.insert(sa_table).values(id=1, p=value))
            expected_row = connection.execute(sa.select(sa_table.c.p)).scalar()
        assert actual == expected_row == expected
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
def test_numeric_precision_and_scale_not_enforced_on_sqlite() -> None:
    """SQLite NUMERIC affinity does not enforce NUMERIC(5,2): out-of-range and
    over-scale values store and read back anyway (quirk shared with SA)."""
    assert Numeric is not None
    catalyst_table = Table(
        "num_overflow",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("p", Numeric(5, 2)),
    )
    sa_table = sa.Table(
        "num_overflow",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("p", sa.Numeric(5, 2)),
    )
    cases = [(D("1234.5"), D("1234.50")), (D("999.99"), D("999.99")), (D("999.995"), D("1000.00"))]
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            for i, (value, _) in enumerate(cases, start=1):
                connection.execute(insert(catalyst_table).values(id=i, p=value))
            actual = connection.execute(
                select(catalyst_table.c.p).order_by(catalyst_table.c.id)
            ).all()
        with sqlalchemy.begin() as connection:
            connection.execute(
                sa.insert(sa_table), [{"id": i, "p": v} for i, (v, _) in enumerate(cases, start=1)]
            )
            expected = connection.execute(sa.select(sa_table.c.p).order_by(sa_table.c.id)).all()
        assert actual == expected == [(e,) for _, e in cases]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
def test_numeric_null_nan_and_infinity_on_sqlite() -> None:
    """NULL round-trips. ``Decimal('NaN')`` binds via ``float('nan')`` which
    SQLite stores as NULL — reads come back ``None`` on BOTH engines.
    ``Infinity`` binds as REAL Inf and reads back ``Decimal('Infinity')``."""
    assert Numeric is not None
    catalyst_table = Table(
        "num_special",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("p", Numeric(20, 6)),
    )
    sa_table = sa.Table(
        "num_special",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("p", sa.Numeric(20, 6)),
    )
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        cases = [(1, None), (2, D("NaN")), (3, D("Infinity")), (4, D("-Infinity"))]
        with catalyst.begin() as connection:
            for row_id, value in cases:
                connection.execute(insert(catalyst_table).values(id=row_id, p=value))
            actual = connection.execute(
                select(catalyst_table.c.p).order_by(catalyst_table.c.id)
            ).all()
        with sqlalchemy.begin() as connection:
            connection.execute(sa.insert(sa_table), [{"id": i, "p": v} for i, v in cases])
            expected = connection.execute(sa.select(sa_table.c.p).order_by(sa_table.c.id)).all()
        assert actual == expected
        # Quirk: NaN is stored as NULL, not as a NaN value.
        assert actual == [(None,), (None,), (D("Infinity"),), (D("-Infinity"),)]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
def test_numeric_asdecimal_and_return_scale_on_sqlite() -> None:
    """``asdecimal=False`` returns raw floats; ``decimal_return_scale`` pads the
    Decimal result beyond the column scale."""
    assert Numeric is not None
    catalyst_table = Table(
        "num_flags",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("a", Numeric(10, 2, asdecimal=False)),
        Column("b", Numeric(10, 2, decimal_return_scale=4)),
    )
    sa_table = sa.Table(
        "num_flags",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("a", sa.Numeric(10, 2, asdecimal=False)),
        sa.Column("b", sa.Numeric(10, 2, decimal_return_scale=4)),
    )
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            connection.execute(insert(catalyst_table).values(id=1, a=D("12.345"), b=D("12.345")))
            actual = connection.execute(select(catalyst_table.c.a, catalyst_table.c.b)).one()
        with sqlalchemy.begin() as connection:
            connection.execute(sa.insert(sa_table).values(id=1, a=D("12.345"), b=D("12.345")))
            expected = connection.execute(sa.select(sa_table.c.a, sa_table.c.b)).one()
        assert actual == expected == (12.345, D("12.3450"))
        assert type(actual[0]) is float
        assert type(actual[1]) is D
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
def test_numeric_text_decimal_bind_raises_programming_error_on_sqlite() -> None:
    """sqlite3 cannot bind ``decimal.Decimal``; both engines surface
    ProgrammingError for an untyped ``text()``/raw bind."""
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        with catalyst.connect() as connection:
            with pytest.raises(Exception, match="Decimal") as catalyst_error:
                connection.execute(text("SELECT :n"), {"n": D("3.14")})
        with sqlalchemy.connect() as connection:
            with pytest.raises(sa.exc.ProgrammingError) as sa_error:
                connection.execute(sa.text("SELECT :n"), {"n": D("3.14")})
        assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
def test_numeric_executemany_matches_sqlalchemy() -> None:
    assert Numeric is not None
    catalyst_table = Table(
        "num_em",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("p", Numeric(20, 6)),
        Column("u", Numeric),
    )
    sa_table = sa.Table(
        "num_em",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("p", sa.Numeric(20, 6)),
        sa.Column("u", sa.Numeric),
    )
    rows = [{"id": i, "p": D(f"{i}.5"), "u": D(f"{i}.5")} for i in range(3)]
    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            catalyst_result = connection.execute(insert(catalyst_table), rows)
            catalyst_rows = connection.execute(
                select(catalyst_table.c.p, catalyst_table.c.u).order_by(catalyst_table.c.id)
            ).all()
        with sqlalchemy.begin() as connection:
            sa_result = connection.execute(sa.insert(sa_table), rows)
            sa_rows = connection.execute(
                sa.select(sa_table.c.p, sa_table.c.u).order_by(sa_table.c.id)
            ).all()
        assert catalyst_result.rowcount == sa_result.rowcount == 3
        assert catalyst_rows == sa_rows
        assert catalyst_rows == [
            (D("0.500000"), D("0.5000000000")),
            (D("1.500000"), D("1.5000000000")),
            (D("2.500000"), D("2.5000000000")),
        ]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@_NUMERIC_MISSING
def test_mapped_decimal_infers_numeric_and_hydrates() -> None:
    """``Mapped[decimal.Decimal]`` infers ``NUMERIC`` (nullable for ``| None``),
    ``mapped_column(Numeric(10, 2))`` keeps precision/scale, and ORM hydration
    yields ``Decimal`` values identical to SQLAlchemy's."""
    assert Numeric is not None

    class Base(DeclarativeBase):
        pass

    class Item(Base):
        __tablename__ = "num_items"
        id: Mapped[int] = mapped_column(primary_key=True)
        amount: Mapped[D]
        price: Mapped[D | None] = mapped_column(Numeric(10, 2))
        qty: Mapped[D] = mapped_column(Numeric)

    class SABase(SADeclarativeBase):
        pass

    class SAItem(SABase):
        __tablename__ = "num_items"
        id: SAMapped[int] = sa_mapped_column(primary_key=True)
        amount: SAMapped[D]
        price: SAMapped[D | None] = sa_mapped_column(sa.Numeric(10, 2))
        qty: SAMapped[D] = sa_mapped_column(sa.Numeric)

    # Same DDL and nullability as SQLAlchemy's inferred mapping.
    assert Item.__table__.compile_ddl().string == str(
        sa.schema.CreateTable(SAItem.__table__).compile(dialect=sa_sqlite_dialect.dialect())
    )
    for col, sa_col in zip(Item.__table__.columns, SAItem.__table__.columns, strict=True):
        assert col.nullable == sa_col.nullable

    catalyst = create_engine(":memory:")
    sqlalchemy = sa.create_engine("sqlite:///:memory:")
    try:
        Item.metadata.create_all(catalyst)
        SABase.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            connection.execute(
                insert(Item.__table__).values(id=1, amount=D("12.34"), price=D("5.5"), qty=D("3"))
            )
        with SASession(sqlalchemy) as sa_session:
            sa_session.add(SAItem(id=1, amount=D("12.34"), price=D("5.5"), qty=D("3")))
            sa_session.commit()
        with Session(catalyst) as session:
            item = session.scalars(select(Item)).one()
            assert item.amount == D("12.3400000000")
            assert item.price == D("5.50")
            assert item.qty == D("3.0000000000")
            assert all(type(v) is D for v in (item.amount, item.price, item.qty))
        with SASession(sqlalchemy) as sa_session:
            sa_item = sa_session.scalars(sa.select(SAItem)).one()
            assert (item.amount, item.price, item.qty) == (
                sa_item.amount,
                sa_item.price,
                sa_item.qty,
            )
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


# ---------------------------------------------------------------------------
# Live: MySQL (asyncmy) parity
# ---------------------------------------------------------------------------


def _live_tables(suffix: str) -> tuple[MetaData, Table, sa.Table]:
    """One wide table per engine: unbounded + (20,6) + (10,2) + overflow (5,2).

    The Catalyst and SQLAlchemy tables get distinct names so each engine
    inserts into its own physical table — identical explicit primary keys
    would otherwise collide on the second engine's insert.
    """
    assert Numeric is not None
    metadata = MetaData()
    table = Table(
        f"gt_num_cat_{suffix}",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("n", Numeric),
        Column("p", Numeric(20, 6)),
        Column("s", Numeric(10, 2)),
        Column("o", Numeric(5, 2)),
    )
    sa_table = sa.Table(
        f"gt_num_sa_{suffix}",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("n", sa.Numeric),
        sa.Column("p", sa.Numeric(20, 6)),
        sa.Column("s", sa.Numeric(10, 2)),
        sa.Column("o", sa.Numeric(5, 2)),
    )
    return metadata, table, sa_table


@pytest.fixture
async def mysql_pair() -> AsyncIterator[tuple[Any, Any, str]]:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    suffix = uuid.uuid4().hex[:10]
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_mysql_url())
    try:
        yield catalyst_engine, sa_engine, suffix
    finally:
        async with sa_engine.begin() as connection:
            names = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = DATABASE() AND table_name LIKE :suffix"
                        ),
                        {"suffix": f"gt_num_%{suffix}"},
                    )
                )
                .scalars()
                .all()
            )
            for name in names:
                await connection.execute(sa.text(f"DROP TABLE IF EXISTS `{name}`"))
        await sa_engine.dispose()
        await catalyst_engine.dispose()


async def _mysql_create(
    catalyst_engine: Any, sa_engine: Any, suffix: str
) -> tuple[MetaData, Table, sa.Table]:
    metadata, table, sa_table = _live_tables(suffix)
    await metadata.create_all(catalyst_engine)
    async with sa_engine.begin() as connection:
        await connection.run_sync(sa_table.metadata.create_all)
    return metadata, table, sa_table


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_mysql_numeric_ddl_and_catalog(mysql_pair: tuple[Any, Any, str]) -> None:
    """DDL renders ``NUMERIC(...)``; MySQL stores it as DECIMAL. Unbounded
    ``Numeric`` becomes ``DECIMAL(10, 0)`` — a lossy server-side default."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _mysql_create(catalyst_engine, sa_engine, suffix)
    assert table.compile_ddl(dialect="mysql").string == str(
        sa.schema.CreateTable(sa_table).compile(dialect=sa_mysql_dialect.pymysql.dialect())
    ).replace(sa_table.name, table.name)
    query = sa.text(
        "SELECT column_name, data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :name "
        "ORDER BY ordinal_position"
    )
    async with sa_engine.connect() as connection:
        rows = [
            tuple(item) for item in (await connection.execute(query, {"name": table.name})).all()
        ]
    # The catalog reflects MySQL's view of the Catalyst-created table.
    assert rows == [
        ("id", "int", 10, 0),
        ("n", "decimal", 10, 0),
        ("p", "decimal", 20, 6),
        ("s", "decimal", 10, 2),
        ("o", "decimal", 5, 2),
    ]


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_mysql_numeric_roundtrip_and_scale_rounding(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    """DECIMAL(20,6) returns padded Decimals; DECIMAL(10,2) rounds half-up
    (12.345->12.35, 12.355->12.36, -12.345->-12.35); float/int/str binds all
    coerce through the column type."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _mysql_create(catalyst_engine, sa_engine, suffix)
    rows = [
        {"id": 1, "p": D("12.34"), "s": D("12.34")},
        {"id": 2, "p": D("0.01"), "s": D("-7.5")},
        {"id": 3, "p": D("123456789.123456789"), "s": D("1E+3")},
        {"id": 4, "p": 12.345, "s": 12.345},
        {"id": 5, "p": 7, "s": "12.355"},
    ]
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table), rows)
        catalyst_rows = (
            await connection.execute(select(table.c.p, table.c.s).order_by(table.c.id))
        ).all()
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table), rows)
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.p, sa_table.c.s).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_rows == sa_rows
    assert catalyst_rows == [
        (D("12.340000"), D("12.34")),
        (D("0.010000"), D("-7.50")),
        (D("123456789.123457"), D("1000.00")),
        (D("12.345000"), D("12.35")),  # float 12.345 rounds half-up at scale 2
        (D("7.000000"), D("12.36")),  # int -> Decimal; str '12.355' rounds to 12.36
    ]
    assert all(type(item) is D for row in catalyst_rows for item in row)


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_mysql_numeric_unbounded_is_decimal_10_0(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    """Quirk: ``Numeric`` without precision compiles to ``NUMERIC`` which MySQL
    stores as ``DECIMAL(10,0)`` — fractional values are rounded away on BOTH
    engines identically. Asserted as parity, not as ideal behavior."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _mysql_create(catalyst_engine, sa_engine, suffix)
    rows = [{"id": i, "n": v} for i, v in enumerate([D("12.34"), D("0.1"), 12.7, 7], start=1)]
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table), rows)
        catalyst_rows = (await connection.execute(select(table.c.n).order_by(table.c.id))).all()
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table), rows)
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(sa.select(sa_table.c.n).order_by(sa_table.c.id))
            ).all()
        ]
    assert catalyst_rows == sa_rows
    # DECIMAL(10,0) rounds half away from zero: 12.34->12, 0.1->0, 12.7->13.
    assert catalyst_rows == [(D("12"),), (D("0"),), (D("13"),), (D("7"),)]


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_mysql_numeric_overflow_and_null(mysql_pair: tuple[Any, Any, str]) -> None:
    """NUMERIC(5,2) rejects ``1234.5`` (out of range, errno 1264) and ``999.995``
    (rounds to 1000.00); ``999.99`` fits. NULL round-trips."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _mysql_create(catalyst_engine, sa_engine, suffix)
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table).values(id=1, o=D("999.99"), p=None))
        assert (await connection.execute(select(table.c.o, table.c.p))).one() == (
            D("999.99"),
            None,
        )
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table).values(id=1, o=D("999.99"), p=None))
        sa_row = (await connection.execute(sa.select(sa_table.c.o, sa_table.c.p))).one()
        assert tuple(sa_row) == (D("999.99"), None)
    for bad in (D("1234.5"), D("999.995")):
        async with catalyst_engine.connect() as connection:
            with pytest.raises(Exception) as catalyst_error:
                await connection.execute(insert(table).values(id=2, o=bad))
            await connection.rollback()
        async with sa_engine.connect() as connection:
            with pytest.raises(sa.exc.DatabaseError) as sa_error:
                await connection.execute(sa.insert(sa_table).values(id=2, o=bad))
        assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
        assert "1264" in str(catalyst_error.value) or "Out of range" in str(catalyst_error.value)


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_mysql_numeric_text_binds_and_special_values(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    """``text()`` Decimal binds return ``Decimal``. ``NaN``/``Infinity`` cannot
    be represented: asyncmy renders them as bare identifiers -> error 1054 on
    both engines."""
    assert Numeric is not None
    catalyst_engine, sa_engine, _ = mysql_pair
    # ``text()`` needs no table; NaN/Infinity fail inside the driver.
    async with catalyst_engine.connect() as connection:
        row = (await connection.execute(text("SELECT :n AS v"), {"n": D("3.14")})).one()
        assert row == (D("3.14"),)
        assert type(row[0]) is D
        catalyst_errors = []
        for bind in (D("NaN"), D("Infinity"), D("-Infinity")):
            with pytest.raises(Exception) as catalyst_error:
                await connection.execute(text("SELECT :n AS v"), {"n": bind})
            await connection.rollback()
            catalyst_errors.append(type(catalyst_error.value).__name__)
    async with sa_engine.connect() as connection:
        sa_row = (await connection.execute(sa.text("SELECT :n AS v"), {"n": D("3.14")})).one()
        assert tuple(sa_row) == (D("3.14"),)
        sa_errors = []
        for bind in (D("NaN"), D("Infinity"), D("-Infinity")):
            with pytest.raises(sa.exc.OperationalError) as sa_error:
                await connection.execute(sa.text("SELECT :n AS v"), {"n": bind})
            await connection.rollback()
            sa_errors.append(type(sa_error.value).__name__)
    # asyncmy renders non-finite Decimals as bare identifiers -> error 1054
    # "Unknown column 'NaN' in 'field list'" -> OperationalError on both.
    assert catalyst_errors == sa_errors == ["OperationalError"] * 3


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_mysql_numeric_executemany(mysql_pair: tuple[Any, Any, str]) -> None:
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _mysql_create(catalyst_engine, sa_engine, suffix)
    # ``id`` starts at 1: inserting 0 into the AUTO_INCREMENT primary key
    # auto-assigns 1 and collides with the explicit id=1 — verified to fail
    # under pure SQLAlchemy+asyncmy as well.
    rows = [{"id": i, "p": D(f"{i}.5"), "s": D(f"{i}.25")} for i in range(1, 4)]
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(insert(table), rows)
        catalyst_rows = (
            await connection.execute(select(table.c.p, table.c.s).order_by(table.c.id))
        ).all()
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(sa.insert(sa_table), rows)
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.p, sa_table.c.s).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_result.rowcount == sa_result.rowcount == 3
    assert catalyst_rows == sa_rows
    assert catalyst_rows == [
        (D("1.500000"), D("1.25")),
        (D("2.500000"), D("2.25")),
        (D("3.500000"), D("3.25")),
    ]


# ---------------------------------------------------------------------------
# Live: PostgreSQL (psycopg) parity
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_pair() -> AsyncIterator[tuple[Any, Any, str]]:
    if not POSTGRES_URL:
        pytest.skip("CATALYST_POSTGRES_URL is not set")
    suffix = uuid.uuid4().hex[:10]
    catalyst_engine = create_async_engine(POSTGRES_URL)
    sa_engine = sa_create_async_engine(_sa_pg_url())
    try:
        yield catalyst_engine, sa_engine, suffix
    finally:
        async with sa_engine.begin() as connection:
            await connection.execute(sa.text(f'DROP TABLE IF EXISTS "gt_num_cat_{suffix}"'))
            await connection.execute(sa.text(f'DROP TABLE IF EXISTS "gt_num_sa_{suffix}"'))
        await sa_engine.dispose()
        await catalyst_engine.dispose()


async def _pg_create(
    catalyst_engine: Any, sa_engine: Any, suffix: str
) -> tuple[MetaData, Table, sa.Table]:
    metadata, table, sa_table = _live_tables(suffix)
    await metadata.create_all(catalyst_engine)
    async with sa_engine.begin() as connection:
        await connection.run_sync(sa_table.metadata.create_all)
    return metadata, table, sa_table


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_ddl_and_catalog(pg_pair: tuple[Any, Any, str]) -> None:
    """DDL renders ``NUMERIC``/``NUMERIC(p, s)``; PostgreSQL stores unbounded
    ``Numeric`` as ``numeric`` with NULL precision/scale (no DECIMAL(10,0)
    truncation like MySQL)."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = pg_pair
    _, table, sa_table = await _pg_create(catalyst_engine, sa_engine, suffix)
    assert table.compile_ddl(dialect="postgresql").string == str(
        sa.schema.CreateTable(sa_table).compile(dialect=sa_pg_dialect.dialect())
    ).replace(sa_table.name, table.name)
    query = sa.text(
        "SELECT column_name, data_type, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_name = :name "
        "ORDER BY ordinal_position"
    )
    async with sa_engine.connect() as connection:
        rows = [
            tuple(item) for item in (await connection.execute(query, {"name": table.name})).all()
        ]
    assert rows == [
        ("id", "integer", 32, 0),
        ("n", "numeric", None, None),
        ("p", "numeric", 20, 6),
        ("s", "numeric", 10, 2),
        ("o", "numeric", 5, 2),
    ]


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_roundtrip(pg_pair: tuple[Any, Any, str]) -> None:
    """Unbounded numeric keeps full precision; DECIMAL-equivalent scale pads;
    float binds coerce through numeric (0.1 -> Decimal('0.1'), not float noise)."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = pg_pair
    _, table, sa_table = await _pg_create(catalyst_engine, sa_engine, suffix)
    rows = [
        {"id": 1, "n": D("12.34"), "p": D("12.34")},
        {"id": 2, "n": D("0.01"), "p": D("-7.5")},
        {"id": 3, "n": D("123456789.123456789"), "p": D("123456789.123456789")},
        {"id": 4, "n": 0.1, "p": 12.345},
        {"id": 5, "n": 7, "p": "12.355"},
        {"id": 6, "n": None, "p": None},
    ]
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table), rows)
        catalyst_rows = (
            await connection.execute(select(table.c.n, table.c.p).order_by(table.c.id))
        ).all()
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table), rows)
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.n, sa_table.c.p).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_rows == sa_rows
    assert catalyst_rows == [
        (D("12.34"), D("12.340000")),
        (D("0.01"), D("-7.500000")),
        (D("123456789.123456789"), D("123456789.123457")),
        (D("0.1"), D("12.345000")),
        (D("7"), D("12.355000")),
        (None, None),
    ]
    assert all(type(item) is D or item is None for row in catalyst_rows for item in row)


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_scale_rounding(pg_pair: tuple[Any, Any, str]) -> None:
    """NUMERIC(10,2) rounds half away from zero on the decimal value:
    12.345->12.35, 12.355->12.36, -12.345->-12.35 — identical for Decimal,
    float, and str binds."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = pg_pair
    _, table, sa_table = await _pg_create(catalyst_engine, sa_engine, suffix)
    cases = [
        (D("12.345"), D("12.35")),
        (D("12.344"), D("12.34")),
        (D("12.355"), D("12.36")),
        (D("-12.345"), D("-12.35")),
        (12.345, D("12.35")),
        (12.355, D("12.36")),
        ("12.345", D("12.35")),
    ]
    async with catalyst_engine.begin() as connection:
        for i, (value, _) in enumerate(cases, start=1):
            await connection.execute(insert(table).values(id=i, s=value))
        actual = (await connection.execute(select(table.c.s).order_by(table.c.id))).all()
    async with sa_engine.begin() as connection:
        await connection.execute(
            sa.insert(sa_table), [{"id": i, "s": v} for i, (v, _) in enumerate(cases, start=1)]
        )
        expected = [
            tuple(item)
            for item in (
                await connection.execute(sa.select(sa_table.c.s).order_by(sa_table.c.id))
            ).all()
        ]
    assert actual == expected == [(e,) for _, e in cases]


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_special_values(pg_pair: tuple[Any, Any, str]) -> None:
    """PostgreSQL numeric natively supports NaN/Infinity/-Infinity — including
    on scaled columns (``NUMERIC(10,2)`` accepts NaN; ``Infinity`` on
    ``NUMERIC(5,2)`` overflows)."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = pg_pair
    _, table, sa_table = await _pg_create(catalyst_engine, sa_engine, suffix)
    cases = [
        (1, "n", D("NaN")),
        (2, "n", D("Infinity")),
        (3, "n", D("-Infinity")),
        (4, "s", D("NaN")),
        (5, "n", D("123456789.123456789123456789")),
    ]
    async with catalyst_engine.begin() as connection:
        for row_id, col, value in cases:
            await connection.execute(insert(table).values(id=row_id, **{col: value}))
        actual = (await connection.execute(select(table.c.n, table.c.s).order_by(table.c.id))).all()
    async with sa_engine.begin() as connection:
        for row_id, col, value in cases:
            await connection.execute(sa.insert(sa_table).values(id=row_id, **{col: value}))
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.n, sa_table.c.s).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert len(actual) == len(sa_rows) == len(cases)
    for actual_row, sa_row in zip(actual, sa_rows, strict=True):
        for a, s_ in zip(actual_row, sa_row, strict=True):
            if a is None or s_ is None:
                assert a == s_
            elif a.is_nan() and s_.is_nan():
                continue
            else:
                assert a == s_
    # Exact expected values (Decimal NaN never equals itself).
    assert str(actual[0][0]) == "NaN" and actual[0][1] is None
    assert actual[1][0] == D("Infinity")
    assert actual[2][0] == D("-Infinity")
    assert str(actual[3][1]) == "NaN"
    assert actual[4][0] == D("123456789.123456789123456789")


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_overflow_errors(pg_pair: tuple[Any, Any, str]) -> None:
    """NUMERIC(5,2) rejects 1234.5 and 999.995 (rounds to 1000.00 -> overflow)
    and Infinity; 999.99 fits."""
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = pg_pair
    _, table, sa_table = await _pg_create(catalyst_engine, sa_engine, suffix)
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table).values(id=1, o=D("999.99")))
        assert (await connection.execute(select(table.c.o))).scalar() == D("999.99")
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table).values(id=1, o=D("999.99")))
        assert (await connection.execute(sa.select(sa_table.c.o))).scalar() == D("999.99")
    for bad in (D("1234.5"), D("999.995"), D("Infinity")):
        async with catalyst_engine.connect() as connection:
            with pytest.raises(Exception) as catalyst_error:
                await connection.execute(insert(table).values(id=2, o=bad))
            await connection.rollback()
        async with sa_engine.connect() as connection:
            with pytest.raises(sa.exc.DataError) as sa_error:
                await connection.execute(sa.insert(sa_table).values(id=2, o=bad))
        assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
        assert "numeric field overflow" in str(catalyst_error.value)


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_text_binds(pg_pair: tuple[Any, Any, str]) -> None:
    """``text()`` binds: Decimal -> Decimal, NaN -> Decimal('NaN'),
    Infinity -> Decimal('Infinity') (psycopg adapts Decimal to numeric)."""
    assert Numeric is not None
    catalyst_engine, sa_engine, _ = pg_pair
    # ``text()`` needs no table; psycopg adapts Decimal binds to numeric.
    binds = [D("3.14"), D("NaN"), D("Infinity"), D("-Infinity"), 12.345, 7]
    async with catalyst_engine.connect() as connection:
        actual = []
        for bind in binds:
            row = (await connection.execute(text("SELECT :n AS v"), {"n": bind})).one()
            actual.append(row[0])
    async with sa_engine.connect() as connection:
        expected = []
        for bind in binds:
            row = (await connection.execute(sa.text("SELECT :n AS v"), {"n": bind})).one()
            expected.append(row[0])
    assert len(actual) == len(expected)
    for a, s_ in zip(actual, expected, strict=True):
        if isinstance(a, D) and a.is_nan() and isinstance(s_, D) and s_.is_nan():
            continue
        assert a == s_
        assert type(a) is type(s_)
    assert actual[0] == D("3.14") and type(actual[0]) is D
    assert str(actual[1]) == "NaN"
    assert actual[2] == D("Infinity")
    assert actual[3] == D("-Infinity")
    assert actual[4] == 12.345 and type(actual[4]) is float
    assert actual[5] == 7 and type(actual[5]) is int


@_NUMERIC_MISSING
@pytest.mark.integration
async def test_pg_numeric_executemany(pg_pair: tuple[Any, Any, str]) -> None:
    assert Numeric is not None
    catalyst_engine, sa_engine, suffix = pg_pair
    _, table, sa_table = await _pg_create(catalyst_engine, sa_engine, suffix)
    rows = [{"id": i, "n": D(f"{i}.5"), "s": D(f"{i}.25")} for i in range(3)]
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(insert(table), rows)
        catalyst_rows = (
            await connection.execute(select(table.c.n, table.c.s).order_by(table.c.id))
        ).all()
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(sa.insert(sa_table), rows)
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.n, sa_table.c.s).order_by(sa_table.c.id)
                )
            ).all()
        ]
    # psycopg reports rowcount -1 for executemany (driver limitation,
    # verified); Catalyst reports the true aggregate.
    assert catalyst_result.rowcount == 3
    assert sa_result.rowcount == -1
    assert catalyst_rows == sa_rows
    assert catalyst_rows == [
        (D("0.5"), D("0.25")),
        (D("1.5"), D("1.25")),
        (D("2.5"), D("2.25")),
    ]
