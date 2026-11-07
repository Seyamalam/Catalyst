"""Type information for Catalyst's native extension."""

__all__ = [
    "SQLAlchemyError",
    "CatalystError",
    "StatementError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "ProgrammingError",
    "OperationalError",
    "sqlite_text",
]

class SQLAlchemyError(Exception):
    """Root exception class for SQLAlchemy-compatible Catalyst errors."""

CatalystError = SQLAlchemyError

def sqlite_text(sql: str, parameters: list[object]) -> list[list[object]]:
    """Execute parameterized SQL on Catalyst's persistent process-local SQLite database."""

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
