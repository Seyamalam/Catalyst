"""Type information for Catalyst's native extension."""

from typing import final

__all__ = [
    "SQLAlchemyError",
    "CatalystError",
    "StatementError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "ProgrammingError",
    "OperationalError",
    "TimeoutError",
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

    def __new__(cls, path: str) -> NativeEngine: ...
    def connect(self) -> NativeConnection: ...
    def close(self) -> None: ...

@final
class NativeConnection:
    """Native SQLite connection checked out from an engine pool."""

    def execute(self, sql: str, parameters: list[object]) -> NativeResult: ...
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
