"""Thin SQLAlchemy-Core-shaped expression constructors; AST compilation is native."""

from __future__ import annotations

import warnings
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING, Any, ClassVar, cast

from ._catalyst import ArgumentError, DetachedInstanceError, compile_expression

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
    BaseException | None,
]:
    (
        sql,
        params,
        kind,
        _ordered,
        bind_types,
        bind_names,
        result_types,
        bind_error,
    ) = compile_expression(serialized, dialect)
    return (
        sql,
        tuple(params.items()),
        kind,
        tuple(bind_types),
        tuple(bind_names),
        tuple(result_types),
        bind_error,
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
    BaseException | None,
]:
    try:
        sql, items, kind, bind_types, bind_names, result_types, bind_error = _compile_cached(
            serialized, dialect
        )
    except TypeError:
        # Bind values may be unhashable objects; compile directly without caching.
        (
            sql,
            params,
            kind,
            ordered,
            bind_types,
            bind_names,
            result_types,
            bind_error,
        ) = compile_expression(serialized, dialect)
        return (
            sql,
            params,
            kind,
            list(ordered),
            list(bind_types),
            list(bind_names),
            list(result_types),
            bind_error,
        )
    # Rebuild per call so the shared cache entry is never exposed mutably.
    params = dict(items)
    ordered = [value for _key, value in items]
    return (
        sql,
        params,
        kind,
        ordered,
        list(bind_types),
        list(bind_names),
        list(result_types),
        bind_error,
    )


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

    # SQLAlchemy ``TypeCompiler.process`` reads ``_variant_mapping`` before
    # dispatching; Catalyst markers carry no variants.
    _variant_mapping: ClassVar[dict[str, object]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

        # SQLAlchemy's ``TypeCompiler`` compiles a column type via
        # ``type_._compiler_dispatch(compiler)`` on either an instance
        # (``String(30)``) or the marker class itself (``String``).  Bind a
        # per-class dispatch that renders both forms so
        # ``sqlalchemy.schema.CreateTable`` can compile a Catalyst ``Table``
        # in the parity suites.
        def _dispatch(self_or_visitor: object, visitor: object = None, **kw: object) -> str:
            marker = self_or_visitor if isinstance(self_or_visitor, TypeEngine) else cls()
            return _sa_type_name(marker)

        cls._compiler_dispatch = _dispatch  # type: ignore[attr-defined]


def _sa_type_name(marker: TypeEngine) -> str:
    """SQLAlchemy-dialect SQL name for a type marker (DDL parity interop)."""
    if isinstance(marker, Integer):
        return "INTEGER"
    if isinstance(marker, String):
        return f"VARCHAR({marker.length})" if marker.length else "VARCHAR"
    if isinstance(marker, Boolean):
        return "BOOLEAN"
    if isinstance(marker, Text):
        return "TEXT"
    if isinstance(marker, Float):
        return "FLOAT"
    if isinstance(marker, Double):
        return "DOUBLE PRECISION"
    if isinstance(marker, DateTime):
        return "DATETIME"
    if isinstance(marker, Date):
        return "DATE"
    if isinstance(marker, LargeBinary):
        return "BLOB"
    if isinstance(marker, Numeric):
        if marker.precision is not None:
            scale = f", {marker.scale}" if marker.scale is not None else ""
            return f"NUMERIC({marker.precision}{scale})"
        return "NUMERIC"
    raise ArgumentError(f"Cannot render {marker!r} as a SQLAlchemy type")


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


class Numeric(TypeEngine):
    """Fixed-point decimal type marker, mirroring ``sqlalchemy.Numeric``."""

    def __init__(
        self,
        precision: int | None = None,
        scale: int | None = None,
        decimal_return_scale: int | None = None,
        asdecimal: bool = True,
    ) -> None:
        self.precision = precision
        self.scale = scale
        self.decimal_return_scale = decimal_return_scale
        self.asdecimal = asdecimal


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """Reference to a remote column, e.g. ``ForeignKey("parent.id")``.

    The target is stored as ``"table.column"`` text and resolved lazily
    against table names in the owning :class:`MetaData`, matching
    SQLAlchemy's string-target behavior.
    """

    target: str

    def __post_init__(self) -> None:
        if "." not in self.target:
            raise ArgumentError(
                f"ForeignKey target '{self.target}' must be of the form 'table.column'"
            )

    @property
    def target_table(self) -> str:
        return self.target.rsplit(".", 1)[0]

    @property
    def target_column(self) -> str:
        return self.target.rsplit(".", 1)[1]


# Defaults restored when a positionally-passed ForeignKey is relocated into
# the ``foreign_key`` field by ``Column.__post_init__``.
_COLUMN_FIELD_DEFAULTS: dict[str, object] = {
    "primary_key": False,
    "nullable": None,
    "unique": False,
    "default": None,
    "server_default": None,
}


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
    foreign_key: ForeignKey | None = None
    table_name: str = field(default="", init=False, repr=False, compare=False)
    owner: Table | None = field(default=None, init=False, repr=False, compare=False)
    # Mapped attribute name when bound to a declarative class; used by
    # ``__get__`` to resolve instance state (the column name can differ).
    key: str = field(default="", init=False, repr=False, compare=False)

    # SQLAlchemy DDL-compilation duck-typing surface (see ``Table``): class
    # attributes, never per-instance data, so ``sqlalchemy.schema.CreateTable``
    # can render a Catalyst ``Table`` in the parity suites.
    system: ClassVar[bool] = False
    computed: ClassVar[None] = None
    identity: ClassVar[None] = None
    autoincrement: ClassVar[None] = None
    constraints: ClassVar[tuple[()]] = ()
    foreign_keys: ClassVar[tuple[()]] = ()

    @property
    def table(self) -> Table | None:
        """SQLAlchemy-style back-reference to the owning ``Table``."""
        return self.owner

    @property
    def dialect_options(self) -> Mapping[str, dict[str, object]]:
        """SQLAlchemy-style per-dialect options (SQLite keys preset)."""
        return defaultdict(
            dict,
            {"sqlite": {"on_conflict_not_null": None, "on_conflict_primary_key": None}},
        )

    def __post_init__(self) -> None:
        # Accept ``Column("x", Integer, ForeignKey("t.c"))`` positionally like
        # SQLAlchemy's ``*args`` handling: a ForeignKey found among the scalar
        # fields is relocated into ``foreign_key``.
        for fname in ("primary_key", "nullable", "unique", "default", "server_default"):
            value = getattr(self, fname)
            if isinstance(value, ForeignKey):
                if self.foreign_key is None:
                    object.__setattr__(self, "foreign_key", value)
                object.__setattr__(self, fname, _COLUMN_FIELD_DEFAULTS[fname])

    def __get__(self, instance: object, owner: object = None) -> object:
        # Non-data descriptor: the instance ``__dict__`` is consulted first, so
        # this only fires for expired or never-assigned attributes.  When the
        # owning session marked the attribute expired, trigger a lazy reload
        # before returning the (now populated) instance value.
        if instance is None:
            return self
        instance_dict = getattr(instance, "__dict__", None)
        if instance_dict is None:
            return None
        key = self.key or self.name
        uow = instance_dict.get("_catalyst_state")
        if uow is not None and uow.attr_expired(instance, key):
            session = uow.session
            if session is not None:
                session._load_expired(instance, key)
        elif uow is None:
            detached = instance_dict.get("_catalyst_detached")
            if detached is not None and key in detached[2]:
                raise DetachedInstanceError(
                    f"Instance '<{type(instance).__name__} at 0x{id(instance):x}>' "
                    "is not bound to a Session; attribute refresh operation "
                    "cannot proceed"
                )
        return instance_dict.get(key)

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

    def __setitem__(self, name: str, column: Column) -> None:
        self._columns[name] = column

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


class _SAConstraint:
    """Pre-compiled constraint fragment for SQLAlchemy's DDL compiler.

    ``sqlalchemy.schema.CreateTable`` walks ``table.primary_key`` /
    ``_sorted_constraints`` and compiles each entry via
    ``constraint._compiler_dispatch``; carrying the rendered SQL here lets a
    Catalyst :class:`Table` compile under that path (parity suites).
    """

    __slots__ = ("_sql", "use_alter")

    def __init__(self, sql: str) -> None:
        self._sql = sql
        self.use_alter = False

    def _should_create_for_compiler(self, compiler: object) -> bool:
        return True

    def _compiler_dispatch(self, visitor: object, **kw: object) -> str:
        return self._sql


class Table:
    """Table name and column metadata used by Core expressions."""

    name: str
    metadata: MetaData
    columns: tuple[Column, ...]
    c: _ColumnCollection
    schema: str | None
    description: str
    primary_key: _SAConstraint | None
    foreign_key_constraints: frozenset[_SAConstraint]
    dialect_options: Mapping[str, dict[str, object]]
    _prefixes: list[str]
    _sorted_constraints: list[_SAConstraint]

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
                column.foreign_key,
            )
            object.__setattr__(bound, "table_name", name)
            object.__setattr__(bound, "owner", self)
            bound_columns.append(bound)
        self.columns = tuple(bound_columns)
        self.c = _ColumnCollection(self)
        metadata.tables[name] = self
        # SQLAlchemy DDL-compilation duck-typing surface: the attributes
        # ``sqlalchemy.sql.compiler.DDLCompiler.visit_create_table`` reads so
        # ``SACreateTable(catalyst_table)`` renders identical DDL.
        self._prefixes = []
        self.schema = None
        self.description = name
        pk_names = [column.name for column in bound_columns if column.primary_key]
        self.primary_key = (
            _SAConstraint(f"PRIMARY KEY ({', '.join(pk_names)})") if pk_names else None
        )
        self._sorted_constraints = [
            _SAConstraint(
                f"FOREIGN KEY({column.name}) REFERENCES "
                f"{column.foreign_key.target_table} ({column.foreign_key.target_column})"
            )
            for column in bound_columns
            if column.foreign_key is not None
        ]
        self.foreign_key_constraints = frozenset(self._sorted_constraints)
        self.dialect_options = defaultdict(
            dict, {"sqlite": {"with_rowid": True, "strict": False, "autoincrement": False}}
        )

    def __iter__(self) -> Iterator[Column]:
        return iter(self.columns)

    def _append_columns(self, columns: Sequence[Column]) -> tuple[Column, ...]:
        """Bind ``columns`` onto this table in place (STI subclass additions)."""
        bound: list[Column] = []
        for column in columns:
            bound_column = Column(
                column.name,
                column.type,
                column.primary_key,
                column.nullable,
                column.unique,
                column.default,
                column.server_default,
                column.foreign_key,
            )
            object.__setattr__(bound_column, "table_name", self.name)
            object.__setattr__(bound_column, "owner", self)
            bound.append(bound_column)
        self.columns = (*self.columns, *bound)
        for bound_column in bound:
            self.c[bound_column.name] = bound_column
            if bound_column.foreign_key is not None:
                constraint = _SAConstraint(
                    f"FOREIGN KEY({bound_column.name}) REFERENCES "
                    f"{bound_column.foreign_key.target_table} "
                    f"({bound_column.foreign_key.target_column})"
                )
                self._sorted_constraints.append(constraint)
                self.foreign_key_constraints = self.foreign_key_constraints | {constraint}
        return tuple(bound)

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
        foreign_keys = tuple(
            (column.name, column.foreign_key.target_table, column.foreign_key.target_column)
            for column in self.columns
            if column.foreign_key is not None
        )
        (
            sql,
            _params,
            kind,
            _ordered,
            _bind_types,
            _bind_names,
            _result_types,
            _bind_error,
        ) = compile_expression(
            ("create_table", self.name, columns, primary_key, foreign_keys),
            _dialect_name(dialect),
        )
        return Compiled(sql, {}, kind, [], [], [], [])

    def compile_drop(self, dialect: str | None = None) -> Compiled:
        """Compile this table's ``DROP TABLE`` statement natively."""
        (
            sql,
            _params,
            kind,
            _ordered,
            _bind_types,
            _bind_names,
            _result_types,
            _bind_error,
        ) = compile_expression(("drop_table", self.name), _dialect_name(dialect))
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
    "Numeric": "numeric",
}


