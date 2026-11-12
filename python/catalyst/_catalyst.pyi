"""Type information for Catalyst's native extension."""

from collections.abc import Awaitable, Sequence
from typing import final

__all__ = [
    "SQLAlchemyError",
    "CatalystError",
    "ArgumentError",
    "AmbiguousForeignKeysError",
    "NoSuchModuleError",
    "StatementError",
    "DBAPIError",
    "DatabaseError",
    "IntegrityError",
    "ProgrammingError",
    "OperationalError",
    "DataError",
    "TimeoutError",
    "InvalidRequestError",
    "PendingRollbackError",
    "UnmappedInstanceError",
    "StaleDataError",
    "ObjectDeletedError",
    "DetachedInstanceError",
    "sqlite_text",
    "compile_expression",
    "NativeEngine",
    "NativeConnection",
    "NativeResult",
    "NativeUow",
    "NativeMysqlEngine",
    "NativeMysqlConnection",
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
    dialect: str = ...,
) -> tuple[
    str,
    dict[str, object],
    str,
    list[object],
    list[str | None],
    list[str],
    list[str | None],
    BaseException | None,
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
    def async_begin_savepoint(self) -> Awaitable[str]: ...
    def async_release_savepoint(self, name: str) -> Awaitable[None]: ...
    def async_rollback_savepoint(self, name: str) -> Awaitable[None]: ...
    def async_is_active(self) -> Awaitable[bool]: ...
    def execute_flush(
        self, ops: list[tuple[str, list[object], list[str | None], bool]]
    ) -> list[int | None]: ...
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def begin_savepoint(self) -> str: ...
    def release_savepoint(self, name: str) -> None: ...
    def rollback_savepoint(self, name: str) -> None: ...
    def rollback_to_savepoint(self, name: str) -> None: ...
    def has_table(self, name: str) -> bool: ...
    def async_has_table(self, name: str) -> Awaitable[bool]: ...
    def is_active(self) -> bool: ...
    def close(self) -> None: ...

@final
class NativePgEngine:
    """Native async PostgreSQL engine."""
    def __new__(
        cls,
        url: str,
        pool_size: int = ...,
        max_overflow: int = ...,
        pool_timeout: float = ...,
    ) -> NativePgEngine: ...
    def async_connect(self) -> Awaitable[NativePgConnection]: ...
    def async_close(self) -> Awaitable[None]: ...

@final
class NativeMysqlEngine:
    """Native async MySQL engine."""
    def __new__(
        cls,
        url: str,
        pool_size: int = ...,
        max_overflow: int = ...,
        pool_timeout: float = ...,
    ) -> NativeMysqlEngine: ...
    def async_connect(self) -> Awaitable[NativeMysqlConnection]: ...
    def async_close(self) -> Awaitable[None]: ...

@final
class NativeMysqlConnection:
    """Native asynchronous MySQL connection."""
    def async_execute(
        self,
        sql: str,
        parameters: list[object] | dict[str, object],
        text_mode: bool,
        statement_kind: str | None = ...,
        type_metadata: tuple[list[str | None], list[str], list[str | None]] | None = ...,
    ) -> Awaitable[NativeResult]: ...
    def async_executemany(
        self,
        sql: str,
        parameters: list[object],
        text_mode: bool,
        statement_kind: str | None = ...,
        type_metadata: tuple[list[str | None], list[str], list[str | None]] | None = ...,
    ) -> Awaitable[NativeResult]: ...
    def async_has_table(self, name: str) -> Awaitable[bool]: ...
    def async_transaction(self, operation: str) -> Awaitable[None]: ...
    def async_begin_savepoint(self) -> Awaitable[str]: ...
    def async_release_savepoint(self, name: str) -> Awaitable[None]: ...
    def async_rollback_savepoint(self, name: str) -> Awaitable[None]: ...
    def async_is_active(self) -> Awaitable[bool]: ...
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
    def async_executemany(
        self,
        sql: str,
        parameters: list[object],
        text_mode: bool,
        statement_kind: str | None = ...,
        type_metadata: tuple[list[str | None], list[str], list[str | None]] | None = ...,
    ) -> Awaitable[NativeResult]: ...
    def async_has_table(self, name: str) -> Awaitable[bool]: ...
    def async_transaction(self, operation: str) -> Awaitable[None]: ...
    def async_begin_savepoint(self) -> Awaitable[str]: ...
    def async_release_savepoint(self, name: str) -> Awaitable[None]: ...
    def async_rollback_savepoint(self, name: str) -> Awaitable[None]: ...
    def async_is_active(self) -> Awaitable[bool]: ...
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
    def hydrate(
        self,
        cls: object,
        attrs: list[str],
        polymorphic: tuple[int, dict[object, type[object]]] | None = ...,
    ) -> list[object]: ...
    def hydrate_entities(
        self,
        specs: Sequence[
            tuple[type[object], list[str], tuple[int, dict[object, type[object]]] | None]
        ],
    ) -> list[tuple[object, ...]]: ...

class ArgumentError(CatalystError):
    """Invalid arguments to a database API call."""

class AmbiguousForeignKeysError(ArgumentError):
    """Raised when more than one foreign key path could satisfy a relationship join."""

class NoSuchModuleError(ArgumentError):
    """Dialect plugin could not be loaded."""

class StatementError(CatalystError):
    """Base class for SQL statement errors."""

    orig: BaseException | None
    statement: str | None
    params: object

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

class DataError(DatabaseError):
    """Invalid data values detected by the database."""

class TimeoutError(CatalystError):
    """Connection pool checkout timed out or was exhausted."""

class InvalidRequestError(CatalystError):
    """Invalid connection or transaction state."""

class PendingRollbackError(InvalidRequestError):
    """Session operation attempted while a rollback is pending."""

class UnmappedInstanceError(InvalidRequestError):
    """An operation was attempted on an object that is not mapped."""

class StaleDataError(CatalystError):
    """An UPDATE or DELETE matched fewer rows than expected."""

class ObjectDeletedError(InvalidRequestError):
    """An operation expected an object row that no longer exists."""

class DetachedInstanceError(CatalystError):
    """An unloaded attribute was accessed on a detached object."""

@final
class NativeUow:
    """Per-session unit-of-work registry: identity map plus object state.

    Holds strong references to managed instances keyed by pointer identity;
    the identity map keys ``(class pointer, pk)`` to instances.  All attribute
    set/expiry bookkeeping lives here so whole collections transition in one
    FFI call.
    """

    def __new__(cls, session: object) -> NativeUow: ...
    @property
    def session(self) -> object | None: ...
    def attach_pending(self, obj: object, cls: object, attrs: list[str]) -> None: ...
    def attach_persistent(self, obj: object, cls: object, attrs: list[str], pk: object) -> None: ...
    def status(self, obj: object) -> int: ...
    def is_managed(self, obj: object) -> bool: ...
    def attr_set(self, obj: object, name: str) -> None: ...
    def attr_expired(self, obj: object, name: str) -> bool: ...
    def attr_loaded(self, obj: object, name: str) -> None: ...
    def pk_of(self, obj: object) -> object | None: ...
    def is_modified(self, obj: object) -> bool: ...
    def new_objects(self) -> list[object]: ...
    def dirty_objects(self) -> list[object]: ...
    def deleted_objects(self) -> list[object]: ...
    def dirty_attrs_of(self, obj: object) -> list[str]: ...
    def expired_attrs_of(self, obj: object) -> list[str]: ...
    def identity_get(self, cls: object, pk: object) -> object | None: ...
    def has_changes(self) -> bool: ...
    def expire(self, obj: object) -> None: ...
    def expire_all(self) -> list[object]: ...
    def mark_loaded(self, obj: object) -> None: ...
    def mark_clean(self, obj: object) -> None: ...
    def mark_deleted(self, obj: object) -> None: ...
    def un_delete(self, obj: object) -> None: ...
    def commit_transition(self) -> list[tuple[object, object | None]]: ...
    def rollback(self) -> tuple[list[object], list[tuple[object, int]], list[object]]: ...
    def snapshot_begin(self) -> None: ...
    def snapshot_commit(self) -> list[object]: ...
    def snapshot_rollback(
        self,
    ) -> tuple[list[tuple[object, int]], list[object], list[object]]: ...
    def expire_attrs(self, obj: object, names: list[str]) -> None: ...
    def detach(self, obj: object) -> tuple[object | None, int, list[str], list[str]] | None: ...
    def detach_all(
        self,
    ) -> list[tuple[object, object | None, int, list[str], list[str]]]: ...
    def persistent_objects(self, cls: object) -> list[object]: ...
    def clear(self) -> None: ...
    def flush_transition(
        self,
        persisted: list[tuple[object, object]],
        cleaned: list[object],
        dropped: list[object],
    ) -> None: ...
    def reconcile(
        self, cls: object, pk_attr: str, attrs: list[str], instances: list[object]
    ) -> list[object]: ...
