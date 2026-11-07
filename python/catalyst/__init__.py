"""Catalyst's public Python API.

The native implementation is provided by the compiled ``_catalyst`` extension.

Examples:
    >>> from catalyst import CatalystError
    >>> issubclass(CatalystError, Exception)
    True
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
]
