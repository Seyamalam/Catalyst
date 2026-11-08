"""Thin SQLAlchemy-Core-shaped expression constructors; AST compilation is native."""

from __future__ import annotations

import warnings
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from ._catalyst import ArgumentError, compile_expression

if TYPE_CHECKING:
    from .engine import AsyncEngine, Engine


class TypeEngine:
    """Marker base for declarative SQL column types."""


class Integer(TypeEngine):
    pass


class String(TypeEngine):
    def __init__(self, length: int | None = None) -> None:
        self.length = length


class Boolean(TypeEngine):
    pass


class Text(TypeEngine):
    pass


class Float(TypeEngine):
    pass


class DateTime(TypeEngine):
    pass


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: object = None
    primary_key: bool = False
    # SQLAlchemy treats an unspecified ``nullable`` as False for primary key
    # columns and True otherwise.
    nullable: bool | None = None
    unique: bool = False
    # Client-side insert default (metadata only; never rendered in DDL,
    # matching SQLAlchemy's ``Column.default``).
    default: object = None
    server_default: str | None = None
    table_name: str = field(default="", init=False, repr=False, compare=False)
    owner: Table | None = field(default=None, init=False, repr=False, compare=False)

    def serialize(self) -> tuple[object, ...]:
        return ("column", self.table_name, self.name)

    def _binary(self, operator: str, other: object) -> Expression:
        if other is None and operator in {"=", "!="}:
            return Expression(("is_not" if operator == "!=" else "is", self.serialize()))
        return Expression(("binary", self.serialize(), operator, _value(other, self.name)))

    def __eq__(self, other: object) -> Expression:  # type: ignore[override]
        return self._binary("=", other)

    def __ne__(self, other: object) -> Expression:  # type: ignore[override]
        return self._binary("!=", other)

    def __lt__(self, other: object) -> Expression:
        return self._binary("<", other)

    def __le__(self, other: object) -> Expression:
        return self._binary("<=", other)

    def __gt__(self, other: object) -> Expression:
        return self._binary(">", other)

    def __ge__(self, other: object) -> Expression:
        return self._binary(">=", other)

    def __add__(self, other: object) -> Expression:
        return self._binary("+", other)

    def __sub__(self, other: object) -> Expression:
        return self._binary("-", other)

    def __mul__(self, other: object) -> Expression:
        return self._binary("*", other)

    def __truediv__(self, other: object) -> Expression:
        return self._binary("/", other)

    def __neg__(self) -> Expression:
        return Expression(("neg", self.serialize()))

    def in_(self, values: Sequence[object]) -> Expression:
        candidate = cast(object, values)
        if isinstance(candidate, (str, bytes)) or not isinstance(candidate, Sequence):
            raise ArgumentError("in_() requires a sequence of scalar values")
        # Plain base: Rust assigns the SQLAlchemy-style per-column counter
        # (group "id_1", members "id_1_1", "id_1_2", ...).
        return Expression(
            ("in", self.serialize(), tuple(_value(value, self.name) for value in values))
        )

    def is_(self, value: object) -> Expression:
        if value is not None:
            return Expression(("is_scalar", self.serialize(), _value(value, self.name)))
        return Expression(("is", self.serialize()))

    def is_not(self, value: object) -> Expression:
        if value is not None:
            return Expression(("is_not_scalar", self.serialize(), _value(value, self.name)))
        return Expression(("is_not", self.serialize()))

    def like(self, value: object, escape: str | None = None) -> Expression:
        if escape is None:
            return self._binary("LIKE", value)
        return Expression(("like_escape", self.serialize(), _value(value, self.name), escape))

    def between(self, lower: object, upper: object) -> Expression:
        return Expression(
            ("between", self.serialize(), _value(lower, self.name), _value(upper, self.name))
        )

    def asc(self) -> Expression:
        return Expression(("order", self.serialize(), True))

    def desc(self) -> Expression:
        return Expression(("order", self.serialize(), False))