def _numeric_field(value: int | None) -> str:
    return "-" if value is None else str(value)


def _column_type_tag(type_obj: object) -> str | None:
    marker = type_obj if isinstance(type_obj, type) else type(type_obj)
    tag = _TYPE_TAGS.get(marker.__name__)
    if tag == "numeric" and isinstance(type_obj, Numeric):
        # The type's processing options ride along in the tag so compiled
        # bind/result metadata carries them into the native drivers.
        return ":".join(
            (
                "numeric",
                _numeric_field(type_obj.precision),
                _numeric_field(type_obj.scale),
                _numeric_field(type_obj.decimal_return_scale),
                "1" if type_obj.asdecimal else "0",
            )
        )
    return tag


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
    if type_tag.startswith("numeric"):
        from decimal import Decimal
        from numbers import Number

        return (
            type_tag
            if (isinstance(value, (Number, Decimal, str, bytes)))
            and not isinstance(value, (bool, complex))
            else None
        )
    if type_tag in {"string", "text"}:
        return type_tag if isinstance(value, str) else None
    if type_tag == "binary":
        return type_tag if isinstance(value, bytes) else None
    return None


def _type_node(type_obj: object) -> tuple[str, object]:
    """Map a Catalyst type marker to the native DDL type tag and its args."""
    marker = type_obj if isinstance(type_obj, type) else type(type_obj)
    tag = _TYPE_TAGS.get(marker.__name__)
    if tag is None:
        raise ArgumentError(f"Unsupported DDL column type '{marker.__name__}'")
    if isinstance(type_obj, type):
        return tag, None
    if isinstance(type_obj, Numeric):
        return tag, (type_obj.precision, type_obj.scale)
    return tag, getattr(type_obj, "length", None)


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
        bind_error: BaseException | None = None,
    ) -> None:
        self.string = sql
        self.params = params
        self.statement_kind = kind
        self._ordered = ordered
        self._bind_types = bind_types
        self._bind_names = bind_names
        self._result_types = result_types
        self.entity = entity
        # Literal bind processing is deferred to execution like SQLAlchemy:
        # a conversion that failed during compile is raised here on execute.
        self.bind_error = bind_error

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
    # ORM-side join specs, already serialized: ("left_outer_join", table,
    # alias, on) | ("join", table, alias|None, on) | ("implicit_from", table)
    # | ("left_outer_join_nested", assoc, assoc_alias, table, alias,
    # inner_on, outer_on).
    joins: tuple[tuple[object, ...], ...] = ()
    # Loader options (joinedload/selectinload) applied by Session.execute.
    loader_options: tuple[object, ...] = ()

    def where(self, *clauses: Column | Expression | bool) -> Select:
        if not clauses:
            return self
        nodes = tuple(
            Expression(("boolean_word", clause)) if isinstance(clause, bool) else clause
            for clause in clauses
        )
        predicate = nodes[0] if len(nodes) == 1 else and_(*nodes)  # noqa: PR01
        if self.predicate is not None:
            predicate = and_(cast("Column | Expression", self.predicate), predicate)
        return Select(
            self.columns,
            self.table,
            predicate,
            self.ordering,
            self.row_limit,
            self.row_offset,
            self.entity,
            self.joins,
            self.loader_options,
        )

    def order_by(self, *clauses: Column | Expression) -> Select:
        return Select(
            self.columns,
            self.table,
            cast("Column | Expression | None", self.predicate),
            self.ordering + clauses,
            self.row_limit,
            self.row_offset,
            self.entity,
            self.joins,
            self.loader_options,
        )

    def limit(self, value: int) -> Select:
        return Select(
            self.columns,
            self.table,
            cast("Column | Expression | None", self.predicate),
            self.ordering,
            value,
            self.row_offset,
            self.entity,
            self.joins,
            self.loader_options,
        )

    def offset(self, value: int) -> Select:
        return Select(
            self.columns,
            self.table,
            cast("Column | Expression | None", self.predicate),
            self.ordering,
            self.row_limit,
            value,
            self.entity,
            self.joins,
            self.loader_options,
        )

    def options(self, *opts: object) -> Select:
        """Attach ORM loader options (``joinedload``/``selectinload``)."""
        if not opts:
            return self
        return Select(
            self.columns,
            self.table,
            cast("Column | Expression | None", self.predicate),
            self.ordering,
            self.row_limit,
            self.row_offset,
            self.entity,
            self.joins,
            self.loader_options + tuple(opts),
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
        columns: tuple[Column | Expression, ...]
        if self.columns:
            columns = self.columns
        elif self.entity is not None:
            # ``select(Entity)`` emits the *mapper's* columns: a
            # single-table-inheritance parent selects only its own mapped
            # attributes even though subclass columns share its Table.
            attr_columns = getattr(getattr(self.entity, "__mapper__", None), "attr_columns", None)
            if isinstance(attr_columns, dict) and attr_columns:
                columns = tuple(cast("dict[str, Column]", attr_columns).values())
            else:
                columns = table.columns if table is not None else ()
        else:
            columns = table.columns if table is not None else ()
        nodes = tuple(_node(column) for column in columns)
        stored = cast("Column | Expression | None", self.predicate)
        predicate = None if stored is None else _node(stored)
        if self.entity is not None:
            # STI subclass selects are restricted to the identity subtree.
            poly = _polymorphic_criterion(self.entity)
            if poly is not None:
                predicate = poly.node if predicate is None else ("and", (predicate, poly.node))
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
        joins = tuple(self.joins)
        return (
            "select",
            nodes,
            table.name if table is not None else table_name,
            predicate,
            ordering,
            limit,
            offset,
            joins,
        )

    def compile(self, dialect: str | None = None) -> Compiled:
        name = _dialect_name(dialect)
        serialized = self._serialize(name)
        _check_node_depth(serialized)
        (
            sql,
            params,
            kind,
            ordered,
            bind_types,
            bind_names,
            result_types,
            bind_error,
        ) = _compiled_from_cache(serialized, name)
        return Compiled(
            sql,
            params,
            kind,
            ordered,
            bind_types,
            bind_names,
            result_types,
            self.entity,
            bind_error,
        )

    def __clause_element__(self) -> object:
        """Coerce to a SQLAlchemy ``Select`` for SA-mapped entities.

        Parity suites interop-test Catalyst statements against a real
        SQLAlchemy session; a bare ``select(SAModel)`` delegates to
        ``sqlalchemy.select`` so SA's coercion machinery accepts it.  Only
        unmodified entity selects are representable — Catalyst columns and
        predicates are not SQLAlchemy clause elements.
        """
        entity = self.entity
        if (
            entity is None
            or self.columns
            or self.predicate is not None
            or self.ordering
            or self.row_limit is not None
            or self.row_offset is not None
            or self.joins
            or self.loader_options
        ):
            raise ArgumentError(
                "Only a bare select(Entity) of a SQLAlchemy-mapped class can "
                "be coerced to a SQLAlchemy select"
            )
        mapper = getattr(entity, "__mapper__", None)
        if mapper is None or not type(mapper).__module__.startswith("sqlalchemy"):
            raise ArgumentError(
                f"Cannot coerce select() of '{entity.__name__}' to a SQLAlchemy "
                "select; the entity is not SQLAlchemy-mapped"
            )
        import sqlalchemy as sa  # dev/test dependency, imported lazily

        return sa.select(entity)

    def _execute_on_connection(
        self,
        connection: object,
        distilled_params: object,
        execution_options: object,
    ) -> object:
        """SQLAlchemy executability marker delegating to the SA coercion.

        SA's statement coercion only checks the attribute exists on the
        original object; execution itself runs the resolved SA select.
        """
        return cast(Any, self.__clause_element__())._execute_on_connection(  # pyright: ignore[reportUnknownMemberType]
            connection, distilled_params, execution_options
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
        (
            sql,
            params,
            kind,
            ordered,
            bind_types,
            bind_names,
            result_types,
            bind_error,
        ) = _compiled_from_cache(("insert", self.table.name, values), name)
        return Compiled(
            sql, params, kind, ordered, bind_types, bind_names, result_types, bind_error=bind_error
        )

    def __str__(self) -> str:
        return str(self.compile())


@dataclass(frozen=True, slots=True)
class Update:
    table: Table
    values_map: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())
    predicate: Column | Expression | None = None
    entity: type[object] | None = None
    exec_options: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())

    def values(self, values: Mapping[str, object] | None = None, **kwargs: object) -> Update:
        # Chained .values() calls accumulate, matching SQLAlchemy semantics.
        merged = dict(self.values_map)
        if values is not None:
            merged.update(values)
        merged.update(kwargs)
        return Update(
            self.table,
            merged,
            cast("Column | Expression | None", self.predicate),
            self.entity,
            self.exec_options,
        )

    def where(self, *clauses: Column | Expression | bool) -> Update:
        if not clauses:
            return self
        nodes = tuple(
            Expression(("boolean_word", clause)) if isinstance(clause, bool) else clause
            for clause in clauses
        )
        predicate = nodes[0] if len(nodes) == 1 else and_(*nodes)  # noqa: PR01
        if self.predicate is not None:
            predicate = and_(cast("Column | Expression", self.predicate), predicate)
        return Update(self.table, self.values_map, predicate, self.entity, self.exec_options)

    def execution_options(self, **options: object) -> Update:
        merged = dict(self.exec_options)
        merged.update(options)
        return Update(
            self.table,
            self.values_map,
            cast("Column | Expression | None", self.predicate),
            self.entity,
            merged,
        )

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
        stored = cast("Column | Expression | None", self.predicate)
        predicate = None if stored is None else _node(stored)
        (
            sql,
            params,
            kind,
            ordered,
            bind_types,
            bind_names,
            result_types,
            bind_error,
        ) = _compiled_from_cache(
            ("update", self.table.name, values, predicate), _dialect_name(dialect)
        )
        return Compiled(
            sql, params, kind, ordered, bind_types, bind_names, result_types, bind_error=bind_error
        )

    def __str__(self) -> str:
        return str(self.compile())


