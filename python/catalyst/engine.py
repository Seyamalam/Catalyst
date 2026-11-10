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
from typing import Any, cast
from urllib.parse import parse_qs, unquote, urlsplit

from ._catalyst import (
    ArgumentError,
    DBAPIError,
    InvalidRequestError,
    NativeConnection,
    NativeEngine,
    NativeMysqlConnection,
    NativeMysqlEngine,
    NativePgConnection,
    NativePgEngine,
    NativeResult,
    StatementError,
)
from .expression import Compiled, Delete, Insert, Select, Table, Update

_logger = logging.getLogger(__name__)

# SQLAlchemy's ``StatementError`` instances always carry ``orig``,
# ``statement``, and ``params``; declare the attribute shape on the native
# exception type so stubs and attribute access reflect it.
StatementError.orig = None
StatementError.statement = None
StatementError.params = None


def _statement_error(orig: BaseException, statement: str, params: object) -> StatementError:
    """Build a SQLAlchemy-shaped ``StatementError`` wrapping a bind failure.

    Driver-level failures keep their concrete ``DBAPIError`` subclass (for
    example ``ProgrammingError`` for unbindable types), matching SQLAlchemy's
    wrapped exceptions; bind-processing failures use plain ``StatementError``.
    """
    origin = type(orig)
    cls = origin if issubclass(origin, DBAPIError) else StatementError
    message = f"({origin.__module__}.{origin.__qualname__}) {orig}\n[SQL: {statement}]"
    if params:
        message += f"\n[parameters: {params!r}]"
    error = cls(message)
    error.orig = orig
    error.statement = statement
    error.params = params
    return error


def _validate_compiled_parameters(parameters: object) -> None:
    if isinstance(parameters, Mapping):
        return
    if not isinstance(parameters, Sequence):
        raise ArgumentError("mapping or list expected for parameters")
    parameter_sequence = cast(Sequence[object], parameters)
    if any(not isinstance(item, Mapping) for item in parameter_sequence):
        raise ArgumentError("List argument must consist only of dictionaries")


def _validate_text_parameters(parameters: object) -> None:
    if not isinstance(parameters, Sequence):
        raise ArgumentError("List argument must consist only of dictionaries")
    parameter_sequence = cast(Sequence[object], parameters)
    if any(not isinstance(item, Mapping) for item in parameter_sequence):
        raise ArgumentError("List argument must consist only of dictionaries")


def _merge_compiled_batch(
    statement: Compiled,
    parameters: Sequence[object],
) -> list[dict[str, object]]:
    """Merge per-row overrides with statement-level compiled defaults."""
    defaults = dict(statement.params)
    default_names = set(defaults)
    merged: list[dict[str, object]] = []
    for group, row in enumerate(parameters):
        if not isinstance(row, Mapping):
            raise ArgumentError("List argument must consist only of dictionaries")
        row_values = cast(Mapping[str, object], row)
        missing = set(statement._bind_names) - default_names - set(row_values)
        if missing:
            name = next(name for name in statement._bind_names if name in missing)
            raise StatementError(
                f"A value is required for bind parameter '{name}', in parameter group {group}"
            )
        values = dict(defaults)
        values.update(row_values)
        merged.append(values)
    return merged


@dataclass(frozen=True, slots=True, repr=False)
class TextClause:
    """A SQL text statement suitable for execution with named parameters."""

    text: str

    def __repr__(self) -> str:
        return f"TextClause({self.text!r})"


