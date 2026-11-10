"""Live MySQL checks for Catalyst's async driver slice (ADR 0004).

Text execution, typed result hydration, transactions, ``has_table``, pool
semantics, and error mapping are compared with SQLAlchemy + asyncmy.
Compiled Core statements execute through the MySQL dialect (see
``test_mysql_core.py``) and executemany batches run natively (see
``test_mysql_executemany.py``).
"""

from __future__ import annotations

import asyncio
import datetime
import decimal
import os
import re
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from catalyst import (
    IntegrityError,
    TimeoutError,
    create_async_engine,
    text,
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


def test_mysql_async_url_routes_to_native_driver() -> None:
    engine = create_async_engine(
        "mysql+asyncmy://catalyst:secret@localhost/catalyst?sslmode=disable"
    )
    assert engine._mysql is True
    assert engine._echo is False
    asyncio.run(engine.dispose())


@pytest.fixture
async def table_name() -> AsyncIterator[str]:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    name = f"catalyst_mysql_{uuid.uuid4().hex[:10]}"
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        yield name
    finally:
        async with sa_engine.begin() as connection:
            for suffix in ("", "_sa"):
                await connection.execute(sa.text(f"DROP TABLE IF EXISTS `{name}{suffix}`"))
        await sa_engine.dispose()


async def test_mysql_named_text_execution_matches_sqlalchemy(table_name: str) -> None:
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        sa_table_name = f"{table_name}_sa"
        ddl = (
            "(id INT PRIMARY KEY, name VARCHAR(30), flag BOOL, score DOUBLE, "
            "created DATETIME(6), day DATE, payload BLOB, amount DECIMAL(10,2))"
        )
        row = {
            "id": 1,
            "name": "héllo",
            "flag": True,
            "score": 2.5,
            "created": datetime.datetime(2026, 11, 9, 12, 34, 56, 123456),
            "day": datetime.date(2026, 11, 9),
            "payload": b"\x00\xff",
            "amount": decimal.Decimal("19.99"),
        }
        insert = "VALUES (:id, :name, :flag, :score, :created, :day, :payload, :amount)"
        async with catalyst_engine.connect() as connection:
            await connection.execute(text(f"CREATE TABLE `{table_name}` {ddl}"))
            inserted = await connection.execute(text(f"INSERT INTO `{table_name}` {insert}"), row)
            assert inserted.rowcount == 1
            await connection.commit()
        async with sa_engine.begin() as connection:
            await connection.execute(sa.text(f"CREATE TABLE `{sa_table_name}` {ddl}"))
            sa_inserted = await connection.execute(
                sa.text(f"INSERT INTO `{sa_table_name}` {insert}"), row
            )
        # asyncmy reports the same affected-row count for inserts.
        assert inserted.rowcount == sa_inserted.rowcount

        async with catalyst_engine.connect() as connection:
            result = await connection.execute(text(f"SELECT * FROM `{table_name}` ORDER BY id"))
            keys = result.keys
            catalyst_rows = result.fetchall()
            await connection.commit()
        async with sa_engine.connect() as connection:
            sa_result = await connection.execute(
                sa.text(f"SELECT * FROM `{sa_table_name}` ORDER BY id")
            )
            assert keys == tuple(sa_result.keys())
            sa_rows = [tuple(row) for row in sa_result.fetchall()]
        assert catalyst_rows == sa_rows
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_dml_rowcounts_match_sqlalchemy(table_name: str) -> None:
    sa_table_name = f"{table_name}_sa"
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        async with catalyst_engine.begin() as connection:
            await connection.execute(
                text(f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY, v INT)")
            )
            await connection.execute(
                text(f"INSERT INTO `{table_name}` VALUES (:id, :v)"),
                {"id": 1, "v": 10},
            )
        async with catalyst_engine.begin() as connection:
            updated = await connection.execute(
                text(f"UPDATE `{table_name}` SET v = :v WHERE id = :id"),
                {"v": 20, "id": 1},
            )
            deleted = await connection.execute(
                text(f"DELETE FROM `{table_name}` WHERE id = :id"), {"id": 1}
            )
        async with sa_engine.begin() as connection:
            await connection.execute(
                sa.text(f"CREATE TABLE `{sa_table_name}` (id INT PRIMARY KEY, v INT)")
            )
            await connection.execute(
                sa.text(f"INSERT INTO `{sa_table_name}` VALUES (:id, :v)"),
                {"id": 1, "v": 10},
            )
        async with sa_engine.begin() as connection:
            sa_updated = await connection.execute(
                sa.text(f"UPDATE `{sa_table_name}` SET v = :v WHERE id = :id"),
                {"v": 20, "id": 1},
            )
            sa_deleted = await connection.execute(
                sa.text(f"DELETE FROM `{sa_table_name}` WHERE id = :id"), {"id": 1}
            )
        assert updated.rowcount == sa_updated.rowcount
        assert deleted.rowcount == sa_deleted.rowcount
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_transactions_commit_and_rollback(table_name: str) -> None:
    engine = create_async_engine(MYSQL_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY)"))
        async with engine.begin() as connection:
            await connection.execute(
                text(f"INSERT INTO `{table_name}` (id) VALUES (:id)"), {"id": 1}
            )
        # Uncommitted work rolls back on connection close (pool return).
        connection = await engine.connect()
        await connection.execute(text(f"INSERT INTO `{table_name}` (id) VALUES (:id)"), {"id": 2})
        await connection.close()
        # Explicit rollback discards the autobegun transaction.
        async with engine.connect() as connection:
            await connection.execute(
                text(f"INSERT INTO `{table_name}` (id) VALUES (:id)"), {"id": 3}
            )
            await connection.rollback()
        async with engine.connect() as connection:
            rows = (
                await connection.execute(text(f"SELECT id FROM `{table_name}` ORDER BY id"))
            ).fetchall()
            assert rows == [(1,)]
            await connection.commit()
    finally:
        await engine.dispose()


async def test_mysql_has_table_matches_sqlalchemy_inspect(table_name: str) -> None:
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        async with catalyst_engine.connect() as connection:
            assert await connection.has_table(table_name) is False
            await connection.execute(text(f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY)"))
            await connection.commit()
            assert await connection.has_table(table_name) is True
        async with sa_engine.connect() as connection:
            sa_has_table = await connection.run_sync(
                lambda sync_conn: sa.inspect(sync_conn).has_table(table_name)
            )
            assert sa_has_table is True
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_integrity_error_matches_sqlalchemy(table_name: str) -> None:
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        sa_table_name = f"{table_name}_sa"
        async with catalyst_engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY)"))
            await connection.execute(
                text(f"INSERT INTO `{table_name}` (id) VALUES (:id)"), {"id": 1}
            )
        async with sa_engine.begin() as connection:
            await connection.execute(
                sa.text(f"CREATE TABLE `{sa_table_name}` (id INT PRIMARY KEY)")
            )
            await connection.execute(
                sa.text(f"INSERT INTO `{sa_table_name}` (id) VALUES (:id)"), {"id": 1}
            )
        with pytest.raises(IntegrityError):
            async with catalyst_engine.connect() as connection:
                await connection.execute(
                    text(f"INSERT INTO `{table_name}` (id) VALUES (:id)"), {"id": 1}
                )
        with pytest.raises(sa.exc.IntegrityError):
            async with sa_engine.connect() as connection:
                await connection.execute(
                    sa.text(f"INSERT INTO `{sa_table_name}` (id) VALUES (:id)"),
                    {"id": 1},
                )
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_empty_select_keeps_column_names(table_name: str) -> None:
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        async with catalyst_engine.begin() as connection:
            await connection.execute(
                text(f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY, v INT)")
            )
        async with catalyst_engine.connect() as connection:
            result = await connection.execute(
                text(f"SELECT id, v FROM `{table_name}` WHERE id = :id"), {"id": 999}
            )
            keys = result.keys
            rows = result.fetchall()
            await connection.commit()
        async with sa_engine.connect() as connection:
            sa_result = await connection.execute(
                sa.text(f"SELECT id, v FROM `{table_name}` WHERE id = :id"), {"id": 999}
            )
            assert keys == tuple(sa_result.keys()) == ("id", "v")
            assert rows == sa_result.fetchall() == []
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_unpreparable_statements_use_text_protocol(table_name: str) -> None:
    del table_name
    engine = create_async_engine(MYSQL_URL)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT VERSION()"))
            row = result.fetchone()
            assert row is not None and isinstance(row[0], str)
            # SHOW statements cannot be prepared; the text-protocol fallback
            # still decodes rows by column type.
            shown = (await connection.execute(text("SHOW TABLES"))).fetchall()
            assert all(isinstance(item[0], str) for item in shown)
            await connection.commit()
    finally:
        await engine.dispose()


async def test_mysql_select_executemany_runs_per_group_like_sqlalchemy(
    table_name: str,
) -> None:
    # asyncmy executemany does NOT reject SELECT batches (PyMySQL's
    # "executemany() can only execute DML statements." does not apply here):
    # it executes once per parameter group, discards each result set, and
    # reports the summed rowcount — verified live: `SELECT :x` x2 groups
    # yields rowcount 2, and `SELECT *` over a 2-row table x2 groups yields 4.
    sa_table_name = f"{table_name}_sa"
    catalyst_engine = create_async_engine(MYSQL_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        async with catalyst_engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE `{table_name}` (id INT PRIMARY KEY)"))
            await connection.execute(
                text(f"INSERT INTO `{table_name}` VALUES (:id)"),
                [{"id": 1}, {"id": 2}],
            )
        async with sa_engine.begin() as connection:
            await connection.execute(
                sa.text(f"CREATE TABLE `{sa_table_name}` (id INT PRIMARY KEY)")
            )
            await connection.execute(
                sa.text(f"INSERT INTO `{sa_table_name}` VALUES (:id)"),
                [{"id": 1}, {"id": 2}],
            )
        async with catalyst_engine.connect() as connection:
            bound = await connection.execute(text("SELECT :x"), [{"x": 1}, {"x": 2}])
            scanned = await connection.execute(text(f"SELECT * FROM `{table_name}`"), [{}, {}])
            await connection.rollback()
        async with sa_engine.connect() as connection:
            sa_bound = await connection.execute(sa.text("SELECT :x"), [{"x": 1}, {"x": 2}])
            sa_scanned = await connection.execute(
                sa.text(f"SELECT * FROM `{sa_table_name}`"), [{}, {}]
            )
        assert bound.all() == sa_bound.all() == []
        assert bound.rowcount == sa_bound.rowcount == 2
        assert scanned.all() == sa_scanned.all() == []
        assert scanned.rowcount == sa_scanned.rowcount == 4
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_driver_does_not_leak_connection_url_on_failure() -> None:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    engine = create_async_engine("mysql://catalyst:secret@127.0.0.1:1/catalyst?sslmode=disable")
    try:
        with pytest.raises(Exception) as raised:
            await engine.connect()
        assert "secret" not in str(raised.value)
        assert "127.0.0.1:1" not in str(raised.value)
    finally:
        await engine.dispose()


async def test_mysql_pool_reuses_returned_connections() -> None:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    engine = create_async_engine(MYSQL_URL, pool_size=2, max_overflow=0)
    try:
        async with engine.connect() as first:
            first_id = (await first.execute(text("SELECT CONNECTION_ID()"))).fetchone()
        async with engine.connect() as second:
            second_id = (await second.execute(text("SELECT CONNECTION_ID()"))).fetchone()
        assert first_id == second_id
        async with engine.connect() as held:
            held_id = (await held.execute(text("SELECT CONNECTION_ID()"))).fetchone()
            async with engine.connect() as other:
                other_id = (await other.execute(text("SELECT CONNECTION_ID()"))).fetchone()
        assert held_id != other_id
    finally:
        await engine.dispose()


async def test_mysql_pool_exhaustion_times_out_like_sqlalchemy() -> None:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    catalyst_engine = create_async_engine(MYSQL_URL, pool_size=1, max_overflow=0, pool_timeout=0.2)
    sa_engine = sa_create_async_engine(_sa_url(), pool_size=1, max_overflow=0, pool_timeout=0.2)
    try:
        async with catalyst_engine.connect():
            with pytest.raises(TimeoutError):
                await catalyst_engine.connect()
        async with sa_engine.connect():
            with pytest.raises(sa.exc.TimeoutError):
                await sa_engine.connect()
        async with catalyst_engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).fetchone() == (1,)
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_mysql_pool_dispose_rejects_new_checkouts() -> None:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    engine = create_async_engine(MYSQL_URL, pool_timeout=0.5)
    held = await engine.connect()
    await engine.dispose()
    with pytest.raises(RuntimeError, match="disposed"):
        await engine.connect()
    await held.close()
    with pytest.raises(RuntimeError, match="closed"):
        await held.execute(text("SELECT 1"))


async def test_mysql_pool_concurrent_checkout_stress() -> None:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    engine = create_async_engine(MYSQL_URL, pool_size=2, max_overflow=1)

    async def worker() -> int:
        async with engine.connect() as connection:
            row = (await connection.execute(text("SELECT 1"))).fetchone()
            assert row is not None
            return int(row[0])

    try:
        results = await asyncio.gather(*[worker() for _ in range(20)])
        assert results == [1] * 20
    finally:
        await engine.dispose()
