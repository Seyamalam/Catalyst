"""Catalyst's public Python API.

The native implementation is provided by the compiled ``_catalyst`` extension.
This foundation release includes only basic positional SQLite statement execution
and the initial database error hierarchy.

Examples:
    >>> from catalyst import CatalystError, create_engine
    >>> issubclass(CatalystError, Exception)
    True
    >>> engine = create_engine(":memory:")
    >>> with engine.connect() as connection:
    ...     connection.execute("SELECT ?", [42]).fetchone()
    (42,)
    >>> engine.dispose()
"""

from ._catalyst import (
    CatalystError,
    DatabaseError,
    DBAPIError,
    IntegrityError,
    NativeConnection,
    NativeEngine,
    NativeResult,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
    StatementError,
    TimeoutError,
    sqlite_text,
)
from .engine import Connection, CursorResult, Engine, create_engine

__all__ = [
    "CatalystError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "OperationalError",
    "ProgrammingError",
    "StatementError",
    "TimeoutError",
    "SQLAlchemyError",
    "sqlite_text",
    "Engine",
    "Connection",
    "CursorResult",
    "create_engine",
    "NativeEngine",
    "NativeConnection",
    "NativeResult",
]
