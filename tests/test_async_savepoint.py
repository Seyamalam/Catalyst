"""Async savepoint parity: SQLite compared with SQLAlchemy+aiosqlite on a real
file database, plus live PostgreSQL and MySQL checks.

Covers ``begin_nested`` deferred start, ``async with`` commit/rollback,
explicit ``commit``/``rollback``/``close``, and nested savepoints.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from catalyst import InvalidRequestError, create_async_engine, text
from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine

POSTGRES_URL = os.environ.get("CATALYST_POSTGRES_URL")
MYSQL_URL = os.environ.get("CATALYST_MYSQL_URL")


async def test_async_savepoint_release_and_rollback_match_aiosqlite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "savepoints.sqlite"
    catalyst_engine = create_async_engine(f"sqlite:///{database}")
    sa_engine = sa_create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with catalyst_engine.begin() as connection:
            await connection.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY)"))
        async with sa_engine.begin() as connection:
            await connection.execute(sa.text("INSERT INTO items VALUES (:id)"), {"id": 0})

        # Released savepoint persists its writes inside the outer transaction.
        # ``await conn.begin_nested()`` starts the savepoint like SQLAlchemy.
        async with catalyst_engine.connect() as connection:
            savepoint = await connection.begin_nested()
            await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 1})
            await savepoint.commit()
            await connection.commit()
        # Rolled-back savepoint discards only its own writes.
        async with catalyst_engine.connect() as connection:
            savepoint = connection.begin_nested()
            await savepoint.start()
            await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 2})
            await savepoint.rollback()
            await connection.commit()

        async with sa_engine.connect() as connection:
            sa_savepoint = await connection.begin_nested()
            await connection.execute(sa.text("INSERT INTO items VALUES (:id)"), {"id": 3})
            await sa_savepoint.commit()
            await connection.commit()
        async with sa_engine.connect() as connection:
            sa_savepoint = await connection.begin_nested()
            await connection.execute(sa.text("INSERT INTO items VALUES (:id)"), {"id": 4})
            await sa_savepoint.rollback()
            await connection.commit()

        expected = [(0,), (1,), (3,)]
        async with catalyst_engine.connect() as connection:
            rows = (await connection.execute(text("SELECT id FROM items ORDER BY id"))).fetchall()
            assert rows == expected
            await connection.commit()
        async with sa_engine.connect() as connection:
            sa_rows = (
                await connection.execute(sa.text("SELECT id FROM items ORDER BY id"))
            ).fetchall()
            assert [tuple(row) for row in sa_rows] == expected
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_async_savepoint_context_manager_commits_and_rolls_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "savepoint_cm.sqlite"
    catalyst_engine = create_async_engine(f"sqlite:///{database}")
    sa_engine = sa_create_async_engine(f"sqlite+aiosqlite:///{database}")
    try:
        async with sa_engine.begin() as connection:
            await connection.execute(sa.text("CREATE TABLE items (id INTEGER PRIMARY KEY)"))

        async with catalyst_engine.connect() as connection:
            async with connection.begin_nested():
                await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 1})
            await connection.commit()
        # Exception inside the block rolls the savepoint back.
        with pytest.raises(RuntimeError, match="boom"):
            async with catalyst_engine.connect() as connection:
                async with connection.begin_nested():
                    await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 2})
                    raise RuntimeError("boom")

        async with sa_engine.connect() as connection:
            sa_rows = (
                await connection.execute(sa.text("SELECT id FROM items ORDER BY id"))
            ).fetchall()
        assert [tuple(row) for row in sa_rows] == [(1,)]
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_async_nested_savepoints_rollback_inner_only(tmp_path: Path) -> None:
    database = tmp_path / "nested_savepoints.sqlite"
    engine = create_async_engine(f"sqlite:///{database}")
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY)"))
        async with engine.connect() as connection:
            async with connection.begin_nested():
                await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 1})
                async with connection.begin_nested():
                    await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 2})
                    raise_inner = connection.begin_nested()
                    await raise_inner.start()
                    await connection.execute(text("INSERT INTO items VALUES (:id)"), {"id": 99})
                    await raise_inner.rollback()
            await connection.commit()
            rows = (await connection.execute(text("SELECT id FROM items"))).fetchall()
            assert rows == [(1,), (2,)]
    finally:
        await engine.dispose()


async def test_async_savepoint_guards(tmp_path: Path) -> None:
    database = tmp_path / "savepoint_guards.sqlite"
    engine = create_async_engine(f"sqlite:///{database}")
    try:
        async with engine.connect() as connection:
            savepoint = connection.begin_nested()
            # commit/rollback before start() are invalid.
            with pytest.raises(InvalidRequestError):
                await savepoint.commit()
            with pytest.raises(InvalidRequestError):
                await savepoint.rollback()
            await savepoint.start()
            await savepoint.commit()
            # Finished savepoints reject further operations.
            with pytest.raises(InvalidRequestError):
                await savepoint.rollback()
            with pytest.raises(InvalidRequestError):
                await savepoint.commit()
            await connection.commit()
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_table() -> AsyncIterator[str]:
    if not POSTGRES_URL:
        pytest.skip("CATALYST_POSTGRES_URL is not set")
    name = f"catalyst_sp_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(POSTGRES_URL)
    try:
        yield name
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        await engine.dispose()


@pytest.mark.integration
async def test_postgres_async_savepoint_release_and_rollback(pg_table: str) -> None:
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE TABLE "{pg_table}" (id INTEGER PRIMARY KEY)'))
        async with engine.connect() as connection:
            async with connection.begin_nested():
                await connection.execute(text(f'INSERT INTO "{pg_table}" VALUES (:id)'), {"id": 1})
                async with connection.begin_nested() as inner:
                    await connection.execute(
                        text(f'INSERT INTO "{pg_table}" VALUES (:id)'), {"id": 2}
                    )
                    await inner.rollback()
            await connection.commit()
            rows = (
                await connection.execute(text(f'SELECT id FROM "{pg_table}" ORDER BY id'))
            ).fetchall()
            assert rows == [(1,)]
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_postgres_async_savepoint_rolls_back_on_error(pg_table: str) -> None:
    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE TABLE "{pg_table}" (id INTEGER PRIMARY KEY)'))
        async with engine.connect() as connection:
            savepoint = connection.begin_nested()
            await savepoint.start()
            await connection.execute(text(f'INSERT INTO "{pg_table}" VALUES (:id)'), {"id": 1})
            await savepoint.rollback()
            await connection.commit()
            rows = (await connection.execute(text(f'SELECT id FROM "{pg_table}"'))).fetchall()
            assert rows == []
    finally:
        await engine.dispose()


@pytest.fixture
async def mysql_table() -> AsyncIterator[str]:
    if not MYSQL_URL:
        pytest.skip("CATALYST_MYSQL_URL is not set")
    name = f"catalyst_sp_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(MYSQL_URL)
    try:
        yield name
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP TABLE IF EXISTS `{name}`"))
        await engine.dispose()


@pytest.mark.integration
async def test_mysql_async_savepoint_release_and_rollback(mysql_table: str) -> None:
    engine = create_async_engine(MYSQL_URL)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE TABLE `{mysql_table}` (id INT PRIMARY KEY)"))
        async with engine.connect() as connection:
            async with connection.begin_nested():
                await connection.execute(
                    text(f"INSERT INTO `{mysql_table}` VALUES (:id)"), {"id": 1}
                )
                async with connection.begin_nested() as inner:
                    await connection.execute(
                        text(f"INSERT INTO `{mysql_table}` VALUES (:id)"), {"id": 2}
                    )
                    await inner.rollback()
            await connection.commit()
            rows = (
                await connection.execute(text(f"SELECT id FROM `{mysql_table}` ORDER BY id"))
            ).fetchall()
            assert rows == [(1,)]
    finally:
        await engine.dispose()