class CursorResult:
    """Buffered result returned by a connection execution.

    With ``entity``, the buffered rows hydrate into mapped class instances in
    one batched Rust call and each result row is the one-tuple ``(entity,)``,
    matching SQLAlchemy's ORM-level ``Session.execute(select(Entity))``.
    """

    def __init__(self, native_result: NativeResult, entity: type[object] | None = None) -> None:
        self._native_result = native_result
        self._entity = entity
        self._entity_rows: list[tuple[object, ...]] | None = None
        self._entity_index = 0
        if entity is not None:
            mapper = cast(Any, entity).__mapper__
            instances = native_result.hydrate(entity, list(mapper.attrs))
            self._entity_rows = [(instance,) for instance in instances]

    @property
    def native_result(self) -> NativeResult:
        """Return the buffered native result backing this cursor."""
        return self._native_result

    @property
    def keys(self) -> tuple[str, ...]:
        """Return result column names in database order."""
        if self._entity_rows is not None and self._entity is not None:
            return (self._entity.__name__,)
        return tuple(self._native_result.keys)

    @property
    def rowcount(self) -> int:
        """Return the number of rows changed by the statement."""
        return int(self._native_result.rowcount)

    def fetchone(self) -> tuple[object, ...] | None:
        """Fetch the next row, or ``None`` when the result is exhausted."""
        if self._entity_rows is None:
            return self._native_result.fetchone()
        if self._entity_index >= len(self._entity_rows):
            return None
        row = self._entity_rows[self._entity_index]
        self._entity_index += 1
        return row

    def fetchall(self) -> list[tuple[object, ...]]:
        """Fetch all remaining rows."""
        if self._entity_rows is None:
            return self._native_result.fetchall()
        rows = self._entity_rows[self._entity_index :]
        self._entity_index = len(self._entity_rows)
        return rows

    def all(self) -> list[tuple[object, ...]]:
        """Fetch all remaining rows."""
        return self.fetchall()

    def scalars(self) -> ScalarResult:
        """Return a result view yielding the first column of each row."""
        return ScalarResult(self)

    def __iter__(self) -> Iterator[tuple[object, ...]]:
        """Iterate over remaining rows."""
        while (row := self.fetchone()) is not None:
            yield row


class NoResultFound(InvalidRequestError):
    """Raised when a result expected exactly one row and found none."""


class MultipleResultsFound(InvalidRequestError):
    """Raised when a result expected at most one row and found several."""


