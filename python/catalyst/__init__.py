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
    ArgumentError,
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
from .engine import Connection, CursorResult, Engine, TextClause, create_engine, text

__all__ = [
    "CatalystError",
    "ArgumentError",
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
    "TextClause",
    "text",
    "create_engine",
    "NativeEngine",
    "NativeConnection",
    "NativeResult",
]