@dataclass(frozen=True, slots=True)
class Delete:
    table: Table
    predicate: Column | Expression | None = None
    entity: type[object] | None = None
    exec_options: Mapping[str, object] = field(default_factory=lambda: dict[str, object]())

    def where(self, *clauses: Column | Expression | bool) -> Delete:
        if not clauses:
            return self
        nodes = tuple(
            Expression(("boolean_word", clause)) if isinstance(clause, bool) else clause
            for clause in clauses
        )
        predicate = nodes[0] if len(nodes) == 1 else and_(*nodes)  # noqa: PR01
        if self.predicate is not None:
            predicate = and_(cast("Column | Expression", self.predicate), predicate)
        return Delete(self.table, predicate, self.entity, self.exec_options)

    def execution_options(self, **options: object) -> Delete:
        merged = dict(self.exec_options)
        merged.update(options)
        return Delete(
            self.table, cast("Column | Expression | None", self.predicate), self.entity, merged
        )

    def compile(self, dialect: str | None = None) -> Compiled:
        stored = cast("Column | Expression | None", self.predicate)
        predicate = None if stored is None else _node(stored)
        (
            sql,
            params,
            kind,
            ordered,
            bind_types,
            bind_names,
            result_types,
            bind_error,
        ) = _compiled_from_cache(("delete", self.table.name, predicate), _dialect_name(dialect))
        return Compiled(
            sql, params, kind, ordered, bind_types, bind_names, result_types, bind_error=bind_error
        )

    def __str__(self) -> str:
        return str(self.compile())