class ScalarResult:
    """Buffered result view yielding the first column of each row."""

    def __init__(self, result: CursorResult) -> None:
        self._result = result

    @property
    def keys(self) -> tuple[str, ...]:
        """Return result column names in database order."""
        return self._result.keys

    @property
    def rowcount(self) -> int:
        """Return the number of rows changed by the statement."""
        return self._result.rowcount

    def fetchone(self) -> object | None:
        """Fetch the next row's first column, or ``None`` when exhausted."""
        row = self._result.fetchone()
        return None if row is None else row[0]

    def fetchall(self) -> list[object]:
        """Fetch the first column of all remaining rows."""
        return [row[0] for row in self._result.fetchall()]

    def all(self) -> list[object]:
        """Fetch the first column of all remaining rows."""
        return self.fetchall()

    def first(self) -> object | None:
        """Fetch the first row's first column, or ``None``."""
        return self.fetchone()

    def one(self) -> object:
        """Fetch exactly one row's first column, like ``ScalarResult.one``."""
        items = self.fetchall()
        if not items:
            raise NoResultFound("No row was found when one was required")
        if len(items) != 1:
            raise MultipleResultsFound(
                f"Multiple rows were found when exactly one was required ({len(items)})"
            )
        return items[0]

    def one_or_none(self) -> object | None:
        """Fetch one row's first column, ``None`` for zero rows, error for many."""
        items = self.fetchall()
        if not items:
            return None
        if len(items) != 1:
            raise MultipleResultsFound(
                f"Multiple rows were found when one or none was required ({len(items)})"
            )
        return items[0]

    def __iter__(self) -> Iterator[object]:
        """Iterate over remaining first-column values."""
        while (item := self.fetchone()) is not None:
            yield item


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
        """Execute SQL with positional, named, or batched parameters."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        if (
            isinstance(statement, TextClause)
            and parameters is not None
            and not isinstance(parameters, Mapping)
        ):
            _validate_text_parameters(parameters)
        if (
            isinstance(parameters, Sequence)
            and not isinstance(parameters, (str, bytes))
            and not parameters
            and isinstance(statement, (Compiled, Select, Insert, Update, Delete))
        ):
            parameters = None
        if (
            isinstance(statement, (Compiled, Select, Insert, Update, Delete))
            and parameters is not None
        ):
            _validate_compiled_parameters(parameters)
        if isinstance(statement, Insert) and isinstance(parameters, Sequence) and parameters:
            first = parameters[0]
            first_values = cast(Mapping[str, object], first)
            defaults = dict(statement.values_map)
            # SQLAlchemy fixes INSERT shape from the first row plus the
            # statement-level .values(); later-only and unknown keys are ignored.
            shape_names = set(first_values) | set(defaults)
            required_row_names = set(first_values) - set(defaults)
            for group, row in enumerate(parameters):
                if not isinstance(row, Mapping):
                    raise ArgumentError("List argument must consist only of dictionaries")
                row_values = cast(Mapping[str, object], row)
                missing = required_row_names - set(row_values)
                if missing:
                    name = next(
                        name for name in statement.table.columns if name.name in missing
                    ).name
                    raise StatementError(
                        f"A value is required for bind parameter '{name}', "
                        f"in parameter group {group}"
                    )
            ordered_names = [
                column.name for column in statement.table.columns if column.name in shape_names
            ]
            insert_shape = {name: defaults.get(name) for name in ordered_names}
            statement = Insert(statement.table).values(insert_shape)
            compiled_shape = statement.compile()
            statement_values = dict(compiled_shape.params)
            statement_values.update(defaults)
            statement = Compiled(
                compiled_shape.string,
                statement_values,
                compiled_shape.statement_kind,
                [statement_values[name] for name in compiled_shape._bind_names],
                compiled_shape._bind_types,
                compiled_shape._bind_names,
                compiled_shape._result_types,
                bind_error=compiled_shape.bind_error,
            )
        if isinstance(statement, (Select, Insert, Update, Delete)):
            statement = statement.compile()
        if isinstance(statement, Compiled) and isinstance(parameters, Sequence) and parameters:
            parameters = _merge_compiled_batch(statement, parameters)
        kind: str | None = None
        bind_types: list[str | None] = []
        bind_names: list[str] = []
        result_types: list[str | None] = []
        if isinstance(statement, Compiled):
            sql = statement.string
            bindings: Sequence[object] | Mapping[str, object] = (
                statement._ordered
                if parameters is None or (isinstance(parameters, Sequence) and not parameters)
                else parameters
            )
            kind = statement.statement_kind
            if parameters is not None:
                bind_types = statement._bind_types
                bind_names = statement._bind_names
            result_types = statement._result_types
        elif isinstance(statement, TextClause):
            sql = statement.text
            if parameters is not None and not isinstance(parameters, Mapping):
                if any(not isinstance(item, Mapping) for item in parameters):
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
        if isinstance(statement, Compiled) and statement.bind_error is not None:
            raise _statement_error(
                statement.bind_error, sql, parameters if parameters is not None else ()
            )
        try:
            native_result = self._native.execute(
                sql,
                native_parameters,
                isinstance(statement, TextClause),
                kind,
                (bind_types, bind_names, result_types),
            )
        except DBAPIError as error:
            # Driver-level failures keep their concrete class and gain
            # statement context like SQLAlchemy's wrapped exceptions.
            raise _statement_error(
                error, sql, parameters if parameters is not None else ()
            ) from error
        except StatementError as error:
            # Already statement-shaped (a missing bind value, for example):
            # attach the execution context SQLAlchemy sets on these.
            if error.statement is None:
                error.statement = sql
                error.params = parameters if parameters is not None else ()
            raise
        return CursorResult(native_result)

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
        # An already-ended savepoint no-ops here; SQLAlchemy warns instead of
        # raising in this path (documented parity difference).
        if self._finished:
            return
        if exc_type is None and self.native.is_active():
            self.commit()
        elif self.native.is_active():
            self.rollback()


class AsyncSavepointTransaction:
    """Async handle for a native savepoint.

    Mirrors SQLAlchemy's ``AsyncConnection.begin_nested``: construction is
    synchronous and the ``SAVEPOINT`` statement is deferred to ``start()``
    or the async context manager.
    """

    def __init__(
        self,
        native: NativeConnection | NativePgConnection | NativeMysqlConnection,
    ) -> None:
        self._native = native
        self._name: str | None = None
        self._finished = False

    async def start(self) -> AsyncSavepointTransaction:
        """Emit the ``SAVEPOINT`` statement if it has not run yet."""
        if self._finished:
            raise InvalidRequestError("This savepoint is no longer active")
        if self._name is None:
            self._name = await self._native.async_begin_savepoint()
        return self

    async def commit(self) -> None:
        """Release this savepoint."""
        if self._finished or self._name is None:
            raise InvalidRequestError("This savepoint is no longer active")
        await self._native.async_release_savepoint(self._name)
        self._finished = True

    async def release(self) -> None:
        """Release this savepoint."""
        await self.commit()

    async def rollback(self) -> None:
        """Roll back to this savepoint and release it."""
        if self._finished or self._name is None:
            raise InvalidRequestError("This savepoint is no longer active")
        await self._native.async_rollback_savepoint(self._name)
        self._finished = True

    async def close(self) -> None:
        """Roll back this savepoint if it is still active."""
        if not self._finished and self._name is not None:
            await self.rollback()

    def __await__(self) -> Any:
        # ``await conn.begin_nested()`` starts the savepoint, matching
        # SQLAlchemy's AsyncTransaction.
        return self.start().__await__()

    async def __aenter__(self) -> AsyncSavepointTransaction:
        return await self.start()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        # An already-ended savepoint no-ops here; SQLAlchemy warns instead of
        # raising in this path (documented parity difference).
        if self._finished:
            return
        active = await self._native.async_is_active()
        if exc_type is None and active:
            await self.commit()
        elif active:
            await self.rollback()


class AsyncConnection:
    """A logical async connection using Tokio for native database work."""

    def __init__(
        self,
        native: NativeConnection | NativePgConnection | NativeMysqlConnection,
        *,
        echo: bool = False,
        postgres: bool = False,
        mysql: bool = False,
    ) -> None:
        self._native = native
        self._closed = False
        self._echo = echo
        self._postgres = postgres
        self._mysql = mysql

    async def execute(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CursorResult:
        if self._closed:
            raise RuntimeError("Connection is closed")
        if self._postgres and isinstance(statement, (Select, Insert, Update, Delete)):
            statement = statement.compile("postgresql")
        if (
            self._postgres
            and isinstance(statement, Compiled)
            and isinstance(parameters, Sequence)
            and not isinstance(parameters, (str, bytes))
            and parameters
        ):
            raise ArgumentError("PostgreSQL executemany is not supported yet")
        if (
            (self._postgres or self._mysql)
            and isinstance(parameters, Mapping)
            and isinstance(statement, str)
        ):
            dialect = "PostgreSQL" if self._postgres else "MySQL"
            raise ArgumentError(f"{dialect} named parameters require text() statements")
        # MySQL compiles lazily at the shared compile site so Insert batches
        # keep their first-row shape and missing-key checks (PostgreSQL is
        # already compiled above; SQLite uses the default dialect).
        compile_dialect = "mysql" if self._mysql else None
        if (
            isinstance(statement, TextClause)
            and parameters is not None
            and not isinstance(parameters, Mapping)
        ):
            _validate_text_parameters(parameters)
        if (
            isinstance(parameters, Sequence)
            and not isinstance(parameters, (str, bytes))
            and not parameters
            and isinstance(statement, (Compiled, Select, Insert, Update, Delete))
        ):
            parameters = None
        if (
            isinstance(statement, (Compiled, Select, Insert, Update, Delete))
            and parameters is not None
        ):
            _validate_compiled_parameters(parameters)
        if isinstance(statement, Insert) and isinstance(parameters, Sequence) and parameters:
            first = parameters[0]
            first_values = cast(Mapping[str, object], first)
            defaults = dict(statement.values_map)
            # SQLAlchemy fixes INSERT shape from the first row plus the
            # statement-level .values(); later-only and unknown keys are ignored.
            shape_names = set(first_values) | set(defaults)
            required_row_names = set(first_values) - set(defaults)
            for group, row in enumerate(parameters):
                if not isinstance(row, Mapping):
                    raise ArgumentError("List argument must consist only of dictionaries")
                row_values = cast(Mapping[str, object], row)
                missing = required_row_names - set(row_values)
                if missing:
                    name = next(
                        name for name in statement.table.columns if name.name in missing
                    ).name
                    raise StatementError(
                        f"A value is required for bind parameter '{name}', "
                        f"in parameter group {group}"
                    )
            ordered_names = [
                column.name for column in statement.table.columns if column.name in shape_names
            ]
            insert_shape = {name: defaults.get(name) for name in ordered_names}
            statement = Insert(statement.table).values(insert_shape)
            compiled_shape = statement.compile(compile_dialect)
            statement_values = dict(compiled_shape.params)
            statement_values.update(defaults)
            statement = Compiled(
                compiled_shape.string,
                statement_values,
                compiled_shape.statement_kind,
                [statement_values[name] for name in compiled_shape._bind_names],
                compiled_shape._bind_types,
                compiled_shape._bind_names,
                compiled_shape._result_types,
                bind_error=compiled_shape.bind_error,
            )
        if isinstance(statement, (Select, Insert, Update, Delete)):
            statement = statement.compile(compile_dialect)
        if isinstance(statement, Compiled) and isinstance(parameters, Sequence) and parameters:
            parameters = _merge_compiled_batch(statement, parameters)
        kind: str | None = None
        bind_types: list[str | None] = []
        bind_names: list[str] = []
        result_types: list[str | None] = []
        if isinstance(statement, Compiled):
            sql = statement.string
            bindings: Sequence[object] | Mapping[str, object] = (
                statement._ordered
                if parameters is None or (isinstance(parameters, Sequence) and not parameters)
                else parameters
            )
            kind = statement.statement_kind
            if parameters is not None or self._postgres or self._mysql:
                bind_types = statement._bind_types
                bind_names = statement._bind_names
            result_types = statement._result_types
        elif isinstance(statement, TextClause):
            sql = statement.text
            if parameters is not None and not isinstance(parameters, Mapping):
                if any(not isinstance(item, Mapping) for item in parameters):
                    raise ArgumentError("List argument must consist only of dictionaries")
            bindings = {} if parameters is None else parameters
        else:
            sql = statement
            if parameters is not None and isinstance(parameters, Mapping):
                raise ArgumentError("Mapping is only supported with text() statements")
            bindings = () if parameters is None else parameters
        if (
            (self._postgres or self._mysql)
            and isinstance(statement, Compiled)
            and isinstance(bindings, Mapping)
        ):
            mapping_bindings = cast(Mapping[str, object], bindings)
            ordered_bindings: list[object] = []
            for name in statement._bind_names:
                if name not in mapping_bindings:
                    raise StatementError(f"A value is required for bind parameter '{name}'")
                ordered_bindings.append(mapping_bindings[name])
            bindings = ordered_bindings
        native_parameters: dict[str, object] | list[object]
        if isinstance(bindings, Mapping):
            native_parameters = dict(bindings)
        else:
            native_parameters = list(bindings)
        if self._echo:
            _logger.info("%s", sql)
        if isinstance(statement, Compiled) and statement.bind_error is not None:
            raise _statement_error(
                statement.bind_error, sql, parameters if parameters is not None else ()
            )
        native = self._native
        if (
            isinstance(native, NativeMysqlConnection)
            and isinstance(native_parameters, list)
            and native_parameters
            and isinstance(native_parameters[0], (Mapping, Sequence))
            and not isinstance(native_parameters[0], (str, bytes))
        ):
            # A dict/sequence batch executes as one native executemany call;
            # the whole batch crosses the FFI boundary once in Rust.
            awaitable = native.async_executemany(
                sql,
                native_parameters,
                isinstance(statement, TextClause),
                kind,
                (bind_types, bind_names, result_types),
            )
        else:
            awaitable = native.async_execute(
                sql,
                native_parameters,
                isinstance(statement, TextClause),
                kind,
                (bind_types, bind_names, result_types),
            )
        try:
            result = await awaitable
        except DBAPIError as error:
            raise _statement_error(
                error, sql, parameters if parameters is not None else ()
            ) from error
        except StatementError as error:
            if error.statement is None:
                error.statement = sql
                error.params = parameters if parameters is not None else ()
            raise
        return CursorResult(result)

    async def begin(self) -> None:
        if self._closed:
            raise RuntimeError("Connection is closed")
        await self._native.async_transaction("begin")

    def begin_nested(self) -> AsyncSavepointTransaction:
        """Begin a nested transaction implemented by a native savepoint."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        return AsyncSavepointTransaction(self._native)

    async def commit(self) -> None:
        if self._closed:
            raise RuntimeError("Connection is closed")
        await self._native.async_transaction("commit")

    async def has_table(self, name: str) -> bool:
        """Return whether a table exists in the connection's visible schemas."""
        if self._closed:
            raise RuntimeError("Connection is closed")
        return bool(await self._native.async_has_table(name))

    async def rollback(self) -> None:
        if self._closed:
            raise RuntimeError("Connection is closed")
        await self._native.async_transaction("rollback")

    async def close(self) -> None:
        if not self._closed:
            if isinstance(self._native, NativePgConnection | NativeMysqlConnection):
                await self._native.async_close()
            else:
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
    """Async engine using Tokio-native PostgreSQL or buffered SQLite."""

    def __init__(
        self,
        database: str | Path,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        timeout: float = 5.0,
        echo: bool = False,
        postgres: bool = False,
        mysql: bool = False,
    ) -> None:
        if postgres:
            self._native: NativeEngine | NativePgEngine | NativeMysqlEngine = NativePgEngine(
                str(database), pool_size, max_overflow, pool_timeout
            )
        elif mysql:
            self._native = NativeMysqlEngine(str(database), pool_size, max_overflow, pool_timeout)
        else:
            self._native = NativeEngine(
                str(database), pool_size, max_overflow, pool_timeout, timeout
            )
        self._echo = echo
        self._postgres = postgres
        self._mysql = mysql
        self._disposed = False

    def connect(self) -> _AsyncConnectContext:
        """Return an awaitable checkout and async context manager."""
        return _AsyncConnectContext(self)

    async def connect_async(self) -> AsyncConnection:
        if self._disposed:
            raise RuntimeError("Engine is disposed")
        return AsyncConnection(
            await self._native.async_connect(),
            echo=self._echo,
            postgres=self._postgres,
            mysql=self._mysql,
        )

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
    it exists. Existence uses the bind dialect's ``has_table`` probe (SQLite
    ``PRAGMA main/temp.table_info``; PostgreSQL's search-path catalog query).
    The synchronous path executes immediately and commits; an ``AsyncEngine``
    bind returns a coroutine.
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


