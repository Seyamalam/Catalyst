"""Thin Python engine and connection shell over Catalyst's native SQLite core."""

from __future__ import annotations

import logging
import math
import re
import warnings
from collections.abc import AsyncGenerator, Callable, Generator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ._catalyst import (
    ArgumentError,
    InvalidRequestError,
    NativeConnection,
    NativeEngine,
    NativeResult,
)
from .expression import Compiled, Delete, Insert, Select, Table, Update

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, repr=False)
class TextClause:
    """A SQL text statement suitable for execution with named parameters."""

    text: str

    def __repr__(self) -> str:
        return f"TextClause({self.text!r})"


class CursorResult:
    """Buffered result returned by a connection execution."""

    def __init__(self, native_result: NativeResult) -> None:
        self._native_result = native_result

    @property
    def keys(self) -> tuple[str, ...]:
        """Return result column names in database order."""
        return tuple(self._native_result.keys)

    @property
    def rowcount(self) -> int:
        """Return the number of rows changed by the statement."""
        return int(self._native_result.rowcount)

    def fetchone(self) -> tuple[object, ...] | None:
        """Fetch the next row, or ``None`` when the result is exhausted."""
        return self._native_result.fetchone()

    def fetchall(self) -> list[tuple[object, ...]]:
        """Fetch all remaining rows."""
        return self._native_result.fetchall()

    def all(self) -> list[tuple[object, ...]]:
        """Fetch all remaining rows."""
        return self.fetchall()

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        """Iterate over remaining rows."""
        while (row := self.fetchone()) is not None:
            yield row


