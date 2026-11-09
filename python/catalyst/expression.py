"""Thin SQLAlchemy-Core-shaped expression constructors; AST compilation is native."""

from __future__ import annotations

import warnings
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

from ._catalyst import ArgumentError, compile_expression

# Compiled statements are deterministic: memoize by the serialized form of the
# frozen statement. Keys that are not hashable (arbitrary Python objects in
# bind values) bypass the cache. Compiled results are rebuilt per call so the
# shared cache entry is never exposed mutably.
_COMPILE_CACHE_SIZE = 512


@lru_cache(maxsize=_COMPILE_CACHE_SIZE)
def _compile_cached(
    serialized: tuple[object, ...],
    dialect: str,
) -> tuple[
    str,
    tuple[tuple[str, object], ...],
    str,
    tuple[str | None, ...],
    tuple[str, ...],
    tuple[str | None, ...],
]:
    sql, params, kind, _ordered, bind_types, bind_names, result_types = compile_expression(
        serialized, dialect
    )
    return (
        sql,
        tuple(params.items()),
        kind,
        tuple(bind_types),
        tuple(bind_names),
        tuple(result_types),
    )


def _compiled_from_cache(
    serialized: tuple[object, ...],
    dialect: str,
) -> tuple[
    str,
    dict[str, object],
    str,
    list[object],
    list[str | None],
    list[str],
    list[str | None],
]:
    try:
        sql, items, kind, bind_types, bind_names, result_types = _compile_cached(
            serialized, dialect
        )
    except TypeError:
        # Bind values may be unhashable objects; compile directly without caching.
        sql, params, kind, ordered, bind_types, bind_names, result_types = compile_expression(
            serialized, dialect
        )
        return (
            sql,
            params,
            kind,
            list(ordered),
            list(bind_types),
            list(bind_names),
            list(result_types),
        )
    # Rebuild per call so the shared cache entry is never exposed mutably.
    params = dict(items)
    ordered = [value for _key, value in items]
    return sql, params, kind, ordered, list(bind_types), list(bind_names), list(result_types)


if TYPE_CHECKING:
    from .engine import AsyncEngine, Engine


def _dialect_name(dialect: str | None) -> str:
    """Normalize the compile-time dialect selector.

    Catalyst accepts dialect names (``"sqlite"``/``"postgresql"``/``"mysql"``)
    where SQLAlchemy accepts dialect objects; ``None`` means SQLite.
    """
    if dialect is None:
        return "sqlite"
    if dialect not in {"sqlite", "postgresql", "mysql"}:
        raise ArgumentError(f"Unsupported compilation dialect '{dialect}'")
    return dialect


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


class Double(TypeEngine):
    pass


class DateTime(TypeEngine):
    pass


class Date(TypeEngine):
    pass


