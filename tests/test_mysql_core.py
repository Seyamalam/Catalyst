"""Live MySQL parity for compiled Core execution against SQLAlchemy (asyncmy)."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from catalyst import (
    ArgumentError,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    IntegrityError,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_async_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine

MYSQL_URL = os.environ.get("CATALYST_MYSQL_URL")
pytestmark = pytest.mark.integration


def _sa_url() -> str:
    assert MYSQL_URL is not None
    # ``sslmode=disable`` is a Catalyst-only option; asyncmy defaults to
    # plaintext, so the query string is dropped for SQLAlchemy.
    base = MYSQL_URL.split("?", 1)[0]
    return re.sub(r"^mysql(?:\+[^:]+)?://", "mysql+asyncmy://", base)


@pytest.fixture
async def mysql_pair() -> AsyncIterator[tuple[Any, Any, str]]:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    suffix = uuid.uuid4().hex[:10]
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        yield catalyst_engine, sa_engine, suffix
    finally:
        async with sa_engine.begin() as connection:
            for prefix in (
                "catalyst_core_",
                "catalyst_nullable_",
                "catalyst_sa_",
                "catalyst_key_",
            ):
                await connection.execute(sa.text(f"DROP TABLE IF EXISTS `{prefix}{suffix}`"))
        await sa_engine.dispose()
        await catalyst_engine.dispose()


def _tables(suffix: str) -> tuple[MetaData, Table, sa.Table]:
    metadata = MetaData()
    table = Table(
        f"catalyst_core_{suffix}",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(30)),
        Column("body", Text),
        Column("flag", Boolean),
        Column("score", Float),
        Column("created", DateTime),
        Column("day", Date),
        Column("payload", LargeBinary),
    )
    sa_table = sa.Table(
        f"catalyst_core_{suffix}",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(30)),
        sa.Column("body", sa.Text),
        sa.Column("flag", sa.Boolean),
        sa.Column("score", sa.Float),
        sa.Column("created", sa.DateTime),
        sa.Column("day", sa.Date),
        sa.Column("payload", sa.LargeBinary),
    )
    return metadata, table, sa_table


async def test_mysql_create_all_drop_all_and_has_table_match_sqlalchemy(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata, table, sa_table = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    async with catalyst_engine.connect() as connection:
        assert await connection.has_table(table.name) is True
        assert await connection.has_table(f"missing_{suffix}") is False
    async with sa_engine.connect() as connection:
        assert await connection.run_sync(lambda sync: sa.inspect(sync).has_table(sa_table.name))
    # checkfirst: a second create_all must be a no-op, not an error.
    await metadata.create_all(catalyst_engine)
    await metadata.drop_all(catalyst_engine)
    async with catalyst_engine.connect() as connection:
        assert await connection.has_table(table.name) is False
    async with sa_engine.connect() as connection:
        assert not await connection.run_sync(lambda sync: sa.inspect(sync).has_table(sa_table.name))


async def test_mysql_compiled_insert_select_roundtrip_types(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata, table, sa_table = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    # SA renders DateTime as DATETIME (fsp 0): MySQL truncates fractional
    # seconds, so the reference value has none.
    created = datetime(2026, 11, 9, 12, 34, 56)
    row = {
        "id": 1,
        "name": "héllo",
        "body": "line\nbreak",
        "flag": True,
        "score": 2.5,
        "created": created,
        "day": date(2026, 11, 9),
        "payload": b"\x00\xffbinary",
    }
    async with catalyst_engine.begin() as connection:
        assert (await connection.execute(insert(table).values(**row))).rowcount == 1
        assert (await connection.execute(insert(table).values(id=2))).rowcount == 1
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (await connection.execute(select(table).order_by(table.c.id))).all()
    async with sa_engine.connect() as connection:
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(sa.select(sa_table).order_by(sa_table.c.id))
            ).all()
        ]
    assert catalyst_rows == sa_rows
    # BOOL is stored as TINYINT(1): both drivers hydrate it as int 1.
    assert catalyst_rows[0] == (
        1,
        "héllo",
        "line\nbreak",
        1,
        2.5,
        created,
        date(2026, 11, 9),
        b"\x00\xffbinary",
    )
    assert catalyst_rows[1] == (2, None, None, None, None, None, None, None)


async def test_mysql_select_clauses_match_sqlalchemy(mysql_pair: tuple[Any, Any, str]) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata, table, sa_table = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    async with sa_engine.begin() as connection:
        await connection.execute(
            sa.insert(sa_table),
            [{"id": i, "name": f"row{i}", "score": float(i)} for i in range(1, 6)],
        )
    queries = [
        (
            select(table).where(table.c.score > 2.0).order_by(table.c.id.desc()).limit(2).offset(1),
            sa.select(sa_table)
            .where(sa_table.c.score > 2.0)
            .order_by(sa_table.c.id.desc())
            .limit(2)
            .offset(1),
        ),
        (
            select(table.c.id).where(table.c.id.in_([1, 3, 99])).order_by(table.c.id),
            sa.select(sa_table.c.id).where(sa_table.c.id.in_([1, 3, 99])).order_by(sa_table.c.id),
        ),
        (
            select(table.c.id).where(table.c.name.like("row%")).order_by(table.c.id.desc()),
            sa.select(sa_table.c.id)
            .where(sa_table.c.name.like("row%"))
            .order_by(sa_table.c.id.desc()),
        ),
        (
            select(table.c.id).where(table.c.id.between(2, 4)).order_by(table.c.id),
            sa.select(sa_table.c.id).where(sa_table.c.id.between(2, 4)).order_by(sa_table.c.id),
        ),
        (
            select(table.c.id).where(table.c.id.in_([])),
            sa.select(sa_table.c.id).where(sa_table.c.id.in_([])),
        ),
        (
            select(table.c.id).where(table.c.flag.is_(True)).order_by(table.c.id),
            sa.select(sa_table.c.id).where(sa_table.c.flag.is_(True)).order_by(sa_table.c.id),
        ),
        (
            select(table.c.id).where(table.c.name.is_(None)),
            sa.select(sa_table.c.id).where(sa_table.c.name.is_(None)),
        ),
        (
            select(table.c.id).order_by(table.c.id).limit(2),
            sa.select(sa_table.c.id).order_by(sa_table.c.id).limit(2),
        ),
        (
            select(table.c.id).order_by(table.c.id).offset(3),
            sa.select(sa_table.c.id).order_by(sa_table.c.id).offset(3),
        ),
    ]
    for catalyst_stmt, sa_stmt in queries:
        async with catalyst_engine.connect() as connection:
            catalyst_rows = (await connection.execute(catalyst_stmt)).all()
        async with sa_engine.connect() as connection:
            sa_rows = [tuple(item) for item in (await connection.execute(sa_stmt)).all()]
        assert catalyst_rows == sa_rows
    # Compiled statements accept mapping overrides ordered by bind name.
    compiled = select(table.c.id).where(table.c.name == "row3").compile("mysql")
    async with catalyst_engine.connect() as connection:
        overridden = (await connection.execute(compiled, {"name_1": "row4"})).all()
    assert overridden == [(4,)]


async def test_mysql_update_delete_rowcounts_match_sqlalchemy(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata, table, _ = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    sa_metadata = sa.MetaData()
    sa_table = sa.Table(
        f"catalyst_sa_{suffix}",
        sa_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(30)),
        sa.Column("flag", sa.Boolean),
    )
    async with sa_engine.begin() as connection:
        await connection.run_sync(sa_metadata.create_all)
    rows = [{"id": i, "name": f"row{i}", "flag": True} for i in range(1, 4)]
    async with catalyst_engine.begin() as connection:
        for row in rows:
            await connection.execute(insert(table).values(**row))
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table), rows)
    async with catalyst_engine.begin() as connection:
        catalyst_updated = await connection.execute(
            update(table).where(table.c.id == 1).values(name="updated", flag=False)
        )
        catalyst_deleted = await connection.execute(delete(table).where(table.c.id == 2))
    async with sa_engine.begin() as connection:
        sa_updated = await connection.execute(
            sa.update(sa_table).where(sa_table.c.id == 1).values(name="updated", flag=False)
        )
        sa_deleted = await connection.execute(sa.delete(sa_table).where(sa_table.c.id == 2))
    assert catalyst_updated.rowcount == sa_updated.rowcount == 1
    assert catalyst_deleted.rowcount == sa_deleted.rowcount == 1
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (
            await connection.execute(
                select(table.c.id, table.c.name, table.c.flag).order_by(table.c.id)
            )
        ).all()
    async with sa_engine.connect() as connection:
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.name, sa_table.c.flag).order_by(
                        sa_table.c.id
                    )
                )
            ).all()
        ]
    assert catalyst_rows == sa_rows == [(1, "updated", 0), (3, "row3", 1)]


async def test_mysql_empty_insert_and_integrity_error_parity(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata, table, sa_table = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    nullable_metadata = MetaData()
    nullable = Table(
        f"catalyst_nullable_{suffix}",
        nullable_metadata,
        Column("a", Integer),
        Column("b", String(10)),
    )
    await nullable.create(catalyst_engine)
    sa_nullable = sa.Table(
        f"catalyst_nullable_{suffix}",
        sa.MetaData(),
        sa.Column("a", sa.Integer),
        sa.Column("b", sa.String(10)),
    )
    async with catalyst_engine.begin() as connection:
        assert (await connection.execute(insert(nullable))).rowcount == 1
    async with sa_engine.begin() as connection:
        assert (await connection.execute(sa.insert(sa_nullable))).rowcount == 1
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (await connection.execute(select(nullable))).all()
    async with sa_engine.connect() as connection:
        sa_rows = [tuple(item) for item in (await connection.execute(sa.select(sa_nullable))).all()]
    assert catalyst_rows == sa_rows == [(None, None), (None, None)]
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table).values(id=1))
    with pytest.raises(IntegrityError):
        async with catalyst_engine.begin() as connection:
            await connection.execute(insert(table).values(id=1))
    with pytest.raises(sa.exc.IntegrityError):
        async with sa_engine.begin() as connection:
            await connection.execute(sa.insert(sa_table).values(id=1))


async def test_mysql_executemany_not_supported_yet(mysql_pair: tuple[Any, Any, str]) -> None:
    catalyst_engine, _, suffix = mysql_pair
    _, table, _ = _tables(suffix)
    async with catalyst_engine.connect() as connection:
        with pytest.raises(ArgumentError, match="executemany"):
            await connection.execute(insert(table), [{"id": 1}, {"id": 2}])


async def test_mysql_ddl_catalog_matches_sqlalchemy(mysql_pair: tuple[Any, Any, str]) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata, table, _ = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    sa_metadata = sa.MetaData()
    sa.Table(
        f"catalyst_sa_{suffix}",
        sa_metadata,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(30)),
        sa.Column("body", sa.Text),
        sa.Column("flag", sa.Boolean),
        sa.Column("score", sa.Float),
        sa.Column("created", sa.DateTime),
        sa.Column("day", sa.Date),
        sa.Column("payload", sa.LargeBinary),
    )
    async with sa_engine.begin() as connection:
        await connection.run_sync(sa_metadata.create_all)
    query = sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, extra "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = :name "
        "ORDER BY ordinal_position"
    )
    async with sa_engine.connect() as connection:
        catalyst_catalog = [
            tuple(item) for item in (await connection.execute(query, {"name": table.name})).all()
        ]
        sa_catalog = [
            tuple(item)
            for item in (await connection.execute(query, {"name": f"catalyst_sa_{suffix}"})).all()
        ]
    assert catalyst_catalog == sa_catalog
    assert catalyst_catalog[0][4] == "auto_increment"


async def test_mysql_reserved_identifiers_roundtrip(mysql_pair: tuple[Any, Any, str]) -> None:
    # ``key`` is a MySQL reserved word; both drivers must quote it in backticks.
    catalyst_engine, sa_engine, suffix = mysql_pair
    metadata = MetaData()
    table = Table(
        f"catalyst_key_{suffix}",
        metadata,
        Column("key", Integer, primary_key=True),
        Column("value", String(20)),
    )
    sa_table = sa.Table(
        f"catalyst_key_{suffix}",
        sa.MetaData(),
        sa.Column("key", sa.Integer, primary_key=True),
        sa.Column("value", sa.String(20)),
    )
    await metadata.create_all(catalyst_engine)
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table).values(key=1, value="one"))
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table).values(key=2, value="two"))
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (await connection.execute(select(table).order_by(table.c.key))).all()
    async with sa_engine.connect() as connection:
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(sa.select(sa_table).order_by(sa_table.c.key))
            ).all()
        ]
    assert catalyst_rows == sa_rows == [(1, "one"), (2, "two")]


async def test_mysql_compiled_expressions_match_sqlalchemy(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # Dialect renderings diverge from SQLite/PostgreSQL in documented ways:
    # concat() for string +, plain / division, LIMIT offset,count, backtick
    # quoting, and BOOL/AUTO_INCREMENT DDL.
    _, _, suffix = mysql_pair
    _, table, sa_table = _tables(suffix)
    pairs = [
        (
            select(table.c.name + "!"),
            sa.select(sa_table.c.name + "!"),
        ),
        (
            select(table.c.id / table.c.id),
            sa.select(sa_table.c.id / sa_table.c.id),
        ),
        (
            select(table).where(table.c.flag == True),  # noqa: E712
            sa.select(sa_table).where(sa_table.c.flag == True),  # noqa: E712
        ),
    ]
    from sqlalchemy.dialects import mysql as sa_mysql

    dialect = sa_mysql.pymysql.dialect()
    for catalyst_stmt, sa_stmt in pairs:
        catalyst_sql = catalyst_stmt.compile("mysql").string
        # SQLAlchemy renders %s placeholders; Catalyst renders ? for the
        # mysql_async driver. Everything else must match byte-for-byte.
        sa_sql = sa_stmt.compile(dialect=dialect).string.replace("%s", "?")
        assert catalyst_sql == sa_sql
