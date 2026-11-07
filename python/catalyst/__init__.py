"""Catalyst's public Python API.

The native implementation is provided by the compiled ``_catalyst`` extension.
This foundation release includes only basic positional SQLite statement execution
and the initial database error hierarchy.

Examples:
    >>> from catalyst import CatalystError, sqlite_text
    >>> issubclass(CatalystError, Exception)
    True
    >>> sqlite_text("SELECT ?", [42])
    [[42]]
"""

from ._catalyst import (
    CatalystError,
    DatabaseError,
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
    StatementError,
    sqlite_text,
)

__all__ = [
    "CatalystError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "OperationalError",
    "ProgrammingError",
    "StatementError",
    "SQLAlchemyError",
    "sqlite_text",
]
