"""Type information for Catalyst's native extension."""

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
    "NativeEngine",
    "NativeConnection",
    "NativeResult",
]

class SQLAlchemyError(Exception):
    """Root exception class for SQLAlchemy-compatible Catalyst errors."""

CatalystError = SQLAlchemyError

def sqlite_text(sql: str, parameters: list[object]) -> list[list[object]]:
    """Execute parameterized SQL on Catalyst's persistent process-local SQLite database."""

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

@final
class NativeConnection:
    """Native SQLite connection checked out from an engine pool."""

    def execute(
        self, sql: str, parameters: list[object] | dict[str, object], text_mode: bool
    ) -> NativeResult: ...
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def begin_savepoint(self) -> str: ...
    def release_savepoint(self, name: str) -> None: ...
    def rollback_savepoint(self, name: str) -> None: ...
    def is_active(self) -> bool: ...
    def close(self) -> None: ...

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
