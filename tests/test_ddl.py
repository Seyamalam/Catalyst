"""SQLite DDL compilation and execution parity against SQLAlchemy 2.1."""

from __future__ import annotations

from typing import Any

import pytest
from catalyst import (
    ArgumentError,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy import (
    Boolean as SABoolean,
)
from sqlalchemy import (
    Column as SAColumn,
)
from sqlalchemy import (
    DateTime as SADateTime,
)
from sqlalchemy import (
    Float as SAFloat,
)
from sqlalchemy import (
    Integer as SAInteger,
)
from sqlalchemy import (
    MetaData as SAMetaData,
)
from sqlalchemy import (
    String as SAString,
)
from sqlalchemy import (
    Table as SATable,
)
from sqlalchemy import (
    Text as SAText,
)
from sqlalchemy import (
    create_engine as sa_create_engine,
)
from sqlalchemy import (
    insert as sa_insert,
)
from sqlalchemy import (
    select as sa_select,
)
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import (
    CreateTable as SACreateTable,
)
from sqlalchemy.schema import (
    DropTable as SADropTable,
)

TypePair = tuple[object, object]

_TYPE_PAIRS: list[tuple[str, TypePair]] = [
    ("integer", (Integer, SAInteger)),
    ("string-30", (String(30), SAString(30))),
    ("text", (Text, SAText)),
    ("boolean", (Boolean, SABoolean)),
    ("float", (Float, SAFloat)),
    ("datetime", (DateTime, SADateTime)),
]


def _paired_table(
    name: str, columns: list[tuple[str, TypePair, dict[str, Any]]]
) -> tuple[Table, SATable]:
    """Build matching Catalyst and SQLAlchemy tables from one spec."""
    catalyst_metadata = MetaData()
    sqlalchemy_metadata = SAMetaData()
    catalyst_columns = [Column(cname, ctype, **opts) for cname, (ctype, _stype), opts in columns]
    sqlalchemy_columns = [
        SAColumn(cname, stype, **opts) for cname, (_ctype, stype), opts in columns
    ]
    return (
        Table(name, catalyst_metadata, *catalyst_columns),
        SATable(name, sqlalchemy_metadata, *sqlalchemy_columns),
    )


def _assert_create_parity(table: Table, sa_table: SATable) -> None:
    compiled = table.compile_ddl()
    sa_compiled = SACreateTable(sa_table).compile(dialect=sqlite.dialect())
    assert compiled.string == str(sa_compiled)
    assert compiled.params == {}


def _assert_drop_parity(table: Table, sa_table: SATable) -> None:
    compiled = table.compile_drop()
    sa_compiled = SADropTable(sa_table).compile(dialect=sqlite.dialect())
    assert compiled.string == str(sa_compiled)
    assert compiled.params == {}


def test_simple_table_create_matches_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "users",
        [
            ("id", (Integer, SAInteger), {"primary_key": True}),
            ("name", (String(30), SAString(30)), {}),
        ],
    )
    _assert_create_parity(table, sa_table)


@pytest.mark.parametrize(("label", "pair"), _TYPE_PAIRS, ids=[label for label, _ in _TYPE_PAIRS])
def test_type_rendering_matches_sqlalchemy(label: str, pair: TypePair) -> None:
    table, sa_table = _paired_table("typed", [("value", pair, {})])
    _assert_create_parity(table, sa_table)


def test_string_without_length_matches_sqlalchemy() -> None:
    table, sa_table = _paired_table("bare", [("value", (String(), SAString()), {})])
    _assert_create_parity(table, sa_table)


def test_nullability_variants_match_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "nulls",
        [
            ("plain", (Integer, SAInteger), {}),
            ("required", (Integer, SAInteger), {"nullable": False}),
            ("key", (Integer, SAInteger), {"primary_key": True}),
            # An explicit nullable=True overrides the primary-key NOT NULL.
            ("optional_key", (Integer, SAInteger), {"primary_key": True, "nullable": True}),
        ],
    )
    _assert_create_parity(table, sa_table)


def test_server_defaults_match_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "defaults",
        [
            ("count", (Integer, SAInteger), {"server_default": "7"}),
            ("label", (String(10), SAString(10)), {"server_default": "o'clock"}),
            ("blank", (String(10), SAString(10)), {"server_default": None}),
            ("required", (Integer, SAInteger), {"nullable": False, "server_default": "1"}),
        ],
    )
    _assert_create_parity(table, sa_table)


def test_client_side_default_is_not_ddl() -> None:
    """Client-side ``default=`` values never appear in DDL, like SQLAlchemy."""
    table, sa_table = _paired_table(
        "clientdefaults",
        [
            ("n", (Integer, SAInteger), {"default": 5}),
            ("s", (String(10), SAString(10)), {"default": "x"}),
        ],
    )
    _assert_create_parity(table, sa_table)


def test_composite_primary_key_matches_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "composite",
        [
            ("a", (Integer, SAInteger), {"primary_key": True}),
            ("b", (String(10), SAString(10)), {"primary_key": True}),
            ("c", (Integer, SAInteger), {}),
        ],
    )
    _assert_create_parity(table, sa_table)


def test_unique_constraints_match_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "uniques",
        [
            ("a", (Integer, SAInteger), {"unique": True}),
            ("b", (Integer, SAInteger), {"unique": True}),
            ("pk", (Integer, SAInteger), {"primary_key": True}),
            ("both", (Integer, SAInteger), {"primary_key": True, "unique": True}),
        ],
    )
    _assert_create_parity(table, sa_table)


