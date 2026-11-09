"""Real PostgreSQL checks for Catalyst's initial async text driver slice."""

from __future__ import annotations

import os

import pytest
from catalyst import create_async_engine, text

POSTGRES_URL = os.environ.get("CATALYST_POSTGRES_URL")
pytestmark = pytest.mark.integration


def test_postgres_async_url_routes_to_native_driver() -> None:
    engine = create_async_engine(
        "postgresql+asyncpg://catalyst:secret@localhost/catalyst?sslmode=disable"
    )
    assert engine._postgres is True
    assert engine._echo is False
    import asyncio

    asyncio.run(engine.dispose())


@pytest.mark.asyncio
async def test_postgres_named_text_select_dml_and_transactions() -> None:
    if not POSTGRES_URL:
        pytest.skip("CATALYST_POSTGRES_URL is not set")

    engine = create_async_engine(POSTGRES_URL)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("CREATE TEMP TABLE catalyst_driver_slice (value BIGINT, label TEXT)")
            )
            await connection.commit()

            inserted = await connection.execute(
                text("INSERT INTO catalyst_driver_slice VALUES (:value, :label)"),
                {"value": 41, "label": "safe :not_a_bind"},
            )
            assert inserted.rowcount == 1
            await connection.commit()

            selected = await connection.execute(
                text("SELECT value, label FROM catalyst_driver_slice WHERE value = :value"),
                {"value": 41},
            )
            assert selected.keys == ("value", "label")
            assert selected.fetchall() == [(41, "safe :not_a_bind")]
            await connection.commit()

            await connection.begin()
            await connection.execute(
                text("INSERT INTO catalyst_driver_slice VALUES (:value, :label)"),
                {"value": 99, "label": "rollback"},
            )
            await connection.rollback()

            remaining = await connection.execute(
                text("SELECT value FROM catalyst_driver_slice ORDER BY value")
            )
            assert remaining.fetchall() == [(41,)]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_driver_does_not_leak_connection_url_on_failure() -> None:
    if not POSTGRES_URL:
        pytest.skip("CATALYST_POSTGRES_URL is not set")
    engine = create_async_engine(
        "postgresql://catalyst:secret@127.0.0.1:1/catalyst?sslmode=disable"
    )
    try:
        with pytest.raises(Exception) as raised:
            await engine.connect()
        assert "secret" not in str(raised.value)
        assert "127.0.0.1:1" not in str(raised.value)
    finally:
        await engine.dispose()
