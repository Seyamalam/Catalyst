"""Thin Python engine and connection shell over Catalyst's native SQLite core."""

from __future__ import annotations

from collections.abc import Generator, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._catalyst import (
    ArgumentError,
    InvalidRequestError,
    NativeConnection,
    NativeEngine,
    NativeResult,
)


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

    def __init__(self, native: NativeConnection) -> None:
        self._native = native
        self._closed = False

    def execute(
        self,
        statement: str | TextClause,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CursorResult:
        """Execute SQL with positional values or a mapping for named binds."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        if isinstance(statement, TextClause):
            sql = statement.text
            if parameters is not None and not isinstance(parameters, Mapping):
                raise ArgumentError("List argument must consist only of dictionaries")
            bindings: Sequence[object] | Mapping[str, object] = (
                {} if parameters is None else parameters
            )
        else:
            sql = statement
            if parameters is not None and isinstance(parameters, Mapping):
                raise ArgumentError("Mapping is only supported with text() statements")
            bindings = () if parameters is None else parameters
        if isinstance(bindings, Mapping):
            native_parameters: dict[str, object] | list[object] = dict(bindings)
        else:
            native_parameters = list(bindings)
        return CursorResult(
            self._native.execute(sql, native_parameters, isinstance(statement, TextClause))
        )

    def begin(self) -> RootTransaction:
        """Begin an explicit root transaction on this connection."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        self._native.begin()
        return RootTransaction(self._native)

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

    def __init__(self, database: str | Path) -> None:
        self._native = NativeEngine(str(database))
        self._disposed = False

    def connect(self) -> Connection:
        """Return a logical connection handle."""
        if self._disposed:
            raise RuntimeError("Engine is disposed")
        return Connection(self._native.connect())

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


def create_engine(url: str | Path, **options: Any) -> Engine:
    """Create a synchronous SQLite engine.

    Accepts ``:memory:``, a filesystem path, and ``sqlite:///path`` URLs.
    Other SQLAlchemy engine options and dialects are not yet supported.
    """
    if options:
        unsupported = ", ".join(sorted(options))
        raise TypeError(f"Unsupported engine options: {unsupported}")

    value = str(url)
    if value == ":memory:":
        database = value
    elif value.startswith("sqlite:///"):
        database = value.removeprefix("sqlite:///")
    elif value.startswith("sqlite://"):
        raise ValueError("SQLite URLs must use sqlite:///path or sqlite:///:memory:")
    else:
        database = value
    return Engine(database)
