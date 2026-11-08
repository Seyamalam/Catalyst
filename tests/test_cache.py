"""Deterministic plan/prepared-statement cache behavior."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from catalyst import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_async_engine,
    create_engine,
    insert,
    select,
)
from catalyst.expression import update

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def tables() -> Generator[tuple[Table, Table], None, None]:
    def build() -> Table:
        metadata = MetaData()
        return Table(
            "items",
            metadata,
            Column("id", Integer),
            Column("name", String),
            Column("score", Integer),
        )

    yield build(), build()


def _seed(engine: object, table: Table) -> None:
    table.metadata.create_all(engine)  # type: ignore[attr-defined]
    with engine.connect() as connection:  # type: ignore[attr-defined]
        connection.execute(insert(table).values(id=1, name="a", score=10))
        connection.execute(insert(table).values(id=2, name="b", score=20))
        connection.commit()


def test_repeated_execution_with_different_parameters_is_correct(
    tables: tuple[Table, Table],
) -> None:
    """The cached compiled statement must rebind per execution."""
    table = tables[0]
    engine = create_engine(":memory:")
    _seed(engine, table)
    with engine.connect() as connection:
        rows = connection.execute(select(table.c.name).where(table.c.id == 1)).fetchall()
        assert rows == [("a",)]
        rows = connection.execute(select(table.c.name).where(table.c.id == 2)).fetchall()
        assert rows == [("b",)]
        rows = connection.execute(select(table.c.name).where(table.c.id == 1)).fetchall()
        assert rows == [("a",)]
    engine.dispose()


def test_in_with_varying_member_counts_recompile_correctly(
    tables: tuple[Table, Table],
) -> None:
    table = tables[0]
    engine = create_engine(":memory:")
    _seed(engine, table)
    with engine.connect() as connection:
        rows = connection.execute(select(table.c.id).where(table.c.id.in_([1]))).fetchall()
        assert rows == [(1,)]
        assert connection.execute(select(table.c.id).where(table.c.id.in_([1, 2]))).fetchall() == [
            (1,),
            (2,),
        ]
        rows = connection.execute(select(table.c.id).where(table.c.id.in_([2]))).fetchall()
        assert rows == [(2,)]
    engine.dispose()


def test_dml_rowcount_is_correct_across_reused_statements(
    tables: tuple[Table, Table],
) -> None:
    table = tables[0]
    engine = create_engine(":memory:")
    _seed(engine, table)
    statement = update(table).values(score=99).where(table.c.score == 10)
    with engine.connect() as connection:
        assert connection.execute(statement).rowcount == 1
        assert connection.execute(statement).rowcount == 0
        assert connection.execute(statement).rowcount == 0
    engine.dispose()


def test_cached_statements_inside_transactions_and_savepoints(
    tables: tuple[Table, Table],
) -> None:
    table = tables[0]
    engine = create_engine(":memory:")
    _seed(engine, table)
    with engine.connect() as connection:
        with connection.begin():
            connection.execute(update(table).values(score=11).where(table.c.id == 1))
            nested = connection.begin_nested()
            connection.execute(update(table).values(score=12).where(table.c.id == 2))
            nested.rollback()
            score = connection.execute(select(table.c.score).where(table.c.id == 2)).fetchone()
            assert score == (20,)
        assert connection.execute(select(table.c.score).where(table.c.id == 1)).fetchone() == (11,)
    engine.dispose()


def test_cache_works_on_file_engines_and_raw_text() -> None:
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine(Path(directory) / "cache.sqlite")
        with engine.connect() as connection:
            connection.execute("CREATE TABLE items (value INTEGER)")
            connection.commit()
            for value in range(3):
                connection.execute("INSERT INTO items VALUES (?)", [value])
            connection.commit()
            assert connection.execute("SELECT COUNT(*) FROM items").fetchone() == (3,)
        engine.dispose()


def test_equal_statements_share_compilation_and_are_immutable(
    tables: tuple[Table, Table],
) -> None:
    table = tables[0]
    first = select(table.c.id).where(table.c.id == 1)
    second = select(table.c.id).where(table.c.id == 1)
    # Equal statements produce identical SQL and parameters (shared cache entry).
    assert first.compile().string == second.compile().string
    assert first.compile().params == second.compile().params == {"id_1": 1}
    # A different bind value has the same SQL text but different parameters.
    different = select(table.c.id).where(table.c.id == 2)
    assert different.compile().string == first.compile().string
    assert different.compile().params == {"id_1": 2}
    with pytest.raises(FrozenInstanceError):
        first.row_limit = 5  # type: ignore[misc]


def test_unhashable_bind_values_bypass_cache_and_execute(
    tables: tuple[Table, Table],
) -> None:
    table = tables[0]
    engine = create_engine(":memory:")
    _seed(engine, table)

    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

    with engine.connect() as connection:
        with pytest.raises(TypeError):
            connection.execute(select(table.c.id).where(table.c.name == Unhashable()))
    engine.dispose()


def test_async_path_shares_the_cache(tables: tuple[Table, Table]) -> None:
    table = tables[1]
    engine = create_async_engine(":memory:")

    async def main() -> None:
        await table.metadata.create_all(engine)  # type: ignore[attr-defined]
        async with engine.connect() as connection:
            await connection.execute(insert(table).values(id=1, name="a", score=5))
            await connection.commit()
            for _ in range(2):
                result = await connection.execute(
                    select(table.c.name).where(table.c.id == 1)
                )
                assert result.fetchall() == [("a",)]
        await engine.dispose()

    asyncio.run(main())
