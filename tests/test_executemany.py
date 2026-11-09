"""SQLite executemany behavior compared with SQLAlchemy 2.1."""

from __future__ import annotations

import pytest
from catalyst import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy import (
    Column as SAColumn,
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
    create_engine as sa_create_engine,
)
from sqlalchemy import (
    delete as sa_delete,
)
from sqlalchemy import (
    insert as sa_insert,
)
from sqlalchemy import (
    select as sa_select,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy import (
    update as sa_update,
)
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import ProgrammingError as SAProgrammingError


def _tables() -> tuple[Table, SATable]:
    catalyst_table = Table(
        "batch_rows",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("value", String),
    )
    sa_table = SATable(
        "batch_rows",
        SAMetaData(),
        SAColumn("id", SAInteger, primary_key=True),
        SAColumn("value", SAString),
    )
    return catalyst_table, sa_table


def test_compiled_insert_update_delete_batches_match_sqlalchemy_rowcounts() -> None:
    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    rows = [{"id": 1, "value": "one"}, {"id": 2, "value": "two"}, {"id": 3, "value": "three"}]
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            catalyst_insert = connection.execute(insert(catalyst_table), rows)
            catalyst_update = connection.execute(
                update(catalyst_table).values(value="changed").where(catalyst_table.c.id == 2),
                [{"value": "two*", "id_1": 2}, {"value": "three*", "id_1": 3}],
            )
            catalyst_delete = connection.execute(
                delete(catalyst_table).where(catalyst_table.c.id == 1),
                [{"id_1": 1}, {"id_1": 2}],
            )
            catalyst_result = connection.execute(select(catalyst_table)).all()
        with sqlalchemy.begin() as connection:
            sa_insert_result = connection.execute(sa_insert(sa_table), rows)
            sa_update_result = connection.execute(
                sa_update(sa_table).where(sa_table.c.id == 2).values(value="changed"),
                [{"value": "two*", "id_1": 2}, {"value": "three*", "id_1": 3}],
            )
            sa_delete_result = connection.execute(
                sa_delete(sa_table).where(sa_table.c.id == 1),
                [{"id_1": 1}, {"id_1": 2}],
            )
            sa_result = connection.execute(sa_select(sa_table)).all()
        assert (
            (catalyst_insert.rowcount, catalyst_update.rowcount, catalyst_delete.rowcount)
            == (
                sa_insert_result.rowcount,
                sa_update_result.rowcount,
                sa_delete_result.rowcount,
            )
            == (3, 2, 2)
        )
        assert catalyst_result == sa_result == [(3, "three*")]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_empty_parameter_sequence_executes_compiled_statement_once() -> None:
    # SQLAlchemy 2.1 currently warns that empty execute parameter sequences
    # are deprecated; parity is intentionally pinned to its current behavior.
    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            inserted = connection.execute(insert(catalyst_table).values(id=1, value="fixed"), [])
            updated = connection.execute(
                update(catalyst_table).values(value="updated").where(catalyst_table.c.id == 1),
                [],
            )
            selected = connection.execute(
                select(catalyst_table.c.value).where(catalyst_table.c.id == 1), []
            ).all()
            deleted = connection.execute(delete(catalyst_table).where(catalyst_table.c.id == 1), [])
            catalyst_rows = connection.execute(select(catalyst_table)).all()
        with sqlalchemy.begin() as connection:
            sa_inserted = connection.execute(sa_insert(sa_table).values(id=1, value="fixed"), [])
            sa_updated = connection.execute(
                sa_update(sa_table).values(value="updated").where(sa_table.c.id == 1), []
            )
            sa_selected = connection.execute(
                sa_select(sa_table.c.value).where(sa_table.c.id == 1), []
            ).all()
            sa_deleted = connection.execute(sa_delete(sa_table).where(sa_table.c.id == 1), [])
            sa_rows = connection.execute(sa_select(sa_table)).all()
        assert inserted.rowcount == sa_inserted.rowcount == 1
        assert updated.rowcount == sa_updated.rowcount == 1
        assert selected == sa_selected == [("updated",)]
        assert deleted.rowcount == sa_deleted.rowcount == 1
        assert catalyst_rows == sa_rows == []
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_empty_parameter_sequence_on_insert_without_values_uses_default_values() -> None:
    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            catalyst_result = connection.execute(insert(catalyst_table), [])
            catalyst_rows = connection.execute(select(catalyst_table)).all()
        with sqlalchemy.begin() as connection:
            sa_result = connection.execute(sa_insert(sa_table), [])
            sa_rows = connection.execute(sa_select(sa_table)).all()
        assert catalyst_result.rowcount == sa_result.rowcount == 1
        assert catalyst_rows == sa_rows == [(1, None)]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_empty_text_batch_still_reports_missing_bind_like_sqlalchemy() -> None:
    from catalyst import StatementError
    from sqlalchemy.exc import StatementError as SAStatementError

    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        with catalyst.connect() as connection:
            with pytest.raises(StatementError):
                connection.execute(text("SELECT :value"), [])
        with sqlalchemy.connect() as connection:
            with pytest.raises(SAStatementError):
                connection.execute(sa_text("SELECT :value"), [])
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_text_named_executemany_matches_sqlalchemy() -> None:
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    params = [{"id": 1, "value": "a"}, {"id": 2, "value": "b"}]
    try:
        with catalyst.begin() as connection:
            connection.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
            result = connection.execute(text("INSERT INTO entries VALUES (:id, :value)"), params)
            rows = connection.execute(text("SELECT value FROM entries ORDER BY id")).all()
        with sqlalchemy.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE entries (id INTEGER PRIMARY KEY, value TEXT)")
            sa_result = connection.execute(
                sa_text("INSERT INTO entries VALUES (:id, :value)"), params
            )
            sa_rows = connection.execute(sa_text("SELECT value FROM entries ORDER BY id")).all()
        assert result.rowcount == sa_result.rowcount == 2
        assert rows == sa_rows == [("a",), ("b",)]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_positional_text_executemany_is_supported() -> None:
    engine = create_engine(":memory:")
    try:
        with engine.begin() as connection:
            connection.execute("CREATE TABLE positional (value INTEGER)")
            result = connection.execute("INSERT INTO positional VALUES (?)", [(1,), (2,)])
            assert result.rowcount == 2
            assert connection.execute("SELECT value FROM positional ORDER BY value").all() == [
                (1,),
                (2,),
            ]
    finally:
        engine.dispose()


def test_batch_failure_leaves_partial_rows_in_open_transaction_like_sqlalchemy() -> None:
    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    batch = [
        {"id": 1, "value": "first"},
        {"id": 1, "value": "duplicate"},
        {"id": 2, "value": "last"},
    ]
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.connect() as connection, sqlalchemy.connect() as sa_connection:
            with pytest.raises(Exception) as catalyst_error:
                connection.execute(insert(catalyst_table), batch)
            with pytest.raises(SAIntegrityError) as sa_error:
                sa_connection.execute(sa_insert(sa_table), batch)
            assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
            assert (
                connection.execute(select(catalyst_table)).all()
                == sa_connection.execute(sa_select(sa_table)).all()
                == [(1, "first")]
            )
            connection.rollback()
            sa_connection.rollback()
            assert connection.execute(select(catalyst_table)).all() == []
            assert sa_connection.execute(sa_select(sa_table)).all() == []
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_select_executemany_matches_sqlalchemy_programming_error() -> None:
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        with catalyst.connect() as connection, sqlalchemy.connect() as sa_connection:
            with pytest.raises(Exception) as catalyst_error:
                connection.execute(text("SELECT :value"), [{"value": 1}, {"value": 2}])
            with pytest.raises(SAProgrammingError) as sa_error:
                sa_connection.execute(sa_text("SELECT :value"), [{"value": 1}, {"value": 2}])
            assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
            assert str(catalyst_error.value) == "executemany() can only execute DML statements."
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


async def test_async_compiled_empty_parameters_execute_compiled_values_once() -> None:
    from catalyst import create_async_engine

    table = Table("async_empty", MetaData(), Column("id", Integer), Column("value", String))
    engine = create_async_engine(":memory:")
    try:
        connection = await engine.connect_async()
        try:
            await connection.execute("CREATE TABLE async_empty (id INTEGER, value TEXT)")
            result = await connection.execute(insert(table).values(id=1, value="fixed"), [])
            rows = await connection.execute(select(table), [])
            assert result.rowcount == 1
            assert rows.all() == [(1, "fixed")]
        finally:
            await connection.close()
    finally:
        await engine.dispose()


async def test_async_executemany() -> None:
    from catalyst import create_async_engine

    engine = create_async_engine(":memory:")
    try:
        connection = await engine.connect_async()
        try:
            await connection.execute("CREATE TABLE async_batch (id INTEGER)")
            result = await connection.execute(
                text("INSERT INTO async_batch VALUES (:id)"), [{"id": 1}, {"id": 2}]
            )
            assert result.rowcount == 2
            assert (await connection.execute("SELECT id FROM async_batch ORDER BY id")).all() == [
                (1,),
                (2,),
            ]
        finally:
            await connection.close()
    finally:
        await engine.dispose()


def test_executemany_merges_statement_values_with_row_overrides() -> None:
    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            inserted = connection.execute(
                insert(catalyst_table).values(value="fixed"),
                [{"id": 1}, {"id": 2, "value": "override"}],
            )
            updated = connection.execute(
                update(catalyst_table).values(value="fixed-update").where(catalyst_table.c.id == 1),
                [{"id_1": 1}],
            )
            rows = connection.execute(select(catalyst_table).order_by(catalyst_table.c.id)).all()
        with sqlalchemy.begin() as connection:
            sa_inserted = connection.execute(
                sa_insert(sa_table).values(value="fixed"),
                [{"id": 1}, {"id": 2, "value": "override"}],
            )
            sa_updated = connection.execute(
                sa_update(sa_table).values(value="fixed-update").where(sa_table.c.id == 1),
                [{"id_1": 1}],
            )
            sa_rows = connection.execute(sa_select(sa_table).order_by(sa_table.c.id)).all()
        assert inserted.rowcount == sa_inserted.rowcount == 2
        assert updated.rowcount == sa_updated.rowcount == 1
        assert rows == sa_rows == [(1, "fixed-update"), (2, "override")]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


async def _async_batch_defaults_roundtrip() -> None:
    from catalyst import create_async_engine
    from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine

    catalyst_table, sa_table = _tables()
    catalyst = create_async_engine(":memory:")
    sqlalchemy = sa_create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        await catalyst_table.create(catalyst)
        async with sqlalchemy.begin() as connection:
            await connection.run_sync(sa_table.metadata.create_all)
        async with catalyst.begin() as connection:
            result = await connection.execute(
                insert(catalyst_table).values(value="fixed"),
                [{"id": 1}, {"id": 2, "value": "override"}],
            )
            rows = (
                await connection.execute(select(catalyst_table).order_by(catalyst_table.c.id))
            ).all()
        async with sqlalchemy.begin() as connection:
            sa_result = await connection.execute(
                sa_insert(sa_table).values(value="fixed"),
                [{"id": 1}, {"id": 2, "value": "override"}],
            )
            sa_rows = (await connection.execute(sa_select(sa_table).order_by(sa_table.c.id))).all()
        assert result.rowcount == sa_result.rowcount == 2
        assert rows == sa_rows == [(1, "fixed"), (2, "override")]
    finally:
        await catalyst.dispose()
        await sqlalchemy.dispose()


def test_async_executemany_merges_statement_values() -> None:
    import asyncio
    import importlib.util

    if (
        importlib.util.find_spec("aiosqlite") is None
        or importlib.util.find_spec("greenlet") is None
    ):
        pytest.skip("SQLAlchemy async SQLite comparison needs aiosqlite and greenlet")
    asyncio.run(_async_batch_defaults_roundtrip())


def test_heterogeneous_insert_rows_require_every_row_only_bind() -> None:
    from catalyst import StatementError
    from sqlalchemy.exc import StatementError as SAStatementError

    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    rows = [{"id": 1, "value": "one"}, {"value": "two"}]
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.connect() as connection:
            with pytest.raises(StatementError) as catalyst_error:
                connection.execute(insert(catalyst_table), rows)
        with sqlalchemy.connect() as connection:
            with pytest.raises(SAStatementError) as sa_error:
                connection.execute(sa_insert(sa_table), rows)
        assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
        assert "parameter group 1" in str(catalyst_error.value)
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_insert_batch_shape_comes_from_first_row_and_ignores_later_keys() -> None:
    catalyst_table, sa_table = _tables()
    catalyst = create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    rows = [
        {"id": 1, "value": "first"},
        {"id": 2, "value": "second", "not_a_column": 99},
    ]
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            first_result = connection.execute(insert(catalyst_table), rows)
            second_result = connection.execute(
                insert(catalyst_table).values(id=10),
                [{"id": 3}, {"id": 4, "value": "ignored-later-key"}],
            )
            actual = connection.execute(select(catalyst_table).order_by(catalyst_table.c.id)).all()
        with sqlalchemy.begin() as connection:
            first_sa = connection.execute(sa_insert(sa_table), rows)
            second_sa = connection.execute(
                sa_insert(sa_table).values(id=10),
                [{"id": 3}, {"id": 4, "value": "ignored-later-key"}],
            )
            expected = connection.execute(sa_select(sa_table).order_by(sa_table.c.id)).all()
        assert (
            (first_result.rowcount, second_result.rowcount)
            == (
                first_sa.rowcount,
                second_sa.rowcount,
            )
            == (2, 2)
        )
        assert actual == expected == [(1, "first"), (2, "second"), (3, None), (4, None)]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()
