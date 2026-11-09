from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from catalyst import TimeoutError, create_async_engine, text
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine


@pytest.mark.asyncio
async def test_async_text_execution_and_transactions_match_aiosqlite(
    tmp_path: Path,
) -> None:
    """Compare text execution and transactions with SQLAlchemy's real async
    SQLite driver (aiosqlite) on a shared file database."""
    database = tmp_path / "aiosqlite_parity.sqlite"
    catalyst_engine = create_async_engine(f"sqlite:///{database}")
    sa_engine = sa_create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with sa_engine.begin() as connection:
            await connection.execute(sa.text("CREATE TABLE items (value INTEGER)"))
        async with catalyst_engine.begin() as connection:
            result = await connection.execute(
                text("INSERT INTO items VALUES (:value)"), {"value": 12}
            )
            assert result.rowcount == 1
        async with sa_engine.begin() as connection:
            sa_result = await connection.execute(
                sa.text("INSERT INTO items VALUES (:value)"), {"value": 34}
            )
            assert sa_result.rowcount == result.rowcount
        # Rolled-back writes stay invisible on both drivers.
        async with catalyst_engine.connect() as connection:
            await connection.execute(text("INSERT INTO items VALUES (:value)"), {"value": 99})
            await connection.rollback()
            rows = (
                await connection.execute(text("SELECT value FROM items ORDER BY value"))
            ).fetchall()
            await connection.commit()
        async with sa_engine.connect() as connection:
            sa_rows = (
                await connection.execute(sa.text("SELECT value FROM items ORDER BY value"))
            ).fetchall()
        assert rows == [tuple(row) for row in sa_rows] == [(12,), (34,)]
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


@pytest.mark.asyncio
async def test_async_select_insert_and_ddl_match_sync_sqlalchemy() -> None:
    expected_engine = sqlalchemy_create_engine("sqlite:///:memory:")
    expected = expected_engine.connect()
    expected.exec_driver_sql("CREATE TABLE items (value INTEGER)")
    expected.exec_driver_sql("INSERT INTO items VALUES (?)", (12,))
    expected.commit()
    expected_value = expected.exec_driver_sql("SELECT value FROM items").scalar_one()

    engine = create_async_engine(":memory:")
    async with engine.connect() as connection:
        await connection.execute("CREATE TABLE items (value INTEGER)")
        await connection.execute("INSERT INTO items VALUES (?)", [12])
        await connection.commit()
        result = await connection.execute("SELECT value FROM items")
        assert result.keys == ("value",)
        assert result.fetchone() == (expected_value,)
        assert result.rowcount == -1
    await engine.dispose()
    expected.close()
    expected_engine.dispose()


@pytest.mark.asyncio
async def test_async_text_named_binds_and_transactions() -> None:
    engine = create_async_engine(":memory:")
    async with engine.connect() as connection:
        await connection.execute(text("CREATE TABLE items (value INTEGER)"))
        await connection.commit()
        async with engine.begin() as transaction:
            await transaction.execute(text("INSERT INTO items VALUES (:value)"), {"value": 3})
        result = await connection.execute("SELECT value FROM items")
        assert result.fetchall() == [(3,)]
        await connection.rollback()
        await connection.execute(text("INSERT INTO items VALUES (:value)"), {"value": 9})
        await connection.rollback()
        assert (await connection.execute("SELECT value FROM items")).fetchall() == [(3,)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_memory_database_is_shared_between_checkouts() -> None:
    engine = create_async_engine(":memory:")
    async with engine.connect() as first:
        await first.execute("CREATE TABLE shared (value INTEGER)")
        await first.execute("INSERT INTO shared VALUES (?)", [21])
        await first.commit()
    async with engine.connect() as second:
        assert (await second.execute("SELECT value FROM shared")).fetchall() == [(21,)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_file_database_and_lifecycle_errors(tmp_path: Path) -> None:
    engine = create_async_engine(tmp_path / "async.sqlite")
    connection = await engine.connect()
    await connection.execute("CREATE TABLE items (value INTEGER)")
    await connection.commit()
    await connection.close()
    with pytest.raises(RuntimeError, match="Connection is closed"):
        await connection.execute("SELECT 1")
    await engine.dispose()
    await engine.dispose()
    with pytest.raises(RuntimeError, match="Engine is disposed"):
        await engine.connect()
    with pytest.raises(RuntimeError, match="Engine is disposed"):
        await engine.dispose()
        await engine.connect()


@pytest.mark.asyncio
async def test_async_connect_supports_await_and_async_context_manager() -> None:
    engine = create_async_engine(":memory:")
    connection = await engine.connect()
    await connection.close()
    async with engine.connect() as managed:
        assert (await managed.execute("SELECT 7")).fetchone() == (7,)
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_begin_rolls_back_on_exception() -> None:
    engine = create_async_engine(":memory:")
    async with engine.connect() as connection:
        await connection.execute("CREATE TABLE items (value INTEGER)")
        await connection.commit()
        with pytest.raises(ValueError):
            async with engine.begin() as transaction:
                await transaction.execute("INSERT INTO items VALUES (?)", [5])
                raise ValueError("rollback")
        assert (await connection.execute("SELECT value FROM items")).fetchall() == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_engine_options_and_url_forms(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite:///{tmp_path / 'url.sqlite'}", pool_size=1, echo=False)
    async with engine.connect() as connection:
        assert (await connection.execute("SELECT 1")).fetchone() == (1,)
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_checkout_exhaustion_raises_timeout_error(tmp_path: Path) -> None:
    engine = create_async_engine(
        tmp_path / "exhaust.sqlite", pool_size=1, max_overflow=0, pool_timeout=0
    )
    held = await engine.connect()
    try:
        with pytest.raises(TimeoutError) as raised:
            await engine.connect()
        assert type(raised.value).__name__ == "TimeoutError"
    finally:
        await held.close()
        await engine.dispose()
