"""Behavior and error parity tests for the foundation SQLite helper."""

import pytest
from catalyst import IntegrityError, ProgrammingError, sqlite_text
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
            connection.execute(text("SELECT ?"), [])

    assert type(catalyst_error.value).__name__ == SAProgrammingError.__name__


def test_unsupported_parameter_type_is_rejected_before_execution() -> None:
    """Only SQLite's supported scalar types cross the native boundary."""
    with pytest.raises(TypeError, match="parameters must be"):
        sqlite_text("SELECT ?", [object()])


def test_integrity_error_maps_to_matching_exception() -> None:
    """SQLite constraint errors map to SQLAlchemy's IntegrityError class name."""
    sqlite_text("CREATE TABLE test (value INTEGER UNIQUE)", [])
    sqlite_text("INSERT INTO test VALUES (1)", [])
    with pytest.raises(IntegrityError) as catalyst_error:
        sqlite_text("INSERT INTO test VALUES (1)", [])

    with pytest.raises(SAIntegrityError):
        with create_engine("sqlite://").begin() as connection:
            connection.execute(text("CREATE TABLE test (value INTEGER UNIQUE)"))
            connection.execute(text("INSERT INTO test VALUES (1)"))
            connection.execute(text("INSERT INTO test VALUES (1)"))

    assert type(catalyst_error.value).__name__ == SAIntegrityError.__name__