def _native_driver_options(options: dict[str, Any]) -> dict[str, Any]:
    """Validate pool/future/echo options shared by the native async drivers."""
    merged = dict(options)
    if not merged.pop("future", True):
        raise ArgumentError(
            "The 'future' parameter passed to create_async_engine() may only be set to True."
        )
    echo = _asbool(merged.pop("echo", False))
    try:
        pool_size = int(merged.pop("pool_size", 5))
        max_overflow = int(merged.pop("max_overflow", 10))
        pool_timeout = float(merged.pop("pool_timeout", 30.0))
    except (TypeError, ValueError) as error:
        raise ArgumentError("Invalid numeric value in engine options") from error
    if pool_size < 0 or max_overflow < -1 or pool_timeout < 0:
        raise ArgumentError("Engine pool sizes and timeouts must be non-negative")
    if not math.isfinite(pool_timeout):
        raise ArgumentError("Engine timeouts must be finite numbers")
    if merged:
        names = ", ".join(repr(item) for item in sorted(merged))
        raise TypeError(f"Invalid argument(s) {names} sent to create_async_engine()")
    return {
        "pool_size": pool_size,
        "max_overflow": max_overflow,
        "pool_timeout": pool_timeout,
        "echo": echo,
    }


def create_async_engine(url: str | Path, **options: Any) -> AsyncEngine:
    """Create an async SQLite, PostgreSQL, or initial MySQL engine."""
    value = str(url)
    if _is_postgres_url(value):
        config = _native_driver_options(dict(options))
        normalized = re.sub(
            r"^postgres(?:ql)?\+[^:]+://", "postgresql://", value, flags=re.IGNORECASE
        )
        return AsyncEngine(normalized, postgres=True, **config)
    if _is_mysql_url(value):
        config = _native_driver_options(dict(options))
        normalized = re.sub(r"^mysql\+[^:]+://", "mysql://", value, flags=re.IGNORECASE)
        return AsyncEngine(normalized, mysql=True, **config)
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


def _is_postgres_url(value: str) -> bool:
    return re.match(r"^(?:postgres|postgresql)(?:\+[^:]+)?://", value, re.IGNORECASE) is not None


def _is_mysql_url(value: str) -> bool:
    return re.match(r"^mysql(?:\+[^:]+)?://", value, re.IGNORECASE) is not None


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
