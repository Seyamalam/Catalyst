"""Live MySQL executemany parity for compiled Core and text() against SQLAlchemy (asyncmy)."""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from catalyst import (
    ArgumentError,
    Column,
    Integer,
    MetaData,
    StatementError,
    String,
    Table,
    create_async_engine,
    delete,
    insert,
    select,
    text,
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
            # Only drop tables carrying this fixture's uuid suffix; the MySQL
            # container is shared with other workers using the same gt_ prefix.
            names = (
                (
                    await connection.execute(
                        sa.text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema = DATABASE() AND table_name LIKE :suffix"
                        ),
                        {"suffix": f"%{suffix}"},
                    )
                )
                .scalars()
                .all()
            )
            for name in names:
                await connection.execute(sa.text(f"DROP TABLE IF EXISTS `{name}`"))
        await sa_engine.dispose()
        await catalyst_engine.dispose()


def _tables(suffix: str) -> tuple[MetaData, Table, sa.Table]:
    metadata = MetaData()
    table = Table(
        f"gt_em_{suffix}",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("v", String(30)),
        Column("n", Integer),
    )
    sa_table = sa.Table(
        f"gt_sa_{suffix}",
        sa.MetaData(),
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("v", sa.String(30)),
        sa.Column("n", sa.Integer),
    )
    return metadata, table, sa_table


async def _create_pair(
    catalyst_engine: Any, sa_engine: Any, suffix: str
) -> tuple[MetaData, Table, sa.Table]:
    metadata, table, sa_table = _tables(suffix)
    await metadata.create_all(catalyst_engine)
    async with sa_engine.begin() as connection:
        await connection.run_sync(sa_table.metadata.create_all)
    return metadata, table, sa_table


async def test_executemany_insert_batch_rowcount_and_rows(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # asyncmy executemany reports the aggregate affected-row count (3 for a
    # 3-row INSERT), matching Catalyst's summed rowcount.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    rows = [{"id": i, "v": f"v{i}", "n": i} for i in range(1, 4)]
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(insert(table), rows)
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(sa.insert(sa_table), rows)
    assert catalyst_result.rowcount == sa_result.rowcount == 3
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v, table.c.n).order_by(table.c.id))
        ).all()
    async with sa_engine.connect() as connection:
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v, sa_table.c.n).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_rows == sa_rows == [(1, "v1", 1), (2, "v2", 2), (3, "v3", 3)]


async def test_executemany_insert_merges_statement_values_with_row_overrides(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # Statement-level .values() supplies defaults; keys present in a row win.
    # The INSERT shape is fixed by the first row plus .values().
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    rows = [{"id": 1}, {"id": 2, "v": "override"}]
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(insert(table).values(v="fixed"), rows)
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v).order_by(table.c.id))
        ).all()
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(sa.insert(sa_table).values(v="fixed"), rows)
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_result.rowcount == sa_result.rowcount == 2
    assert catalyst_rows == sa_rows == [(1, "fixed"), (2, "override")]


async def test_executemany_insert_ignores_later_row_unknown_keys(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    rows = [
        {"id": 1, "v": "first"},
        {"id": 2, "v": "second", "not_a_column": 99},
    ]
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(insert(table), rows)
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(sa.insert(sa_table), rows)
    assert catalyst_result.rowcount == sa_result.rowcount == 2
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v).order_by(table.c.id))
        ).all()
    async with sa_engine.connect() as connection:
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_rows == sa_rows == [(1, "first"), (2, "second")]


async def test_executemany_update_delete_batches_match_sqlalchemy_rowcounts(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # Literal comparisons in WHERE compile to per-row bind params (id_1); each
    # row dict supplies the bind values for one executemany iteration.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    rows = [{"id": i, "v": f"row{i}", "n": i} for i in range(1, 4)]
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table), rows)
    async with sa_engine.begin() as connection:
        await connection.execute(sa.insert(sa_table), rows)
    async with catalyst_engine.begin() as connection:
        catalyst_updated = await connection.execute(
            update(table).where(table.c.id == 2).values(v="changed"),
            [{"id_1": 2}, {"id_1": 3}, {"id_1": 99}],
        )
        catalyst_deleted = await connection.execute(
            delete(table).where(table.c.id == 1),
            [{"id_1": 1}, {"id_1": 2}],
        )
    async with sa_engine.begin() as connection:
        sa_updated = await connection.execute(
            sa.update(sa_table).where(sa_table.c.id == 2).values(v="changed"),
            [{"id_1": 2}, {"id_1": 3}, {"id_1": 99}],
        )
        sa_deleted = await connection.execute(
            sa.delete(sa_table).where(sa_table.c.id == 1),
            [{"id_1": 1}, {"id_1": 2}],
        )
    assert catalyst_updated.rowcount == sa_updated.rowcount == 2
    assert catalyst_deleted.rowcount == sa_deleted.rowcount == 2
    async with catalyst_engine.connect() as connection:
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v).order_by(table.c.id))
        ).all()
    async with sa_engine.connect() as connection:
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_rows == sa_rows == [(3, "changed")]


