"""Behavior and error parity tests for the foundation SQLite helper."""

import tempfile
from pathlib import Path

import pytest
from catalyst import (
    IntegrityError,
    ProgrammingError,
    sqlite_text,
)
from catalyst import (
    create_engine as catalyst_create_engine,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError as SAIntegrityError
from sqlalchemy.exc import ProgrammingError as SAProgrammingError


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
        with engine.connect() as connection:
            connection.execute("INSERT INTO items VALUES (?)", [9])
            assert connection.execute("SELECT value FROM items").fetchone() == (9,)

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "items.sqlite"
        engine = catalyst_create_engine(path)
        with engine.connect() as connection:
            connection.execute("CREATE TABLE items (value INTEGER)")
            connection.execute("INSERT INTO items VALUES (?)", [11])
        engine.dispose()
        reopened = catalyst_create_engine(path)
        with reopened.connect() as connection:
            assert connection.execute("SELECT value FROM items").fetchone() == (11,)
        reopened.dispose()


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
