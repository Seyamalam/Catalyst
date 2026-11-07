"""Thin Python engine and connection shell over Catalyst's native SQLite core."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from ._catalyst import NativeConnection, NativeEngine, NativeResult


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

    def execute(self, statement: str, parameters: Sequence[object] = ()) -> CursorResult:
        """Execute SQL with one positional parameter batch."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        return CursorResult(self._native.execute(statement, list(parameters)))

    def close(self) -> None:
        """Release this logical connection handle."""
        if not self._closed:
            self._native.close()
            self._closed = True

    def __enter__(self) -> Connection:
        if self._closed:
            raise RuntimeError("Connection is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


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
