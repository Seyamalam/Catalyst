"""Live PostgreSQL pool behavior compared with SQLAlchemy's async QueuePool."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from catalyst import TimeoutError, create_async_engine, text
from sqlalchemy.ext.asyncio import create_async_engine as sa_create_async_engine

POSTGRES_URL = os.environ.get("CATALYST_POSTGRES_URL")
pytestmark = pytest.mark.integration


def _sa_url() -> str:
    assert POSTGRES_URL is not None
    return re.sub(r"^postgres(?:ql)?(?:\+[^:]+)?://", "postgresql+psycopg://", POSTGRES_URL)


@pytest.fixture
async def table_name() -> AsyncIterator[str]:
    if not POSTGRES_URL:
        pytest.skip("CATALYST_POSTGRES_URL is not set")
    name = f"catalyst_pool_{uuid.uuid4().hex[:10]}"
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        yield name
    finally:
        async with sa_engine.begin() as connection:
            await connection.execute(sa.text(f'DROP TABLE IF EXISTS "{name}"'))
        await sa_engine.dispose()


async def test_pg_pool_reuses_returned_connections(table_name: str) -> None:
    del table_name
    engine = create_async_engine(POSTGRES_URL, pool_size=2, max_overflow=0)
    try:
        async with engine.connect() as first:
            first_pid = (await first.execute(text("SELECT pg_backend_pid()"))).fetchone()
        async with engine.connect() as second:
            second_pid = (await second.execute(text("SELECT pg_backend_pid()"))).fetchone()
        assert first_pid == second_pid
        # Two simultaneous checkouts use distinct backends.
        async with engine.connect() as held:
            held_pid = (await held.execute(text("SELECT pg_backend_pid()"))).fetchone()
            async with engine.connect() as other:
                other_pid = (await other.execute(text("SELECT pg_backend_pid()"))).fetchone()
        assert held_pid != other_pid
    finally:
        await engine.dispose()


async def test_pg_pool_exhaustion_times_out_like_sqlalchemy(table_name: str) -> None:
    del table_name
    catalyst_engine = create_async_engine(
        POSTGRES_URL, pool_size=1, max_overflow=0, pool_timeout=0.2
    )
    sa_engine = sa_create_async_engine(_sa_url(), pool_size=1, max_overflow=0, pool_timeout=0.2)
    try:
        async with catalyst_engine.connect():
            with pytest.raises(TimeoutError):
                await catalyst_engine.connect()
        async with sa_engine.connect():
            with pytest.raises(sa.exc.TimeoutError):
                await sa_engine.connect()
        # A freed slot is immediately checkable again.
        async with catalyst_engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).fetchone() == (1,)
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_pg_pool_rolls_back_uncommitted_checkout(table_name: str) -> None:
    catalyst_engine = create_async_engine(POSTGRES_URL)
    sa_engine = sa_create_async_engine(_sa_url())
    try:
        async with catalyst_engine.begin() as connection:
            await connection.execute(text(f'CREATE TABLE "{table_name}" (id INTEGER PRIMARY KEY)'))
        connection = await catalyst_engine.connect()
        await connection.execute(text(f'INSERT INTO "{table_name}" (id) VALUES (1)'))
        # Closing with an open transaction rolls back on pool return, matching
        # SQLAlchemy's reset-on-return.
        await connection.close()
        sa_connection = await sa_engine.connect()
        await sa_connection.execute(sa.text(f'INSERT INTO "{table_name}" (id) VALUES (1)'))
        await sa_connection.close()
        async with catalyst_engine.connect() as check:
            assert (await check.execute(text(f'SELECT id FROM "{table_name}"'))).fetchall() == []
        async with sa_engine.connect() as sa_check:
            assert (
                await sa_check.execute(sa.text(f'SELECT id FROM "{table_name}"'))
            ).fetchall() == []
    finally:
        await catalyst_engine.dispose()
        await sa_engine.dispose()


async def test_pg_pool_overflow_uses_extra_permits(table_name: str) -> None:
    del table_name
    engine = create_async_engine(POSTGRES_URL, pool_size=1, max_overflow=1, pool_timeout=0.2)
    try:
        async with engine.connect():
            async with engine.connect():
                with pytest.raises(TimeoutError):
                    await engine.connect()
    finally:
        await engine.dispose()


async def test_pg_pool_dispose_rejects_new_checkouts(table_name: str) -> None:
    del table_name
    engine = create_async_engine(POSTGRES_URL, pool_timeout=0.5)
    held = await engine.connect()
    await engine.dispose()
    with pytest.raises(RuntimeError, match="disposed"):
        await engine.connect()
    # A checkout already in flight can still close cleanly after dispose; the
    # client is dropped rather than returned to the closed pool.
    await held.close()
    with pytest.raises(RuntimeError, match="closed"):
        await held.execute(text("SELECT 1"))


async def test_pg_pool_option_validation_matches_engine_rules() -> None:
    if not POSTGRES_URL:
        pytest.skip("CATALYST_POSTGRES_URL is not set")
    from catalyst import ArgumentError

    with pytest.raises(ArgumentError):
        create_async_engine(POSTGRES_URL, pool_size=-1)
    with pytest.raises(ArgumentError):
        create_async_engine(POSTGRES_URL, pool_timeout=-1)
    with pytest.raises(TypeError, match="Invalid argument"):
        create_async_engine(POSTGRES_URL, invalid_option=1)
    with pytest.raises(ArgumentError, match="future"):
        create_async_engine(POSTGRES_URL, future=False)


async def test_pg_pool_concurrent_checkout_stress(table_name: str) -> None:
    del table_name
    engine = create_async_engine(POSTGRES_URL, pool_size=2, max_overflow=1)

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
