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
    InvalidRequestError,
    NativeConnection,
    NativeEngine,
    NativeResult,
    NoSuchModuleError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
    StatementError,
    TimeoutError,
    sqlite_text,
)
from .engine import (
    AsyncConnection,
    AsyncEngine,
    AsyncSavepointTransaction,
    Connection,
    CursorResult,
    Engine,
    RootTransaction,
    SavepointTransaction,
    TextClause,
    create_async_engine,
    create_engine,
    text,
)
from .expression import (
    Boolean,
    Column,
    Compiled,
    Date,
    DateTime,
    Expression,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    and_,
    asc,
    delete,
    desc,
    func,
    insert,
    not_,
    or_,
    select,
    update,
)

__all__ = [
    "CatalystError",
    "ArgumentError",
    "NoSuchModuleError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "OperationalError",
    "ProgrammingError",
    "StatementError",
    "TimeoutError",
    "InvalidRequestError",
    "SQLAlchemyError",
    "sqlite_text",
    "Engine",
    "Connection",
    "AsyncEngine",
    "AsyncConnection",
    "AsyncSavepointTransaction",
    "RootTransaction",
    "SavepointTransaction",
    "CursorResult",
    "TextClause",
    "text",
    "create_engine",
    "create_async_engine",
    "MetaData",
    "Table",
    "Column",
    "Integer",
    "LargeBinary",
    "String",
    "Boolean",
    "Text",
    "Float",
    "DateTime",
    "Date",
    "Expression",
    "Compiled",
    "select",
    "insert",
    "update",
    "delete",
    "and_",
    "or_",
    "not_",
    "asc",
    "desc",
    "func",
    "NativeEngine",
    "NativeConnection",
    "NativeResult",
]
