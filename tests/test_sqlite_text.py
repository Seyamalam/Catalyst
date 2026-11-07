"""Behavior and error parity tests for the foundation SQLite helper."""

import tempfile
from pathlib import Path

import pytest
from catalyst import (
    IntegrityError,
    ProgrammingError,
    SQLAlchemyError,
    sqlite_text,
)
from catalyst import (
    TimeoutError as CatalystTimeoutError,
)
from catalyst import (
    create_engine as catalyst_create_engine,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import ProgrammingError as SAProgrammingError
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.pool import QueuePool


def test_parameterized_select_matches_sqlalchemy() -> None:
    """Bound values are returned in a single batch, without SQL interpolation."""
    catalyst_rows = sqlite_text("SELECT ?, ?, ?", [42, "catalyst", None])
    with create_engine("sqlite://").connect() as connection:
        sqlalchemy_rows = connection.execute(
            text("SELECT :integer, :string, :nullable"),
            {"integer": 42, "string": "catalyst", "nullable": None},
        ).all()

    assert catalyst_rows == [list(sqlalchemy_rows[0])]


def test_binding_mismatch_raises_matching_programming_error() -> None:
    """Incorrect positional parameter counts map to the matching error class."""
    with pytest.raises(ProgrammingError) as catalyst_error:
        sqlite_text("SELECT ?", [])
    with pytest.raises(SAProgrammingError):
        with create_engine("sqlite://").connect() as connection:
            connection.exec_driver_sql("SELECT ?", ())

    assert type(catalyst_error.value).__name__ == SAProgrammingError.__name__


def test_unsupported_parameter_type_is_rejected_before_execution() -> None:
    """Only SQLite's supported scalar types cross the native boundary."""
    with pytest.raises(TypeError, match="parameters must be"):
        sqlite_text("SELECT ?", [object()])


def test_engine_database_lifecycle_matches_memory_and_file_sqlite() -> None:
    """Engine connections share state and dispose cleanly for memory and files."""
    with catalyst_create_engine(":memory:") as engine:
        with engine.connect() as connection:
            connection.execute("CREATE TABLE items (value INTEGER)")
            connection.commit()
        with engine.connect() as connection:
            connection.execute("INSERT INTO items VALUES (?)", [9])
            connection.commit()
            assert connection.execute("SELECT value FROM items").fetchone() == (9,)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "items.sqlite"
        engine = catalyst_create_engine(path)
        with engine.connect() as connection:
            connection.execute("CREATE TABLE items (value INTEGER)")
            connection.execute("INSERT INTO items VALUES (?)", [11])
            connection.commit()
        engine.dispose()
        reopened = catalyst_create_engine(path)
        with reopened.connect() as connection:
            assert connection.execute("SELECT value FROM items").fetchone() == (11,)
        reopened.dispose()


def test_file_engine_checks_out_independent_connections() -> None:
    """Concurrent file checkouts use separate connections and both remain usable."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "pool.sqlite"
        engine = catalyst_create_engine(path)
        with engine.connect() as setup:
            setup.execute("CREATE TABLE items (value INTEGER)")
            setup.commit()

        first = engine.connect()
        second = engine.connect()
        first.execute("CREATE TEMP TABLE local_state (value INTEGER)")
        second.execute("CREATE TEMP TABLE local_state (value INTEGER)")
        first.execute("INSERT INTO local_state VALUES (?)", [10])
        second.execute("INSERT INTO local_state VALUES (?)", [20])
        first.execute("INSERT INTO items VALUES (?)", [1])
        first.commit()
        second.execute("INSERT INTO items VALUES (?)", [2])
        second.commit()
        first.commit()
        second.commit()
        assert first.execute("SELECT value FROM local_state").fetchone() == (10,)
        assert second.execute("SELECT value FROM local_state").fetchone() == (20,)
        assert first.execute("SELECT value FROM items ORDER BY value").fetchall() == [(1,), (2,)]
        assert second.execute("SELECT value FROM items ORDER BY value").fetchall() == [(1,), (2,)]
        first.close()
        second.close()
        engine.dispose()


def test_memory_engine_shares_schema_and_data_between_connections() -> None:
    """The in-memory pool is a singleton so each checkout sees the same database."""
    engine = catalyst_create_engine(":memory:")
    first = engine.connect()
    first.execute("CREATE TABLE items (value INTEGER)")
    first.execute("INSERT INTO items VALUES (?)", [17])
    first.commit()
    second = engine.connect()
    assert second.execute("SELECT value FROM items").fetchone() == (17,)
    first.close()
    second.close()
    engine.dispose()


def test_close_returns_connection_to_pool() -> None:
    """Closing a file checkout allows a later connection to reuse the slot."""
    with tempfile.TemporaryDirectory() as directory:
        engine = catalyst_create_engine(Path(directory) / "pool_return.sqlite")
        with engine.connect() as first:
            first.execute("CREATE TABLE items (value INTEGER)")
            first.execute("CREATE TEMP TABLE marker (value INTEGER)")
            first.commit()
        # A TEMP table is per-connection, so it surviving here proves the same
        # physical connection was reused rather than a new one being opened.
        with engine.connect() as returned:
            assert returned.execute("SELECT COUNT(*) FROM marker").fetchone() == (0,)
        engine.dispose()


def test_exhausted_pool_raises_sqlalchemy_timeout_class() -> None:
    """Catalyst and SQLAlchemy report pool exhaustion with matching class names."""
    with tempfile.TemporaryDirectory() as directory:
        engine = catalyst_create_engine(Path(directory) / "exhausted.sqlite")
        connections = [engine.connect() for _ in range(5)]
        try:
            with pytest.raises(CatalystTimeoutError) as catalyst_error:
                engine.connect()
            sqlalchemy_engine = create_engine(
                "sqlite://",
                poolclass=QueuePool,
                pool_size=1,
                max_overflow=0,
                pool_timeout=0.01,
            )
            sqlalchemy_connections = [sqlalchemy_engine.connect()]
            try:
                with pytest.raises(SATimeoutError) as sqlalchemy_error:
                    sqlalchemy_engine.connect()
                assert type(catalyst_error.value).__name__ == type(sqlalchemy_error.value).__name__
                assert isinstance(catalyst_error.value, SQLAlchemyError)
            finally:
                sqlalchemy_connections[0].close()
                sqlalchemy_engine.dispose()
        finally:
            for connection in connections:
                connection.close()
            engine.dispose()


def test_connection_and_engine_reject_use_after_close() -> None:
    """Closed logical handles and disposed engines fail deterministically."""
    engine = catalyst_create_engine(":memory:")
    connection = engine.connect()
    connection.close()
    with pytest.raises(RuntimeError, match="Connection is closed"):
        connection.execute("SELECT 1")
    engine.dispose()
    with pytest.raises(RuntimeError, match="Engine is disposed"):
        engine.connect()


def test_result_fetch_methods_track_position_and_column_names() -> None:
    """Buffered result navigation and labels are stable across fetch methods."""
    engine = catalyst_create_engine(":memory:")
    with engine.connect() as connection:
        result = connection.execute("SELECT 1 AS one UNION ALL SELECT 2 AS one")
        assert result.keys == ("one",)
        assert result.fetchone() == (1,)
        assert result.fetchall() == [(2,)]
        assert result.fetchone() is None
    engine.dispose()


def test_integrity_error_maps_to_matching_exception() -> None:
    """SQLite constraint errors map to SQLAlchemy's IntegrityError class name."""
    import uuid

    table_name = f"test_{uuid.uuid4().hex}"
    sqlite_text(f"CREATE TABLE {table_name} (value INTEGER UNIQUE)", [])
    sqlite_text(f"INSERT INTO {table_name} VALUES (1)", [])
    with pytest.raises(IntegrityError) as catalyst_error:
        sqlite_text(f"INSERT INTO {table_name} VALUES (1)", [])

    with pytest.raises(SAIntegrityError):
        with create_engine("sqlite://").begin() as connection:
            connection.execute(text("CREATE TABLE test (value INTEGER UNIQUE)"))
            connection.execute(text("INSERT INTO test VALUES (1)"))
            connection.execute(text("INSERT INTO test VALUES (1)"))

    assert type(catalyst_error.value).__name__ == SAIntegrityError.__name__