def _entity_table(value: object) -> Table | None:
    """Resolve a mapped declarative class to its ``Table``; ``None`` otherwise."""
    table = getattr(value, "__table__", None)
    return table if isinstance(value, type) and isinstance(table, Table) else None


def _polymorphic_criterion(entity: object) -> Expression | None:
    """``type IN (identity subtree)`` filter for a mapped subclass, or ``None``.

    SQLAlchemy restricts single-table-inheritance subclass SELECTs and bulk
    DML to the queried mapper's own polymorphic identity plus every
    descendant identity; base-class statements stay unfiltered.
    """
    mapper = getattr(entity, "__mapper__", None)
    if mapper is None or getattr(mapper, "inherits", None) is None:
        return None
    discriminator = getattr(mapper, "polymorphic_on", None)
    if not isinstance(discriminator, Column):
        return None
    identities = [
        sub.polymorphic_identity
        for sub in getattr(mapper, "self_and_descendants", ())
        if getattr(sub, "polymorphic_identity", None) is not None
    ]
    if not identities:
        return None
    return discriminator.in_(identities)


def select(*columns: Column | Expression | Table | type[object]) -> Select:
    if len(columns) == 1:
        first = columns[0]
        if isinstance(first, type):
            # Keep the class as ``entity`` even when it is not Catalyst-mapped:
            # ``__clause_element__`` can still coerce ``select(SAModel)`` for
            # interop parity suites running it on a real SQLAlchemy session.
            return Select((), _entity_table(first), entity=first)
        table = first if isinstance(first, Table) else None
        return Select(
            tuple(column for column in columns if isinstance(column, (Column, Expression))),
            table,
        )
    return Select(tuple(column for column in columns if isinstance(column, (Column, Expression))))


def _dml_table(table: Table | type[object], verb: str) -> tuple[Table, type[object] | None]:
    if isinstance(table, Table):
        return table, None
    resolved = _entity_table(table)
    if resolved is None:
        raise ArgumentError(f"{verb}() expected a Table or mapped declarative class")
    return resolved, table


def insert(table: Table | type[object]) -> Insert:
    return Insert(_dml_table(table, "insert")[0])


def update(table: Table | type[object]) -> Update:
    resolved, entity = _dml_table(table, "update")
    return Update(resolved, entity=entity)


def delete(table: Table | type[object]) -> Delete:
    resolved, entity = _dml_table(table, "delete")
    return Delete(resolved, entity=entity)
