"""Public text() construct and named-bind parity with SQLAlchemy."""

import pytest
from catalyst import (
    ArgumentError,
    ProgrammingError,
    SQLAlchemyError,
    StatementError,
    text,
)
from catalyst import create_engine as catalyst_create_engine
from sqlalchemy import create_engine
from sqlalchemy import text as sqlalchemy_text
from sqlalchemy.exc import ArgumentError as SAArgumentError
from sqlalchemy.exc import ProgrammingError as SAProgrammingError
from sqlalchemy.exc import StatementError as SAStatementError


def test_text_construct_has_value_equality_and_hash() -> None:
    clause = text("SELECT :value")
    assert clause.text == "SELECT :value"
    assert clause == text("SELECT :value")
    assert hash(clause) == hash(text("SELECT :value"))
    assert repr(clause) == "TextClause('SELECT :value')"


def test_named_binds_escaped_colons_and_duplicate_names_match_sqlalchemy() -> None:
    sql = "SELECT :value AS first, :value AS second, '\\:literal' AS literal"
    with catalyst_create_engine(":memory:").connect() as connection:
        actual = connection.execute(text(sql), {"value": 23}).fetchone()
    with create_engine("sqlite://").connect() as connection:
        expected = connection.execute(sqlalchemy_text(sql), {"value": 23}).fetchone()
    assert actual == expected == (23, 23, ":literal")


@pytest.mark.parametrize("placeholder", ["@value", "$value"])
def test_sqlite_alternate_named_bind_prefixes_execute(placeholder: str) -> None:
    with catalyst_create_engine(":memory:").connect() as connection:
        assert connection.execute(text(f"SELECT {placeholder}"), {"value": 12}).fetchone() == (12,)


def test_missing_named_bind_uses_statement_error_like_sqlalchemy() -> None:
    with catalyst_create_engine(":memory:").connect() as connection:
        with pytest.raises(StatementError) as catalyst_error:
            connection.execute(text("SELECT :required"), {})
    with create_engine("sqlite://").connect() as connection:
        with pytest.raises(SAStatementError) as sqlalchemy_error:
            connection.execute(sqlalchemy_text("SELECT :required"), {})
    assert type(catalyst_error.value).__name__ == type(sqlalchemy_error.value).__name__
    # Catalyst's StatementError is a bare class without SA's .orig/.params/.statement
    # attributes (documented parity gap); the hierarchy base is still asserted here.
    assert isinstance(catalyst_error.value, SQLAlchemyError)


def test_text_rejects_positional_list_like_sqlalchemy() -> None:
    """SQLAlchemy raises ArgumentError when a list is executed against text()."""
    with catalyst_create_engine(":memory:").connect() as connection:
        with pytest.raises(ArgumentError):
            connection.execute(text("SELECT :value"), [1])
    with create_engine("sqlite://").connect() as connection:
        with pytest.raises(SAArgumentError):
            connection.execute(sqlalchemy_text("SELECT :value"), [1])


def test_raw_string_execution_does_not_unescape_literal_colons() -> None:
    """The text()-scoped ``\\:`` escape must not mutate raw positional SQL."""
    with catalyst_create_engine(":memory:").connect() as connection:
        assert connection.execute("SELECT 'a\\:b'").fetchone() == ("a\\:b",)


def test_non_string_mapping_keys_use_statement_error_like_sqlalchemy() -> None:
    with catalyst_create_engine(":memory:").connect() as connection:
        with pytest.raises(StatementError):
            connection.execute(text("SELECT :value"), {1: "a"})
    with create_engine("sqlite://").connect() as connection:
        with pytest.raises(SAStatementError):
            connection.execute(sqlalchemy_text("SELECT :value"), {1: "a"})


def test_numbered_placeholders_with_mapping_use_programming_error() -> None:
    """SQLite ``?NNN`` placeholders cannot take named binds; SA's driver errors too."""
    with catalyst_create_engine(":memory:").connect() as connection:
        with pytest.raises(ProgrammingError) as catalyst_error:
            connection.execute(text("SELECT ?1"), {"1": 9})
    with create_engine("sqlite://").connect() as connection:
        with pytest.raises(SAProgrammingError):
            connection.execute(sqlalchemy_text("SELECT ?1"), {"1": 9})
    assert isinstance(catalyst_error.value, SQLAlchemyError)


def test_extra_named_keys_are_ignored_like_sqlalchemy() -> None:
    with catalyst_create_engine(":memory:").connect() as connection:
        actual = connection.execute(text("SELECT :value"), {"value": 5, "extra": 8})
        assert actual.fetchone() == (5,)
    with create_engine("sqlite://").connect() as connection:
        expected = connection.execute(sqlalchemy_text("SELECT :value"), {"value": 5, "extra": 8})
        assert expected.fetchone() == (5,)


def test_positional_string_sql_and_text_result_metadata_match_sqlalchemy() -> None:
    with catalyst_create_engine(":memory:").connect() as connection:
        connection.execute("CREATE TABLE values_table (value INTEGER)")
        inserted = connection.execute(
            text("INSERT INTO values_table VALUES (:value)"), {"value": 31}
        )
        selected = connection.execute(
            "SELECT value AS answer FROM values_table WHERE value = ?", [31]
        )
        actual = (inserted.rowcount, selected.keys, selected.rowcount, selected.fetchall())
    with create_engine("sqlite://").connect() as connection:
        connection.execute(sqlalchemy_text("CREATE TABLE values_table (value INTEGER)"))
        inserted = connection.execute(
            sqlalchemy_text("INSERT INTO values_table VALUES (:value)"), {"value": 31}
        )
        selected = connection.execute(
            sqlalchemy_text("SELECT value AS answer FROM values_table WHERE value = :value"),
            {"value": 31},
        )
        expected = (
            inserted.rowcount,
            tuple(selected.keys()),
            selected.rowcount,
            selected.fetchall(),
        )
    assert actual == expected == (1, ("answer",), -1, [(31,)])