async def test_executemany_text_named_binds_match_sqlalchemy(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    params = [{"id": 1, "v": "a", "n": 10}, {"id": 2, "v": "b", "n": 20}]
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(
            text(f"INSERT INTO {table.name} (id, v, n) VALUES (:id, :v, :n)"), params
        )
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v, table.c.n).order_by(table.c.id))
        ).all()
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(
            sa.text(f"INSERT INTO {sa_table.name} (id, v, n) VALUES (:id, :v, :n)"), params
        )
        sa_rows = [
            tuple(item)
            for item in (
                await connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v, sa_table.c.n).order_by(sa_table.c.id)
                )
            ).all()
        ]
    assert catalyst_result.rowcount == sa_result.rowcount == 2
    assert catalyst_rows == sa_rows == [(1, "a", 10), (2, "b", 20)]


async def test_executemany_missing_key_in_later_row_raises_statement_error(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # SQLAlchemy fixes the parameter set from the first row; a later row missing
    # a required bind fails with StatementError naming the 0-based group index.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    rows = [{"id": 1, "v": "a", "n": 1}, {"id": 2}]
    async with catalyst_engine.connect() as connection:
        with pytest.raises(StatementError) as catalyst_error:
            await connection.execute(insert(table), rows)
        await connection.rollback()
    async with sa_engine.connect() as connection:
        with pytest.raises(sa.exc.StatementError) as sa_error:
            await connection.execute(sa.insert(sa_table), rows)
    assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
    assert "A value is required for bind parameter 'v'" in str(catalyst_error.value)
    assert "parameter group 1" in str(catalyst_error.value)
    assert "parameter group 1" in str(sa_error.value)


async def test_executemany_select_matches_sqlalchemy_asyncmy_behavior(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # Ground truth (asyncmy 0.2.18 + SQLAlchemy 2.1.8): executemany on a SELECT
    # does NOT raise ProgrammingError — asyncmy executes the statement once per
    # parameter group, discards result sets, and aggregates rowcounts. Parity is
    # asserted by comparing both engines' outcomes directly.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, _ = await _create_pair(catalyst_engine, sa_engine, suffix)
    async with catalyst_engine.begin() as connection:
        await connection.execute(insert(table), [{"id": 1, "v": "a", "n": 1}])
    params = [{"x": 1}, {"x": 2}]

    async def _outcome(engine: Any, stmt: Any) -> tuple[str | None, int, list[Any]]:
        async with engine.connect() as connection:
            try:
                result = await connection.execute(stmt, params)
            except Exception as error:  # noqa: BLE001 - capturing class name for parity
                await connection.rollback()
                return (type(error).__name__, -1, [])
            rows = result.all()
            await connection.rollback()
            return (None, result.rowcount, rows)

    catalyst_outcome = await _outcome(catalyst_engine, text("SELECT :x AS val"))
    sa_outcome = await _outcome(sa_engine, sa.text("SELECT :x AS val"))
    assert catalyst_outcome == sa_outcome


async def test_executemany_mid_batch_failure_matches_sqlalchemy(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # asyncmy collapses executemany INSERT into a single multi-row statement,
    # so a mid-batch duplicate leaves NO partial rows visible in the open
    # transaction (verified against SQLAlchemy+asyncmy); earlier rows pending is
    # the SQLite row-by-row behavior, not MySQL's. Parity is asserted by
    # comparing both engines, then both must be empty after rollback.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    batch = [
        {"id": 1, "v": "first", "n": 1},
        {"id": 1, "v": "duplicate", "n": 2},
        {"id": 2, "v": "last", "n": 3},
    ]
    async with catalyst_engine.connect() as connection, sa_engine.connect() as sa_connection:
        await connection.begin()
        await sa_connection.begin()
        with pytest.raises(StatementError) as catalyst_error:
            await connection.execute(insert(table), batch)
        with pytest.raises(sa.exc.IntegrityError) as sa_error:
            await sa_connection.execute(sa.insert(sa_table), batch)
        assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v, table.c.n).order_by(table.c.id))
        ).all()
        sa_rows = [
            tuple(item)
            for item in (
                await sa_connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v, sa_table.c.n).order_by(sa_table.c.id)
                )
            ).all()
        ]
        assert catalyst_rows == sa_rows == []
        await connection.rollback()
        await sa_connection.rollback()
        catalyst_rows = (
            await connection.execute(select(table.c.id, table.c.v, table.c.n).order_by(table.c.id))
        ).all()
        sa_rows = [
            tuple(item)
            for item in (
                await sa_connection.execute(
                    sa.select(sa_table.c.id, sa_table.c.v, sa_table.c.n).order_by(sa_table.c.id)
                )
            ).all()
        ]
        assert catalyst_rows == sa_rows == []