class Connection:
    """A checked-out synchronous connection backed by Rust SQLite."""

    def __init__(self, native: NativeConnection, *, echo: bool = False) -> None:
        self._native = native
        self._closed = False
        self._echo = echo

    def execute(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CursorResult:
        """Execute SQL with positional values or a mapping for named binds."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        if isinstance(statement, (Select, Insert, Update, Delete)):
            statement = statement.compile()
        kind: str | None = None
        if isinstance(statement, Compiled):
            sql = statement.string
            bindings: Sequence[object] | Mapping[str, object] = statement._ordered
            kind = statement.statement_kind
        elif isinstance(statement, TextClause):
            sql = statement.text
            if parameters is not None and not isinstance(parameters, Mapping):
                raise ArgumentError("List argument must consist only of dictionaries")
            bindings = {} if parameters is None else parameters
        else:
            sql = statement
            if parameters is not None and isinstance(parameters, Mapping):
                raise ArgumentError("Mapping is only supported with text() statements")
            bindings = () if parameters is None else parameters
        native_parameters: dict[str, object] | list[object]
        if isinstance(bindings, Mapping):
            native_parameters = dict(bindings)
        else:
            native_parameters = list(bindings)
        if self._echo:
            _logger.info("%s", sql)
        return CursorResult(
            self._native.execute(sql, native_parameters, isinstance(statement, TextClause), kind)
        )

    def begin(self) -> RootTransaction:
        """Begin an explicit root transaction on this connection."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        self._native.begin()
        return RootTransaction(self._native)

    def has_table(self, name: str) -> bool:
        """Return whether a table exists, checking main and temp schemas."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        return self._native.has_table(name)

    def begin_nested(self) -> SavepointTransaction:
        """Begin a nested transaction implemented by a SQLite savepoint."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        name = self._native.begin_savepoint()
        return SavepointTransaction(self._native, name)

    def commit(self) -> None:
        """Commit the current explicit or implicitly begun transaction."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        self._native.commit()

    def rollback(self) -> None:
        """Roll back the current explicit or implicitly begun transaction."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        self._native.rollback()

    def close(self) -> None:
        """Roll back any open transaction and release this logical connection."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self) -> Connection:
        if self._closed:
            raise RuntimeError("Connection is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


class RootTransaction:
    """Thin handle for a native root transaction."""

    def __init__(self, native: NativeConnection) -> None:
        self.native = native
        self._finished = False

    def commit(self) -> None:
        """Commit and finish this transaction."""
        if self._finished or not self.native.is_active():
            raise InvalidRequestError("This transaction is inactive")
        self.native.commit()
        self._finished = True

    def rollback(self) -> None:
        """Roll back and finish this transaction."""
        if self._finished or not self.native.is_active():
            raise InvalidRequestError("This transaction is inactive")
        self.native.rollback()
        self._finished = True

    def close(self) -> None:
        """Roll back this transaction if it is still active."""
        if not self._finished:
            self.rollback()

    def __enter__(self) -> RootTransaction:
        if self._finished:
            raise InvalidRequestError("This transaction is no longer active")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None and self.native.is_active():
            self.commit()
        elif self.native.is_active():
            self.rollback()
        # An already-ended transaction no-ops here; SQLAlchemy warns instead
        # of raising in this path (documented parity difference).


class SavepointTransaction:
    """Thin handle for a native SQLite savepoint."""

    def __init__(self, native: NativeConnection, name: str) -> None:
        self.native = native
        self._name = name
        self._finished = False

    def commit(self) -> None:
        """Release this savepoint."""
        if self._finished:
            raise InvalidRequestError("This savepoint is no longer active")
        self.native.release_savepoint(self._name)
        self._finished = True

    def release(self) -> None:
        """Release this savepoint."""
        self.commit()

    def rollback(self) -> None:
        """Roll back to this savepoint and release it."""
        if self._finished:
            raise InvalidRequestError("This savepoint is no longer active")
        self.native.rollback_savepoint(self._name)
        self._finished = True

    def close(self) -> None:
        """Roll back this savepoint if it is still active."""
        if not self._finished:
            self.rollback()

    def __enter__(self) -> SavepointTransaction:
        if self._finished:
            raise InvalidRequestError("This savepoint is no longer active")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None and self.native.is_active():
            self.commit()
        elif self.native.is_active():
            self.rollback()
        # An already-ended savepoint no-ops here; SQLAlchemy warns instead of
        # raising in this path (documented parity difference).


class AsyncConnection:
    """A logical async connection using Tokio for native SQLite work."""

    def __init__(self, native: NativeConnection, *, echo: bool = False) -> None:
        self._native = native
        self._closed = False
        self._echo = echo

    async def execute(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CursorResult:
        if self._closed:
            raise RuntimeError("Connection is closed")
        if isinstance(statement, (Select, Insert, Update, Delete)):
            statement = statement.compile()
        kind: str | None = None
        if isinstance(statement, Compiled):
            sql = statement.string
            bindings: Sequence[object] | Mapping[str, object] = statement._ordered
            kind = statement.statement_kind
        elif isinstance(statement, TextClause):
            sql = statement.text
            if parameters is not None and not isinstance(parameters, Mapping):
                raise ArgumentError("List argument must consist only of dictionaries")
            bindings = {} if parameters is None else parameters
        else:
            sql = statement
            if parameters is not None and isinstance(parameters, Mapping):
                raise ArgumentError("Mapping is only supported with text() statements")
            bindings = () if parameters is None else parameters
        native_parameters: dict[str, object] | list[object]
        if isinstance(bindings, Mapping):
            native_parameters = dict(bindings)
        else:
            native_parameters = list(bindings)
        if self._echo:
            _logger.info("%s", sql)
        result = await self._native.async_execute(
            sql, native_parameters, isinstance(statement, TextClause), kind
        )
        return CursorResult(result)

    async def begin(self) -> None:
        if self._closed:
            raise RuntimeError("Connection is closed")
        await self._native.async_transaction("begin")

    async def commit(self) -> None:
        if self._closed:
            raise RuntimeError("Connection is closed")
        await self._native.async_transaction("commit")

    async def has_table(self, name: str) -> bool:
        """Return whether a table exists, checking main and temp schemas."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        return bool(await self._native.async_has_table(name))

    async def rollback(self) -> None:
        if self._closed:
            raise RuntimeError("Connection is closed")
        await self._native.async_transaction("rollback")

    async def close(self) -> None:
        if not self._closed:
            await self._native.async_transaction("close")
            self._closed = True

    async def __aenter__(self) -> AsyncConnection:
        if self._closed:
            raise RuntimeError("Connection is closed")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()


class _AsyncConnectContext:
    """Awaitable connection checkout that also supports ``async with``."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._connection: AsyncConnection | None = None

    def __await__(self) -> Any:
        return self._engine.connect_async().__await__()

    async def __aenter__(self) -> AsyncConnection:
        self._connection = await self._engine.connect_async()
        return self._connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._connection is not None:
            await self._connection.close()


class AsyncEngine:
    """Async SQLite engine with synchronous driver work isolated on Tokio."""

    def __init__(
        self,
        database: str | Path,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        timeout: float = 5.0,
        echo: bool = False,
    ) -> None:
        self._native = NativeEngine(str(database), pool_size, max_overflow, pool_timeout, timeout)
        self._echo = echo
        self._disposed = False

    def connect(self) -> _AsyncConnectContext:
        """Return an awaitable checkout and async context manager."""
        return _AsyncConnectContext(self)

    async def connect_async(self) -> AsyncConnection:
        if self._disposed:
            raise RuntimeError("Engine is disposed")
        return AsyncConnection(await self._native.async_connect(), echo=self._echo)

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator[AsyncConnection, None]:
        connection = await self.connect_async()
        try:
            await connection.begin()
            yield connection
        except BaseException:
            await connection.rollback()
            raise
        else:
            await connection.commit()
        finally:
            await connection.close()

    async def dispose(self) -> None:
        if not self._disposed:
            await self._native.async_close()
            self._disposed = True


class Engine:
    """A small synchronous SQLite engine facade.

    The initial engine intentionally supports SQLite only. Database operations,
    parameter binding, and hydration run in Rust; this class owns only the
    user-facing Python context-manager ergonomics.

    Examples:
        >>> engine = create_engine(":memory:")
        >>> with engine.connect() as connection:
        ...     connection.execute("SELECT ?", [7]).fetchone()
        (7,)
        >>> engine.dispose()
    """

    def __init__(
        self,
        database: str | Path,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        timeout: float = 5.0,
        echo: bool = False,
    ) -> None:
        self._native = NativeEngine(str(database), pool_size, max_overflow, pool_timeout, timeout)
        self._echo = echo
        self._disposed = False

    def connect(self) -> Connection:
        """Return a logical connection handle."""
        if self._disposed:
            raise RuntimeError("Engine is disposed")
        return Connection(self._native.connect(), echo=self._echo)

    @contextmanager
    def begin(self) -> Generator[Connection, None, None]:
        """Yield a connection in a root transaction, committing or rolling back."""
        connection = self.connect()
        try:
            with connection.begin():
                yield connection
        finally:
            connection.close()

    def dispose(self) -> None:
        """Close the native database connection; repeated calls are safe."""
        if not self._disposed:
            self._native.close()
            self._disposed = True

    def __enter__(self) -> Engine:
        if self._disposed:
            raise RuntimeError("Engine is disposed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.dispose()


def text(sql: str) -> TextClause:
    """Construct a SQL text statement for execution with named parameters."""
    return TextClause(sql)


def run_ddl(
    bind: Engine | AsyncEngine,
    tables: Sequence[Table],
    statements: Sequence[Compiled],
    checkfirst: bool,
    *,
    drop: bool = False,
) -> Any:
    """Execute compiled DDL with SQLAlchemy ``checkfirst`` semantics.

    Create statements run when the table is missing; drop statements run when
    it exists. Existence is checked with SQLAlchemy's sqlite ``has_table``
    probes (``PRAGMA main/temp.table_info``). The synchronous path executes
    immediately and commits; an ``AsyncEngine`` bind returns a coroutine.
    """
    if isinstance(bind, AsyncEngine):
        return run_ddl_async(bind, tables, statements, checkfirst, drop)
    with bind.connect() as connection:
        for table, statement in zip(tables, statements, strict=True):
            exists = connection.has_table(table.name)
            if not checkfirst or (exists if drop else not exists):
                connection.execute(statement)
        connection.commit()
    return None


async def run_ddl_async(
    bind: AsyncEngine,
    tables: Sequence[Table],
    statements: Sequence[Compiled],
    checkfirst: bool,
    drop: bool,
) -> None:
    connection = await bind.connect_async()
    try:
        for table, statement in zip(tables, statements, strict=True):
            exists = await connection.has_table(table.name)
            if not checkfirst or (exists if drop else not exists):
                await connection.execute(statement)
        await connection.commit()
    finally:
        await connection.close()


def create_engine(url: str | Path, **options: Any) -> Engine:
    """Create a synchronous SQLite engine with SQLAlchemy-style URL options."""
    database, config = _parse_engine_options(url, options, "create_engine")
    return Engine(database, **config)


def create_async_engine(url: str | Path, **options: Any) -> AsyncEngine:
    """Create an asynchronous SQLite engine backed by Tokio and buffered results."""
    database, config = _parse_engine_options(url, options, "create_async_engine")
    return AsyncEngine(database, **config)


def _parse_engine_options(
    url: str | Path, options: Mapping[str, Any], function_name: str
) -> tuple[str, dict[str, Any]]:
    database, url_options = _parse_database_url(str(url))
    merged: dict[str, Any] = dict(url_options)
    merged.update(options)
    # SQLAlchemy rejects only falsy ``future`` values; truthy strings pass.
    if not merged.pop("future", True):
        raise ArgumentError(
            f"The 'future' parameter passed to {function_name}() may only be set to True."
        )
    accepted = {"pool_size", "max_overflow", "pool_timeout", "echo", "timeout"}
    unknown = sorted(set(merged) - accepted)
    if unknown:
        names = ", ".join(repr(item) for item in unknown)
        raise TypeError(f"Invalid argument(s) {names} sent to {function_name}()")
    try:
        pool_size = int(merged.get("pool_size", 5))
        max_overflow = int(merged.get("max_overflow", 10))
        pool_timeout = float(merged.get("pool_timeout", 30.0))
        timeout = float(merged.get("timeout", 5.0))
    except (TypeError, ValueError) as error:
        raise ArgumentError("Invalid numeric value in engine options") from error
    if pool_size < 0 or max_overflow < -1 or timeout < 0 or pool_timeout < 0:
        raise ArgumentError("Engine pool sizes and timeouts must be non-negative")
    # Non-finite timeouts cannot map to a native Duration; SQLAlchemy accepts
    # ``inf`` but Catalyst rejects it (documented parity difference).
    if not (math.isfinite(pool_timeout) and math.isfinite(timeout)):
        raise ArgumentError("Engine timeouts must be finite numbers")
    echo = _asbool(merged.get("echo", False))
    return database, {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": pool_timeout,
        "timeout": timeout,
        "echo": echo,
    }


def _asbool(value: object) -> bool:
    """Coerce configuration values like SQLAlchemy's ``util.asbool``."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "y", "t"}:
            return True
        if lowered in {"false", "0", "no", "off", "n", "f", ""}:
            return False
        raise ValueError(f"String is not true/false: {value!r}")
    return bool(value)


def _load_sqlite() -> Callable[..., Engine]:
    return Engine


def _load_unimplemented_dialect(name: str) -> Callable[..., Engine]:
    raise ModuleNotFoundError(f"No module named '{name}'")


def _load_unknown_dialect(name: str) -> Callable[..., Engine]:
    from ._catalyst import NoSuchModuleError

    raise NoSuchModuleError(f"Can't load plugin: catalyst.dialects:{name}")


_DIALECTS: dict[str, Callable[[], Callable[..., Engine]]] = {
    "sqlite": _load_sqlite,
    "postgresql": lambda: _load_unimplemented_dialect("psycopg"),
    "mysql": lambda: _load_unimplemented_dialect("MySQLdb"),
    "oracle": lambda: _load_unimplemented_dialect("oracledb"),
}


_DEFAULT_DRIVERS = {"postgresql": "psycopg", "mysql": "MySQLdb", "oracle": "oracledb"}


def _parse_database_url(value: str) -> tuple[str, dict[str, str]]:
    """Parse URL syntax in Python because it is a compatibility surface."""
    if value == ":memory:":
        return value, {}
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", value)
    if match is None:
        if "://" in value:
            raise ArgumentError("Could not parse SQLAlchemy URL from given URL string")
        return value, {}
    scheme = match.group(1).lower()
    dialect, separator, driver = scheme.partition("+")
    if dialect not in _DIALECTS:
        _load_unknown_dialect(scheme)
    if dialect != "sqlite":
        # Known dialect without an installed/known driver fails the same way
        # SQLAlchemy does when its DBAPI module is missing.
        driver_name = driver if separator and driver else _DEFAULT_DRIVERS[dialect]
        if (
            separator
            and dialect in {"postgresql", "mysql"}
            and driver_name
            not in {
                "psycopg",
                "psycopg2",
                "mysqldb",
                "pymysql",
                "mysqlconnector",
            }
        ):
            _load_unknown_dialect(scheme)
        _load_unimplemented_dialect(driver_name)
    if separator and driver != "pysqlite":
        _load_unknown_dialect(scheme)
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ArgumentError("Could not parse SQLAlchemy URL from given URL string") from error
    if parsed.netloc:
        raise ArgumentError("Could not parse SQLAlchemy URL from given URL string")
    if parsed.path in {"", "/"}:
        database = ":memory:"
    else:
        raw_path = unquote(parsed.path)
        database = raw_path[1:] if raw_path.startswith("/") else raw_path
    query = parse_qs(parsed.query, keep_blank_values=True)
    if query:
        # SQLAlchemy's pysqlite dialect ignores unrecognized URL query
        # arguments with a warning instead of applying or rejecting them.
        keys = ", ".join(sorted(query))
        warnings.warn(
            f"Ignoring URL query argument(s) {keys}; they are not supported",
            RuntimeWarning,
            stacklevel=3,
        )
    return database, {}