class _ColumnCollection:
    _table: Table
    _columns: dict[str, Column]

    def __init__(self, table: Table) -> None:
        self._table = table
        self._columns = {column.name: column for column in table.columns}

    def __getattr__(self, name: str) -> Column:
        try:
            return self._columns[name]
        except KeyError as error:
            raise AttributeError(name) from error

    def __getitem__(self, name: str) -> Column:
        return self._columns[name]

    def __iter__(self) -> Iterator[Column]:
        return iter(self._table.columns)


class MetaData:
    """Container for table metadata with SQLAlchemy-style DDL creation."""

    def __init__(self) -> None:
        self.tables: dict[str, Table] = {}

    def create_all(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Create every table in this metadata in declaration order.

        Returns a coroutine when ``bind`` is an :class:`AsyncEngine`; the
        synchronous path creates tables immediately.
        """
        from .engine import run_ddl

        tables = list(self.tables.values())
        return run_ddl(
            bind, tables, [table.compile_ddl() for table in tables], checkfirst, drop=False
        )

    def drop_all(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Drop every table in this metadata in reverse declaration order.

        Returns a coroutine when ``bind`` is an :class:`AsyncEngine`.
        """
        from .engine import run_ddl

        tables = list(reversed(list(self.tables.values())))
        return run_ddl(
            bind, tables, [table.compile_drop() for table in tables], checkfirst, drop=True
        )


class Table:
    """Table name and column metadata used by Core expressions."""

    name: str
    metadata: MetaData
    columns: tuple[Column, ...]
    c: _ColumnCollection

    def __init__(self, name: str, metadata: MetaData, *columns: Column) -> None:
        self.name = name
        self.metadata = metadata
        bound_columns: list[Column] = []
        for column in columns:
            bound = Column(
                column.name,
                column.type,
                column.primary_key,
                column.nullable,
                column.unique,
                column.default,
                column.server_default,
            )
            object.__setattr__(bound, "table_name", name)
            object.__setattr__(bound, "owner", self)
            bound_columns.append(bound)
        self.columns = tuple(bound_columns)
        self.c = _ColumnCollection(self)
        metadata.tables[name] = self

    def __iter__(self) -> Iterator[Column]:
        return iter(self.columns)

    def compile_ddl(self) -> Compiled:
        """Compile this table's ``CREATE TABLE`` statement natively."""
        columns = tuple(
            (
                column.name,
                *_type_node(column.type),
                not _is_nullable(column),
                bool(column.unique),
                column.server_default,
            )
            for column in self.columns
        )
        primary_key = tuple(column.name for column in self.columns if column.primary_key)
        sql, _params, kind, _ordered = compile_expression(
            ("create_table", self.name, columns, primary_key)
        )
        return Compiled(sql, {}, kind, [])

    def compile_drop(self) -> Compiled:
        """Compile this table's ``DROP TABLE`` statement natively."""
        sql, _params, kind, _ordered = compile_expression(("drop_table", self.name))
        return Compiled(sql, {}, kind, [])

    def create(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Create this table; returns a coroutine for an :class:`AsyncEngine`."""
        from .engine import run_ddl

        return run_ddl(bind, [self], [self.compile_ddl()], checkfirst, drop=False)

    def drop(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Drop this table; returns a coroutine for an :class:`AsyncEngine`."""
        from .engine import run_ddl

        return run_ddl(bind, [self], [self.compile_drop()], checkfirst, drop=True)


@dataclass(frozen=True, slots=True, eq=False)
class Expression:
    node: tuple[object, ...]

    def __and__(self, other: Expression) -> Expression:
        return and_(self, other)

    def __or__(self, other: Expression) -> Expression:
        return or_(self, other)

    def __invert__(self) -> Expression:
        return not_(self)

    def __eq__(self, other: object) -> Expression:  # type: ignore[override]
        return self._binary("=", other)

    def __ne__(self, other: object) -> Expression:  # type: ignore[override]
        return self._binary("!=", other)

    def __neg__(self) -> Expression:
        return Expression(("neg", self.node))

    def _binary(self, operator: str, other: object) -> Expression:
        return Expression(("binary", self.node, operator, _value(other)))

    def __lt__(self, other: object) -> Expression:
        return self._binary("<", other)

    def __le__(self, other: object) -> Expression:
        return self._binary("<=", other)

    def __gt__(self, other: object) -> Expression:
        return self._binary(">", other)

    def __ge__(self, other: object) -> Expression:
        return self._binary(">=", other)

    def __add__(self, other: object) -> Expression:
        return self._binary("+", other)

    def __sub__(self, other: object) -> Expression:
        return self._binary("-", other)

    def __mul__(self, other: object) -> Expression:
        return self._binary("*", other)

    def __truediv__(self, other: object) -> Expression:
        return self._binary("/", other)


def _value(value: object, base: str = "param") -> tuple[object, ...]:
    if isinstance(value, (Column, Expression)):
        return _node(value)
    return ("literal", value, base)


def _node(value: Column | Expression | bool) -> tuple[object, ...]:
    if isinstance(value, bool):
        return ("boolean_word", value)
    return value.serialize() if isinstance(value, Column) else value.node


def _check_node_depth(node: object) -> None:
    """Walk the serialized tree so public compilation fails safely on deep trees."""
    if isinstance(node, tuple):
        children = cast(tuple[object, ...], node)
        for child in children:
            _check_node_depth(child)


_TYPE_TAGS = {
    "Integer": "integer",
    "String": "string",
    "Text": "text",
    "Boolean": "boolean",
    "Float": "float",
    "DateTime": "datetime",
}


def _type_node(type_obj: object) -> tuple[str, int | None]:
    """Map a Catalyst type marker to the native DDL type tag and length."""
    marker = type_obj if isinstance(type_obj, type) else type(type_obj)
    tag = _TYPE_TAGS.get(marker.__name__)
    if tag is None:
        raise ArgumentError(f"Unsupported DDL column type '{marker.__name__}'")
    length = None if isinstance(type_obj, type) else getattr(type_obj, "length", None)
    return tag, length


def _is_nullable(column: Column) -> bool:
    """Resolve SQLAlchemy's nullable default (False for primary key columns)."""
    if column.nullable is not None:
        return column.nullable
    return not column.primary_key


def _source_table(node: object) -> str | None:
    if not isinstance(node, tuple):
        return None
    parts = cast(tuple[object, ...], node)
    if parts and parts[0] == "column" and len(parts) > 1:
        return str(parts[1]) or None
    for child in parts:
        found = _source_table(child)
        if found is not None:
            return found
    return None


def and_(*clauses: Column | Expression) -> Expression:
    if not clauses:
        warnings.warn(
            "Invoking and_() without arguments is deprecated, and will be disallowed "
            "in a future release.   For an empty and_() construct, use "
            "'and_(true(), *args)' or 'and_(True, *args)'.",
            DeprecationWarning,
            stacklevel=2,
        )
    return Expression(("and", tuple(_node(clause) for clause in clauses)))


def or_(*clauses: Column | Expression) -> Expression:
    if not clauses:
        warnings.warn(
            "Invoking or_() without arguments is deprecated, and will be disallowed "
            "in a future release.   For an empty or_() construct, use "
            "'or_(false(), *args)' or 'or_(False, *args)'.",
            DeprecationWarning,
            stacklevel=2,
        )
    return Expression(("or", tuple(_node(clause) for clause in clauses)))


_NEGATIONS = {
    "=": "!=",
    "!=": "=",
    "<": ">=",
    ">": "<=",
    "<=": ">",
    ">=": "<",
    "LIKE": "NOT LIKE",
}


def not_(clause: Column | Expression) -> Expression:
    """Negate a clause the way SQLAlchemy does (operator-aware)."""
    node = _node(clause)
    if node[0] == "binary" and isinstance(node[2], str) and node[2] in _NEGATIONS:
        return Expression(("binary", node[1], _NEGATIONS[node[2]], node[3]))
    if node[0] == "in":
        return Expression(("not_in", node[1], node[2]))
    if node[0] == "not_in":
        return Expression(("in", node[1], node[2]))
    if node[0] == "like_escape":
        return Expression(("not_like_escape", node[1], node[2], node[3]))
    if node[0] == "not_like_escape":
        return Expression(("like_escape", node[1], node[2], node[3]))
    if node[0] == "between":
        return Expression(("not_between", node[1], node[2], node[3]))
    if node[0] == "not_between":
        return Expression(("between", node[1], node[2], node[3]))
    if node[0] == "is":
        return Expression(("is_not", node[1]))
    if node[0] == "is_not":
        return Expression(("is", node[1]))
    if node[0] in {"is_scalar", "is_not_scalar"}:
        # SQLAlchemy's IS/IS NOT operators are self-negating for non-NULL
        # values; only the NULL forms above swap operators.
        return Expression(node)
    if node[0] == "between":
        return Expression(("not_between", node[1], node[2], node[3]))
    return Expression(("not", node))


def asc(expression: Column | Expression) -> Expression:
    return Expression(("order", _node(expression), True))


def desc(expression: Column | Expression) -> Expression:
    return Expression(("order", _node(expression), False))


class _FunctionGenerator:
    def __getattr__(self, name: str) -> Any:
        if name not in {"count", "lower"}:
            raise AttributeError(name)

        def function(*args: Column | Expression) -> Expression:
            return Expression(("function", name, tuple(_node(arg) for arg in args)))

        return function


func = _FunctionGenerator()


class Compiled:
    """SQLite compiled SQL and parameters in execution order."""

    def __init__(
        self, sql: str, params: dict[str, object], kind: str, ordered: list[object]
    ) -> None:
        self.string = sql
        self.params = params
        self.statement_kind = kind
        self._ordered = ordered

    def __str__(self) -> str:
        return self.string


@dataclass(frozen=True, slots=True)
class Select:
    columns: tuple[Column | Expression, ...]
    table: Table | None = None
    predicate: Column | Expression | None = None
    ordering: tuple[Column | Expression, ...] = ()
    row_limit: int | None = None
    row_offset: int | None = None

    def where(self, *clauses: Column | Expression | bool) -> Select:
        if not clauses:
            return self
        nodes = tuple(
            Expression(("boolean_word", clause)) if isinstance(clause, bool) else clause
            for clause in clauses
        )
        predicate = nodes[0] if len(nodes) == 1 else and_(*nodes)  # noqa: PR01
        if self.predicate is not None:
            predicate = and_(self.predicate, predicate)
        return Select(
            self.columns, self.table, predicate, self.ordering, self.row_limit, self.row_offset
        )

    def order_by(self, *clauses: Column | Expression) -> Select:
        return Select(
            self.columns,
            self.table,
            self.predicate,
            self.ordering + clauses,
            self.row_limit,
            self.row_offset,
        )

    def limit(self, value: int) -> Select:
        return Select(
            self.columns, self.table, self.predicate, self.ordering, value, self.row_offset
        )

    def offset(self, value: int) -> Select:
        return Select(
            self.columns, self.table, self.predicate, self.ordering, self.row_limit, value
        )

    def _serialize(self) -> tuple[object, ...]:
        table = self.table
        if table is None and self.columns:
            for column in self.columns:
                source = _source_table(_node(column))
                if source is not None:
                    table_name = source
                    break
            else:
                table_name = None
        else:
            table_name = None if table is None else table.name
        if table is not None:
            table_name = table.name
        columns: tuple[Column | Expression, ...] = (
            table.columns if table is not None and self.columns == () else self.columns
        )
        nodes = tuple(_node(column) for column in columns)
        predicate = None if self.predicate is None else _node(self.predicate)
        ordering = tuple(_node(item) for item in self.ordering)
        has_pagination = self.row_limit is not None or self.row_offset is not None
        limit = (
            ("literal", self.row_limit if self.row_limit is not None else -1, "=param_1")
            if has_pagination
            else None
        )
        offset = (
            ("literal", self.row_offset if self.row_offset is not None else 0, "=param_2")
            if has_pagination
            else None
        )
        return (
            "select",
            nodes,
            table.name if table is not None else table_name,
            predicate,
            ordering,
            limit,
            offset,
        )

    def compile(self) -> Compiled:
        serialized = self._serialize()
        _check_node_depth(serialized)
        sql, params, kind, ordered = compile_expression(serialized)
        return Compiled(sql, params, kind, ordered)

    def __str__(self) -> str:
        return str(self.compile())


@dataclass(frozen=True, slots=True)
class Insert:
    table: Table
    values_map: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())

    def values(self, values: Mapping[str, object] | None = None, **kwargs: object) -> Insert:
        # Chained .values() calls accumulate, matching SQLAlchemy semantics.
        merged = dict(self.values_map)
        if values is not None:
            merged.update(values)
        merged.update(kwargs)
        return Insert(self.table, merged)

    def compile(self) -> Compiled:
        values: dict[str, tuple[object, ...]] = {
            column.name: ("literal", self.values_map[column.name], f"={column.name}")
            for column in self.table.columns
            if column.name in self.values_map
        }
        sql, params, kind, ordered = compile_expression(("insert", self.table.name, values))
        return Compiled(sql, params, kind, ordered)

    def __str__(self) -> str:
        return str(self.compile())


@dataclass(frozen=True, slots=True)
class Update:
    table: Table
    values_map: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())
    predicate: Column | Expression | None = None

    def values(self, values: Mapping[str, object] | None = None, **kwargs: object) -> Update:
        # Chained .values() calls accumulate, matching SQLAlchemy semantics.
        merged = dict(self.values_map)
        if values is not None:
            merged.update(values)
        merged.update(kwargs)
        return Update(self.table, merged, self.predicate)

    def where(self, *clauses: Column | Expression | bool) -> Update:
        if not clauses:
            return self
        nodes = tuple(
            Expression(("boolean_word", clause)) if isinstance(clause, bool) else clause
            for clause in clauses
        )
        predicate = nodes[0] if len(nodes) == 1 else and_(*nodes)  # noqa: PR01
        if self.predicate is not None:
            predicate = and_(self.predicate, predicate)
        return Update(self.table, self.values_map, predicate)

    def compile(self) -> Compiled:
        values: dict[str, tuple[object, ...]] = {
            column.name: ("literal", self.values_map[column.name], f"={column.name}")
            for column in self.table.columns
            if column.name in self.values_map
        }
        predicate = None if self.predicate is None else _node(self.predicate)
        sql, params, kind, ordered = compile_expression(
            ("update", self.table.name, values, predicate)
        )
        return Compiled(sql, params, kind, ordered)

    def __str__(self) -> str:
        return str(self.compile())


@dataclass(frozen=True, slots=True)
class Delete:
    table: Table
    predicate: Column | Expression | None = None

    def where(self, *clauses: Column | Expression | bool) -> Delete:
        if not clauses:
            return self
        nodes = tuple(
            Expression(("boolean_word", clause)) if isinstance(clause, bool) else clause
            for clause in clauses
        )
        predicate = nodes[0] if len(nodes) == 1 else and_(*nodes)  # noqa: PR01
        if self.predicate is not None:
            predicate = and_(self.predicate, predicate)
        return Delete(self.table, predicate)

    def compile(self) -> Compiled:
        predicate = None if self.predicate is None else _node(self.predicate)
        sql, params, kind, ordered = compile_expression(("delete", self.table.name, predicate))
        return Compiled(sql, params, kind, ordered)

    def __str__(self) -> str:
        return str(self.compile())


def select(*columns: Column | Expression | Table) -> Select:
    table = columns[0] if len(columns) == 1 and isinstance(columns[0], Table) else None
    return Select(tuple(column for column in columns if not isinstance(column, Table)), table)


def insert(table: Table) -> Insert:
    return Insert(table)


def update(table: Table) -> Update:
    return Update(table)


def delete(table: Table) -> Delete:
    return Delete(table)
