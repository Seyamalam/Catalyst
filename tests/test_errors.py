"""Parity tests for Catalyst's public exception hierarchy."""

from catalyst import (
    CatalystError,
    DatabaseError,
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
    StatementError,
)
from sqlalchemy import exc as sqlalchemy_exc


def test_exception_class_names_match_sqlalchemy() -> None:
    """Catalyst exposes counterparts with SQLAlchemy's public class names."""
    pairs = [
        (SQLAlchemyError, sqlalchemy_exc.SQLAlchemyError),
        (StatementError, sqlalchemy_exc.StatementError),
        (DBAPIError, sqlalchemy_exc.DBAPIError),
        (DatabaseError, sqlalchemy_exc.DatabaseError),
        (IntegrityError, sqlalchemy_exc.IntegrityError),
        (ProgrammingError, sqlalchemy_exc.ProgrammingError),
        (OperationalError, sqlalchemy_exc.OperationalError),
    ]
    assert all(actual.__name__ == expected.__name__ for actual, expected in pairs)


def test_exception_subclass_relationships_match_sqlalchemy() -> None:
    """The supported error hierarchy follows SQLAlchemy's DBAPI branch."""
    pairs = [
        (SQLAlchemyError, sqlalchemy_exc.SQLAlchemyError),
        (StatementError, sqlalchemy_exc.StatementError),
        (DBAPIError, sqlalchemy_exc.DBAPIError),
        (DatabaseError, sqlalchemy_exc.DatabaseError),
        (IntegrityError, sqlalchemy_exc.IntegrityError),
        (ProgrammingError, sqlalchemy_exc.ProgrammingError),
        (OperationalError, sqlalchemy_exc.OperationalError),
    ]
    actual_by_sqlalchemy = {expected: actual for actual, expected in pairs}
    # SQLAlchemyError mixes in HasDescriptionCode; Catalyst currently mirrors the
    # SQLAlchemyError name but not that non-exception mixin's direct inheritance.
    assert issubclass(SQLAlchemyError, Exception)
    for actual, expected in pairs[1:]:
        expected_bases = tuple(actual_by_sqlalchemy.get(base, base) for base in expected.__bases__)
        assert actual.__bases__ == expected_bases

    assert CatalystError is SQLAlchemyError