class LargeBinary(TypeEngine):
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
        return ("column", self.table_name, self.name, _column_type_tag(self.type))

    def _binary(self, operator: str, other: object) -> Expression:
        if other is None and operator in {"=", "!="}:
            return Expression(("is_not" if operator == "!=" else "is", self.serialize()))
        return Expression(
            (
                "binary",
                self.serialize(),
                operator,
                _value(other, self.name, _comparison_type_tag(other, _column_type_tag(self.type))),
            )
        )

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
            (
                "in",
                self.serialize(),
                tuple(
                    _value(
                        value, self.name, _comparison_type_tag(value, _column_type_tag(self.type))
                    )
                    for value in values
                ),
            )
        )

    def is_(self, value: object) -> Expression:
        if value is not None:
            return Expression(
                (
                    "is_scalar",
                    self.serialize(),
                    _value(
                        value, self.name, _comparison_type_tag(value, _column_type_tag(self.type))
                    ),
                )
            )
        return Expression(("is", self.serialize()))

    def is_not(self, value: object) -> Expression:
        if value is not None:
            return Expression(
                (
                    "is_not_scalar",
                    self.serialize(),
                    _value(
                        value, self.name, _comparison_type_tag(value, _column_type_tag(self.type))
                    ),
                )
            )
        return Expression(("is_not", self.serialize()))

    def like(self, value: object, escape: str | None = None) -> Expression:
        if escape is None:
            return self._binary("LIKE", value)
        return Expression(
            (
                "like_escape",
                self.serialize(),
                _value(value, self.name, _comparison_type_tag(value, _column_type_tag(self.type))),
                escape,
            )
        )

    def between(self, lower: object, upper: object) -> Expression:
        return Expression(
            (
                "between",
                self.serialize(),
                _value(lower, self.name, _comparison_type_tag(lower, _column_type_tag(self.type))),
                _value(upper, self.name, _comparison_type_tag(upper, _column_type_tag(self.type))),
            )
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


def _bind_dialect(bind: Engine | AsyncEngine) -> str | None:
    """Select the DDL compilation dialect from the bind's native driver."""
    if getattr(bind, "_postgres", False):
        return "postgresql"
    if getattr(bind, "_mysql", False):
        return "mysql"
    return None


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
        dialect = _bind_dialect(bind)
        return run_ddl(
            bind, tables, [table.compile_ddl(dialect) for table in tables], checkfirst, drop=False
        )

    def drop_all(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Drop every table in this metadata in reverse declaration order.

        Returns a coroutine when ``bind`` is an :class:`AsyncEngine`.
        """
        from .engine import run_ddl

        tables = list(reversed(list(self.tables.values())))
        dialect = _bind_dialect(bind)
        return run_ddl(
            bind, tables, [table.compile_drop(dialect) for table in tables], checkfirst, drop=True
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

    def compile_ddl(self, dialect: str | None = None) -> Compiled:
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
        sql, _params, kind, _ordered, _bind_types, _bind_names, _result_types = compile_expression(
            ("create_table", self.name, columns, primary_key), _dialect_name(dialect)
        )
        return Compiled(sql, {}, kind, [], [], [], [])

    def compile_drop(self, dialect: str | None = None) -> Compiled:
        """Compile this table's ``DROP TABLE`` statement natively."""
        sql, _params, kind, _ordered, _bind_types, _bind_names, _result_types = compile_expression(
            ("drop_table", self.name), _dialect_name(dialect)
        )
        return Compiled(sql, {}, kind, [], [], [], [])

    def create(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Create this table; returns a coroutine for an :class:`AsyncEngine`."""
        from .engine import run_ddl

        dialect = _bind_dialect(bind)
        return run_ddl(bind, [self], [self.compile_ddl(dialect)], checkfirst, drop=False)

    def drop(self, bind: Engine | AsyncEngine, checkfirst: bool = True) -> Any:
        """Drop this table; returns a coroutine for an :class:`AsyncEngine`."""
        from .engine import run_ddl

        dialect = _bind_dialect(bind)
        return run_ddl(bind, [self], [self.compile_drop(dialect)], checkfirst, drop=True)


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


def _value(value: object, base: str = "param", type_tag: str | None = None) -> tuple[object, ...]:
    if isinstance(value, (Column, Expression)):
        return _node(value)
    return ("literal", value, base, type_tag)


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
    "Double": "double",
    "DateTime": "datetime",
    "Date": "date",
    "LargeBinary": "binary",
}


def _column_type_tag(type_obj: object) -> str | None:
    marker = type_obj if isinstance(type_obj, type) else type(type_obj)
    return _TYPE_TAGS.get(marker.__name__)


def _comparison_type_tag(value: object, type_tag: str | None) -> str | None:
    """Apply a column processor only when SQLAlchemy would bind this value as that type."""
    if type_tag is None or value is None:
        return type_tag
    if type_tag == "boolean":
        return type_tag if isinstance(value, bool) else None
    if type_tag in {"date", "datetime"}:
        from datetime import date, datetime

        if isinstance(value, datetime):
            return "datetime"
        if isinstance(value, date):
            return "date"
        return None
    if type_tag == "integer":
        return type_tag if isinstance(value, int) and not isinstance(value, bool) else None
    if type_tag in {"float", "double"}:
        from decimal import Decimal
        from numbers import Number

        return (
            type_tag
            if (isinstance(value, Number) or isinstance(value, Decimal))
            and not isinstance(value, (bool, complex))
            else None
        )
    if type_tag in {"string", "text"}:
        return type_tag if isinstance(value, str) else None
    if type_tag == "binary":
        return type_tag if isinstance(value, bytes) else None
    return None


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
    """Dialect-compiled SQL, ordered parameters, and native type metadata."""

    def __init__(
        self,
        sql: str,
        params: dict[str, object],
        kind: str,
        ordered: list[object],
        bind_types: list[str | None],
        bind_names: list[str],
        result_types: list[str | None],
        entity: type[object] | None = None,
    ) -> None:
        self.string = sql
        self.params = params
        self.statement_kind = kind
        self._ordered = ordered
        self._bind_types = bind_types
        self._bind_names = bind_names
        self._result_types = result_types
        self.entity = entity

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
    entity: type[object] | None = None

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
            self.columns,
            self.table,
            predicate,
            self.ordering,
            self.row_limit,
            self.row_offset,
            self.entity,
        )

    def order_by(self, *clauses: Column | Expression) -> Select:
        return Select(
            self.columns,
            self.table,
            self.predicate,
            self.ordering + clauses,
            self.row_limit,
            self.row_offset,
            self.entity,
        )

    def limit(self, value: int) -> Select:
        return Select(
            self.columns,
            self.table,
            self.predicate,
            self.ordering,
            value,
            self.row_offset,
            self.entity,
        )

    def offset(self, value: int) -> Select:
        return Select(
            self.columns,
            self.table,
            self.predicate,
            self.ordering,
            self.row_limit,
            value,
            self.entity,
        )

    def _serialize(self, dialect: str = "sqlite") -> tuple[object, ...]:
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
        if dialect == "postgresql":
            # SA's PG dialect renders LIMIT ALL for offset-only queries and
            # numbers binds by appearance, with INTEGER casts.
            limit = (
                ("literal", self.row_limit, "=param_1", "integer")
                if self.row_limit is not None
                else None
            )
            offset_name = "=param_2" if self.row_limit is not None else "=param_1"
            offset = (
                ("literal", self.row_offset, offset_name, "integer")
                if self.row_offset is not None
                else None
            )
        elif dialect == "mysql":
            # SA's MySQL dialect renders "LIMIT offset, count": the offset
            # bind appears first, and offset-only queries get the BIGINT max
            # count with no second bind.
            limit = (
                (
                    "literal",
                    self.row_limit,
                    "=param_2" if self.row_offset is not None else "=param_1",
                    "integer",
                )
                if self.row_limit is not None
                else None
            )
            offset = (
                ("literal", self.row_offset, "=param_1", "integer")
                if self.row_offset is not None
                else None
            )
        else:
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

    def compile(self, dialect: str | None = None) -> Compiled:
        name = _dialect_name(dialect)
        serialized = self._serialize(name)
        _check_node_depth(serialized)
        sql, params, kind, ordered, bind_types, bind_names, result_types = _compiled_from_cache(
            serialized, name
        )
        return Compiled(
            sql, params, kind, ordered, bind_types, bind_names, result_types, self.entity
        )

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

    def compile(self, dialect: str | None = None) -> Compiled:
        name = _dialect_name(dialect)
        if name in {"postgresql", "mysql"} and not self.values_map:
            # SA's PG and MySQL dialects render empty inserts as explicit NULL
            # binds for every column rather than SQLite's DEFAULT VALUES.
            values: dict[str, tuple[object, ...]] = {
                column.name: ("literal", None, f"={column.name}", _column_type_tag(column.type))
                for column in self.table.columns
            }
        else:
            values = {
                column.name: (
                    "literal",
                    self.values_map[column.name],
                    f"={column.name}",
                    _column_type_tag(column.type),
                )
                for column in self.table.columns
                if column.name in self.values_map
            }
        sql, params, kind, ordered, bind_types, bind_names, result_types = _compiled_from_cache(
            ("insert", self.table.name, values), name
        )
        return Compiled(sql, params, kind, ordered, bind_types, bind_names, result_types)

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

    def compile(self, dialect: str | None = None) -> Compiled:
        values: dict[str, tuple[object, ...]] = {
            column.name: (
                "literal",
                self.values_map[column.name],
                f"={column.name}",
                _column_type_tag(column.type),
            )
            for column in self.table.columns
            if column.name in self.values_map
        }
        predicate = None if self.predicate is None else _node(self.predicate)
        sql, params, kind, ordered, bind_types, bind_names, result_types = _compiled_from_cache(
            ("update", self.table.name, values, predicate), _dialect_name(dialect)
        )
        return Compiled(sql, params, kind, ordered, bind_types, bind_names, result_types)

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

    def compile(self, dialect: str | None = None) -> Compiled:
        predicate = None if self.predicate is None else _node(self.predicate)
        sql, params, kind, ordered, bind_types, bind_names, result_types = _compiled_from_cache(
            ("delete", self.table.name, predicate), _dialect_name(dialect)
        )
        return Compiled(sql, params, kind, ordered, bind_types, bind_names, result_types)

    def __str__(self) -> str:
        return str(self.compile())


def _entity_table(value: object) -> Table | None:
    """Resolve a mapped declarative class to its ``Table``; ``None`` otherwise."""
    table = getattr(value, "__table__", None)
    return table if isinstance(value, type) and isinstance(table, Table) else None


def select(*columns: Column | Expression | Table | type[object]) -> Select:
    if len(columns) == 1:
        first = columns[0]
        if isinstance(first, type):
            entity_table = _entity_table(first)
            if entity_table is not None:
                return Select((), entity_table, entity=first)
        table = first if isinstance(first, Table) else None
        return Select(
            tuple(column for column in columns if isinstance(column, (Column, Expression))),
            table,
        )
    return Select(tuple(column for column in columns if isinstance(column, (Column, Expression))))


def _dml_table(table: Table | type[object], verb: str) -> Table:
    if isinstance(table, Table):
        return table
    resolved = _entity_table(table)
    if resolved is None:
        raise ArgumentError(f"{verb}() expected a Table or mapped declarative class")
    return resolved


def insert(table: Table | type[object]) -> Insert:
    return Insert(_dml_table(table, "insert"))


def update(table: Table | type[object]) -> Update:
    return Update(_dml_table(table, "update"))


def delete(table: Table | type[object]) -> Delete:
    return Delete(_dml_table(table, "delete"))
