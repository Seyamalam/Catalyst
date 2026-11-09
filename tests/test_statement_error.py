"""StatementError shape parity: orig/statement/params attributes and wrapping.

SQLAlchemy defers bind processing to execution and wraps failures in
``sqlalchemy.exc.StatementError`` carrying ``orig`` (the original exception),
``statement`` (the SQL text), and ``params``. Driver-level failures keep their
concrete DBAPIError subclass (for example ``ProgrammingError`` for unbindable
types). These tests prove Catalyst's wrapped errors match that shape.
"""

from __future__ import annotations

import pytest
from catalyst import (
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    ProgrammingError,
    StatementError,
    String,
    Table,
    create_async_engine,
    create_engine,
    insert,
    select,
    text,
    update,
)


def _table() -> Table:
    return Table(
        "t",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("flag", Boolean),
        Column("when", DateTime),
        Column("name", String),
    )


def test_compile_does_not_process_literal_binds() -> None:
    """SQLAlchemy's ``compile()`` succeeds; the failure surfaces at execute."""
    table = _table()
    compiled = insert(table).values(id=1, flag="not-a-bool").compile()
    assert compiled.string == "INSERT INTO t (id, flag) VALUES (?, ?)"
    assert isinstance(compiled.bind_error, TypeError)


def test_bad_literal_bind_raises_wrapped_statement_error() -> None:
    table = _table()
    engine = create_engine(":memory:")
    table.metadata.create_all(engine)
    with engine.connect() as connection:
        with pytest.raises(StatementError) as error:
            connection.execute(insert(table).values(id=1, flag="not-a-bool"))
        exc = error.value
        assert isinstance(exc.orig, TypeError)
        assert "Not a boolean value" in str(exc.orig)
        assert exc.statement == "INSERT INTO t (id, flag) VALUES (?, ?)"
        assert exc.params == ()
        assert str(exc).startswith(
            "(builtins.TypeError) Not a boolean value: 'not-a-bool'\n"
            "[SQL: INSERT INTO t (id, flag) VALUES (?, ?)]"
        )
    engine.dispose()


def test_bad_datetime_bind_matches_sqlalchemy_shape() -> None:
    table = _table()
    engine = create_engine(":memory:")
    table.metadata.create_all(engine)
    with engine.connect() as connection:
        with pytest.raises(StatementError) as error:
            connection.execute(insert(table).values(id=1, when="not-a-datetime"))
        exc = error.value
        assert isinstance(exc.orig, TypeError)
        assert "datetime and date objects" in str(exc.orig)
        assert exc.statement == 'INSERT INTO t (id, "when") VALUES (?, ?)'
    engine.dispose()


def test_bad_batch_bind_carries_params() -> None:
    table = _table()
    engine = create_engine(":memory:")
    table.metadata.create_all(engine)
    rows = [{"id": 1, "flag": "not-a-bool"}]
    with engine.connect() as connection:
        with pytest.raises(StatementError) as error:
            connection.execute(insert(table), rows)
        exc = error.value
        assert isinstance(exc.orig, TypeError)
        assert exc.statement == "INSERT INTO t (id, flag) VALUES (?, ?)"
        assert exc.params == rows
        assert "[parameters:" in str(exc)
    engine.dispose()


def test_unbindable_type_is_programming_error_with_context() -> None:
    """SA's sqlite driver reports unbindable types as ProgrammingError."""
    engine = create_engine(":memory:")

    class Widget:
        pass

    widget = Widget()
    with engine.connect() as connection:
        with pytest.raises(ProgrammingError) as error:
            connection.execute("SELECT ?", [widget])
        exc = error.value
        assert isinstance(exc, StatementError)
        assert isinstance(exc.orig, ProgrammingError)
        assert exc.statement == "SELECT ?"
        assert exc.params == [widget]
    engine.dispose()


def test_update_and_delete_literal_binds_wrap_too() -> None:
    table = _table()
    engine = create_engine(":memory:")
    table.metadata.create_all(engine)
    with engine.connect() as connection:
        for statement in (
            update(table).where(table.c.id == 1).values(flag="bad"),
            insert(table).values(id=1, flag="bad"),
        ):
            with pytest.raises(StatementError) as error:
                connection.execute(statement)
            assert error.value.statement == statement.compile().string
    engine.dispose()


def test_lax_comparison_binds_do_not_raise() -> None:
    """Comparison literals bind laxly like SQLAlchemy; no error is produced."""
    table = _table()
    engine = create_engine(":memory:")
    table.metadata.create_all(engine)
    with engine.connect() as connection:
        rows = connection.execute(select(table).where(table.c.flag == "not-a-bool")).all()
        assert rows == []
    engine.dispose()


async def test_async_bad_bind_wraps_identically() -> None:
    table = _table()
    engine = create_async_engine(":memory:")
    async with engine.begin() as connection:
        await connection.execute(table.compile_ddl())
    async with engine.connect() as connection:
        with pytest.raises(StatementError) as error:
            await connection.execute(insert(table).values(id=1, flag="bad"))
        exc = error.value
        assert isinstance(exc.orig, TypeError)
        assert exc.statement == "INSERT INTO t (id, flag) VALUES (?, ?)"
    await engine.dispose()


def test_text_bind_failure_wraps() -> None:
    engine = create_engine(":memory:")

    class Widget:
        pass

    widget = Widget()
    with engine.connect() as connection:
        with pytest.raises(ProgrammingError) as error:
            connection.execute(text("SELECT :value"), {"value": widget})
        assert error.value.statement == "SELECT :value"
        assert error.value.params == {"value": widget}
    engine.dispose()