async def test_executemany_empty_insert_batch_executes_once_with_defaults(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # SQLAlchemy 2.1 executes the compiled statement once for an empty
    # parameter sequence (deprecated upstream, pinned for parity): one row with
    # column defaults is inserted and rowcount is 1.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    async with catalyst_engine.begin() as connection:
        catalyst_result = await connection.execute(insert(table), [])
        catalyst_rows = (await connection.execute(select(table.c.id))).all()
    async with sa_engine.begin() as connection:
        sa_result = await connection.execute(sa.insert(sa_table), [])
        sa_rows = [
            tuple(item) for item in (await connection.execute(sa.select(sa_table.c.id))).all()
        ]
    assert catalyst_result.rowcount == sa_result.rowcount == 1
    assert catalyst_rows == sa_rows
    assert len(catalyst_rows) == 1


async def test_executemany_empty_text_batch_raises_missing_bind_like_sqlalchemy(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # An empty parameter sequence on text() still binds once, so the missing
    # named parameter surfaces as StatementError on both engines.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, _ = await _create_pair(catalyst_engine, sa_engine, suffix)
    sql = f"INSERT INTO {table.name} (id) VALUES (:id)"
    async with catalyst_engine.connect() as connection:
        with pytest.raises(StatementError, match="A value is required for bind parameter 'id'"):
            await connection.execute(text(sql), [])
        await connection.rollback()
    async with sa_engine.connect() as connection:
        with pytest.raises(
            sa.exc.StatementError, match="A value is required for bind parameter 'id'"
        ):
            await connection.execute(sa.text(sql), [])


async def test_executemany_bad_bind_value_raises_statement_error(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # Residual gap: SQLAlchemy+asyncmy forwards 'notanint' to the server and
    # surfaces DataError (errno 1366); Catalyst rejects the int bind client-side
    # and raises StatementError wrapping TypeError. Both are StatementError
    # subclasses, so parity is asserted at that level pending error-mapping work.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    rows = [{"id": 1, "v": "ok", "n": 1}, {"id": 2, "v": "bad", "n": "notanint"}]
    async with catalyst_engine.connect() as connection:
        with pytest.raises(StatementError):
            await connection.execute(insert(table), rows)
        await connection.rollback()
    async with sa_engine.connect() as connection:
        with pytest.raises(sa.exc.StatementError):
            await connection.execute(sa.insert(sa_table), rows)


async def test_executemany_positional_rows_raise_argument_error(
    mysql_pair: tuple[Any, Any, str],
) -> None:
    # Both engines require a sequence of dicts; positional tuples are rejected
    # before reaching the driver.
    catalyst_engine, sa_engine, suffix = mysql_pair
    _, table, sa_table = await _create_pair(catalyst_engine, sa_engine, suffix)
    async with catalyst_engine.connect() as connection:
        with pytest.raises(ArgumentError, match="List argument must consist only of dictionaries"):
            await connection.execute(insert(table), [(1, "a", 1)])
        await connection.rollback()
    async with sa_engine.connect() as connection:
        with pytest.raises(
            sa.exc.ArgumentError, match="List argument must consist only of dictionaries"
        ):
            await connection.execute(sa.insert(sa_table), [(1, "a", 1)])
    sql = f"INSERT INTO {table.name} (id) VALUES (:id)"
    async with catalyst_engine.connect() as connection:
        with pytest.raises(ArgumentError, match="List argument must consist only of dictionaries"):
            await connection.execute(text(sql), [(1,), (2,)])
        await connection.rollback()
    async with sa_engine.connect() as connection:
        with pytest.raises(
            sa.exc.ArgumentError, match="List argument must consist only of dictionaries"
        ):
            await connection.execute(sa.text(sql), [(1,), (2,)])
