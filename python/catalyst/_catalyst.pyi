"""Type information for Catalyst's native extension."""

from collections.abc import Awaitable
from typing import final

__all__ = [
    "SQLAlchemyError",
    "CatalystError",
    "ArgumentError",
    "NoSuchModuleError",
    "StatementError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "ProgrammingError",
    "OperationalError",
    "TimeoutError",
    "InvalidRequestError",
    "sqlite_text",
    "compile_expression",
    "NativeEngine",
    "NativeConnection",
    "NativeResult",
    "NativePgEngine",
    "NativePgConnection",
]

class SQLAlchemyError(Exception):
    """Root exception class for SQLAlchemy-compatible Catalyst errors."""

CatalystError = SQLAlchemyError

def sqlite_text(sql: str, parameters: list[object]) -> list[list[object]]:
    """Execute parameterized SQL on Catalyst's persistent process-local SQLite database."""

def compile_expression(
    value: object,
) -> tuple[
    str,
    dict[str, object],
    str,
    list[object],
    list[str | None],
    list[str],
    list[str | None],
]: ...

@final
class NativeEngine:
    """Native SQLite engine used by the public Python Engine wrapper."""

    def __new__(
        cls,
        path: str,
        pool_size: int = ...,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        sqlite_timeout: float = 5.0,
    ) -> NativeEngine: ...
    def connect(self) -> NativeConnection: ...
    def close(self) -> None: ...
    def async_connect(self) -> Awaitable[NativeConnection]: ...
    def async_close(self) -> Awaitable[None]: ...

@final
class NativeConnection:
    """Native SQLite connection checked out from an engine pool."""

    def execute(
        self,
        sql: str,
        parameters: list[object] | dict[str, object],
        text_mode: bool,
        statement_kind: str | None = ...,
        type_metadata: tuple[list[str | None], list[str], list[str | None]] | None = ...,
    ) -> NativeResult: ...
    def async_execute(
        self,
        sql: str,
        parameters: list[object] | dict[str, object],
        text_mode: bool,
        statement_kind: str | None = ...,
        type_metadata: tuple[list[str | None], list[str], list[str | None]] | None = ...,
    ) -> Awaitable[NativeResult]: ...
    def async_transaction(self, operation: str) -> Awaitable[None]: ...
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def begin_savepoint(self) -> str: ...
    def release_savepoint(self, name: str) -> None: ...
    def rollback_savepoint(self, name: str) -> None: ...
    def has_table(self, name: str) -> bool: ...
    def async_has_table(self, name: str) -> Awaitable[bool]: ...
    def is_active(self) -> bool: ...
    def close(self) -> None: ...

@final
class NativePgEngine:
    """Native async PostgreSQL engine."""
    def __new__(cls, url: str) -> NativePgEngine: ...
    def async_connect(self) -> Awaitable[NativePgConnection]: ...
    def async_close(self) -> Awaitable[None]: ...

@final
class NativePgConnection:
    """Native asynchronous PostgreSQL connection."""
    def async_execute(
        self,
        sql: str,
        parameters: list[object] | dict[str, object],
        text_mode: bool,
        statement_kind: str | None = ...,
        type_metadata: tuple[list[str | None], list[str], list[str | None]] | None = ...,
    ) -> Awaitable[NativeResult]: ...
    def async_transaction(self, operation: str) -> Awaitable[None]: ...
    def async_close(self) -> Awaitable[None]: ...

@final
class NativeResult:
    """Buffered results from native execution."""

    @property
    def keys(self) -> list[str]: ...
    @property
    def rowcount(self) -> int: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def fetchone(self) -> tuple[object, ...] | None: ...

class ArgumentError(CatalystError):
    """Invalid arguments to a database API call."""

class NoSuchModuleError(ArgumentError):
    """Dialect plugin could not be loaded."""

class StatementError(CatalystError):
    """Base class for SQL statement errors."""

class DBAPIError(StatementError):
    """Base class for database driver errors."""

class DatabaseError(DBAPIError):
    """Base class for database-related errors."""

class IntegrityError(DatabaseError):
    """Database constraint violation."""

class ProgrammingError(DatabaseError):
    """Invalid database statement."""

class OperationalError(DatabaseError):
    """Database operation failure, such as a connection error."""

class TimeoutError(CatalystError):
    """Connection pool checkout timed out or was exhausted."""

class InvalidRequestError(CatalystError):
    """Invalid connection or transaction state."""