def test_identifier_quoting_in_ddl_matches_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "order",
        [
            ("id", (Integer, SAInteger), {"primary_key": True}),
            ("col name", (Integer, SAInteger), {}),
        ],
    )
    _assert_create_parity(table, sa_table)


def test_drop_table_matches_sqlalchemy() -> None:
    table, sa_table = _paired_table("users", [("id", (Integer, SAInteger), {"primary_key": True})])
    _assert_drop_parity(table, sa_table)


def test_unsupported_type_raises_argument_error() -> None:
    metadata = MetaData()
    table = Table("bad", metadata, Column("value", object()))
    with pytest.raises(ArgumentError, match="Unsupported DDL column type"):
        table.compile_ddl()


def test_metadata_create_all_and_row_behavior_match_sqlalchemy() -> None:
    table, sa_table = _paired_table(
        "records",
        [
            ("id", (Integer, SAInteger), {"primary_key": True}),
            ("name", (String(30), SAString(30)), {}),
        ],
    )
    engine = create_engine(":memory:")
    sqlalchemy_engine = sa_create_engine("sqlite://")
    try:
        table.metadata.create_all(engine)
        sa_table.metadata.create_all(sqlalchemy_engine)
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=1, name="Ada"))
            connection.execute(insert(table).values(id=2, name="Grace"))
            catalyst_rows = connection.execute(
                select(table.c.name).order_by(table.c.id.asc())
            ).fetchall()
        with sqlalchemy_engine.begin() as connection:
            connection.execute(sa_insert(sa_table).values(id=1, name="Ada"))
            connection.execute(sa_insert(sa_table).values(id=2, name="Grace"))
            sqlalchemy_rows = connection.execute(
                sa_select(sa_table.c.name).order_by(sa_table.c.id.asc())
            ).fetchall()
        assert catalyst_rows == sqlalchemy_rows
        # checkfirst=True (default) makes a second create_all a no-op.
        table.metadata.create_all(engine)
        with engine.connect() as connection:
            rows = connection.execute(select(table.c.name)).fetchall()
        assert rows == [("Ada",), ("Grace",)]
        table.metadata.drop_all(engine)
        table.metadata.drop_all(engine)  # checkfirst skips the missing table
        table.metadata.create_all(engine)
        with engine.connect() as connection:
            assert connection.execute(select(table.c.name)).fetchall() == []
            connection.execute(update(table).values(name="x").where(table.c.id == 1))
            assert connection.execute(delete(table).where(table.c.id == 9)).rowcount == 0
    finally:
        engine.dispose()
        sqlalchemy_engine.dispose()


def test_table_create_drop_and_checkfirst_match_sqlalchemy() -> None:
    table, _sa_table = _paired_table("users", [("id", (Integer, SAInteger), {"primary_key": True})])
    engine = create_engine(":memory:")
    try:
        table.create(engine, checkfirst=False)
        with engine.connect() as connection:
            assert connection.has_table("users")
        table.create(engine)  # checkfirst=True default: no-op
        with engine.connect() as connection:
            assert connection.has_table("users")
        table.drop(engine)
        with engine.connect() as connection:
            assert not connection.has_table("users")
        table.drop(engine)  # checkfirst=True default: no-op on missing table
        with engine.connect() as connection:
            assert not connection.has_table("users")
        table.create(engine, checkfirst=False)
        # checkfirst=False against an existing table fails like SQLAlchemy.
        with pytest.raises(Exception) as catalyst_error:
            table.create(engine, checkfirst=False)
        assert type(catalyst_error.value).__name__ == "OperationalError"
    finally:
        engine.dispose()


async def test_async_engine_create_all_round_trip() -> None:
    from catalyst import create_async_engine

    table, _sa_table = _paired_table(
        "async_records",
        [
            ("id", (Integer, SAInteger), {"primary_key": True}),
            ("name", (String(30), SAString(30)), {}),
        ],
    )
    engine = create_async_engine(":memory:")
    try:
        await table.metadata.create_all(engine)
        await table.metadata.create_all(engine)  # checkfirst no-op
        connection = await engine.connect_async()
        try:
            await connection.execute(insert(table).values(id=1, name="Ada"))
            rows = await connection.execute(select(table.c.name))
            assert rows.fetchall() == [("Ada",)]
            assert await connection.has_table("async_records")
        finally:
            await connection.close()
        await table.metadata.drop_all(engine)
        await table.metadata.drop_all(engine)
        connection = await engine.connect_async()
        try:
            assert not await connection.has_table("async_records")
        finally:
            await connection.close()
        await table.metadata.create_all(engine)
    finally:
        await engine.dispose()


def test_empty_table_ddl_matches_sqlalchemy() -> None:
    """An edge SQLAlchemy compiles: a table with no columns."""
    metadata, sa_metadata = MetaData(), SAMetaData()
    table = Table("empty", metadata)
    sa_table = SATable("empty", sa_metadata)
    catalyst_sql = table.compile_ddl().string
    sa_sql = str(SACreateTable(sa_table).compile(dialect=sqlite.dialect()))
    assert catalyst_sql == sa_sql == "\nCREATE TABLE empty (\n)\n\n"
