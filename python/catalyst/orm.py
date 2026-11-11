"""Declarative ORM mapping: ``Mapped[T]``, ``mapped_column``, and ``DeclarativeBase``.

Declarative classes bind Python annotations to Core :class:`Table` metadata so
that ``select(User)`` compiles to the same SQL as ``select(User.__table__)``.
Hydration of buffered result rows into instances happens natively in Rust
through :meth:`NativeResult.hydrate`, keeping one FFI crossing per batch.
"""

from __future__ import annotations

import datetime
import decimal
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    TypeVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from typing_extensions import dataclass_transform

from ._catalyst import ArgumentError
from .engine import Connection, CursorResult, Engine, ScalarResult
from .expression import (
    Boolean,
    Column,
    Compiled,
    Date,
    DateTime,
    Delete,
    Double,
    Insert,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Select,
    String,
    Table,
    Update,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .engine import TextClause

_T = TypeVar("_T")


class Mapped(Generic[_T]):
    """Annotation marker declaring a mapped attribute's Python type.

    ``Mapped`` exists only as an annotation; mapped class attributes hold
    :class:`Column` objects at runtime.  The descriptor methods are defined for
    parity with the type stubs — they are never invoked because ``Mapped`` is
    never assigned as a class attribute.
    """

    def __get__(self, instance: object, owner: object) -> Any:
        raise NotImplementedError

    def __set__(self, instance: object, value: object) -> None:
        raise NotImplementedError

    def __delete__(self, instance: object) -> None:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class MappedColumn(Mapped[_T]):
    """Declarative column specification produced by :func:`mapped_column`."""

    type_: object = None
    primary_key: bool = False
    nullable: bool | None = None
    unique: bool = False
    default: object = None
    default_factory: Callable[[], object] | None = None
    server_default: str | None = None
    name: str | None = None
    init: bool = True
    kw_only: bool = False


def mapped_column(
    name_or_type: object = None,
    type_: object = None,
    *,
    primary_key: bool = False,
    nullable: bool | None = None,
    unique: bool = False,
    default: object = None,
    default_factory: Callable[[], object] | None = None,
    server_default: str | None = None,
    name: str | None = None,
    init: bool = True,
    kw_only: bool = False,
) -> MappedColumn[Any]:
    """Describe a declarative column, mirroring ``sqlalchemy.orm.mapped_column``.

    The first positional argument may be a SQL type marker
    (``mapped_column(String(30))``) or a column name string
    (``mapped_column("user_id")``).
    """
    resolved_name = name
    resolved_type = type_
    if isinstance(name_or_type, str):
        resolved_name = name_or_type if resolved_name is None else resolved_name
    elif name_or_type is not None:
        if resolved_type is not None:
            raise ArgumentError("mapped_column() received two type arguments")
        resolved_type = name_or_type
    return MappedColumn(
        type_=resolved_type,
        primary_key=primary_key,
        nullable=nullable,
        unique=unique,
        default=default,
        default_factory=default_factory,
        server_default=server_default,
        name=resolved_name,
        init=init,
        kw_only=kw_only,
    )


_TYPE_MARKERS: dict[type[object], type[object]] = {
    bool: Boolean,
    int: Integer,
    float: Double,
    str: String,
    bytes: LargeBinary,
    decimal.Decimal: Numeric,
    datetime.datetime: DateTime,
    datetime.date: Date,
}


class Mapper(Generic[_T]):
    """Runtime mapping record attached to a declarative class as ``__mapper__``."""

    def __init__(
        self,
        class_: type[_T],
        table: Table,
        attrs: tuple[str, ...],
        specs: dict[str, MappedColumn[Any] | None],
    ) -> None:
        self.class_ = class_
        self.local_table = table
        self.attrs = attrs
        self.columns = table.columns
        self.specs = specs


@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(MappedColumn, mapped_column),
)
class DeclarativeBase:
    """Base class for declarative table mappings.

    Subclasses without ``__tablename__`` act as new mapping bases and receive a
    fresh :class:`MetaData`; subclasses with ``__tablename__`` are mapped into
    the nearest base's metadata.
    """

    metadata: ClassVar[MetaData]
    __table__: ClassVar[Table]
    __mapper__: ClassVar[Mapper[Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _map_declarative_class(cls)

    def __init__(self, **kwargs: Any) -> None:
        specs = type(self).__mapper__.specs
        for attr, spec in specs.items():
            if attr in kwargs:
                value = kwargs.pop(attr)
            elif spec is not None and spec.default_factory is not None:
                value = spec.default_factory()
            elif spec is not None and spec.default is not None:
                value = spec.default
            else:
                value = None
            setattr(self, attr, value)
        if kwargs:
            name = next(iter(kwargs))
            raise TypeError(f"'{name}' is an invalid keyword argument for {type(self).__name__}")


def declarative_base() -> type[DeclarativeBase]:
    """Create a fresh declarative base class carrying its own ``MetaData``."""

    class Base(DeclarativeBase):
        pass

    return Base


def _unwrap_optional(annotation: object) -> tuple[bool, object]:
    """Return ``(True, inner)`` for ``Optional[T]``/``T | None`` annotations."""
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = get_args(annotation)
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and len(non_none) != len(args):
            return True, non_none[0]
    return False, annotation


def _map_declarative_class(cls: type[object]) -> None:
    """Build a ``Table`` and ``Mapper`` for a ``DeclarativeBase`` subclass."""
    cls_any = cast(Any, cls)
    tablename = cls.__dict__.get("__tablename__")
    if tablename is None:
        # Intermediate base: give it fresh metadata unless one is inherited.
        if not isinstance(getattr(cls, "metadata", None), MetaData):
            cls_any.metadata = MetaData()
        return
    metadata = getattr(cls, "metadata", None)
    if not isinstance(metadata, MetaData):
        metadata = MetaData()
        cls_any.metadata = metadata
    hints = get_type_hints(cls)
    attrs: list[str] = []
    specs: dict[str, MappedColumn[Any] | None] = {}
    columns: list[Column] = []
    for name in cls.__dict__.get("__annotations__", {}):
        if name.startswith("_") or name in {"metadata"}:
            continue
        hint = hints.get(name)
        if get_origin(hint) is ClassVar:
            continue
        spec_value = cls.__dict__.get(name)
        spec = cast(
            "MappedColumn[Any] | None",
            spec_value if isinstance(spec_value, MappedColumn) else None,
        )
        if hint is not None and get_origin(hint) is Mapped:
            args = get_args(hint)
            optional: bool | None
            optional, inner = _unwrap_optional(args[0] if args else None)
        elif spec is None:
            if hint is None:
                continue
            raise ArgumentError(
                f"Attribute '{name}' on {cls.__name__} uses a non-Mapped annotation; "
                "declarative attributes must be annotated with Mapped[...]"
            )
        else:
            optional, inner = None, None
        columns.append(_column_for_attr(cls, name, spec, inner, optional))
        attrs.append(name)
        specs[name] = spec
    # Bare mapped_column() assignments carry no annotation.
    for name, value in cls.__dict__.items():
        if name.startswith("_") or not isinstance(value, MappedColumn) or name in specs:
            continue
        columns.append(_column_for_attr(cls, name, cast("MappedColumn[Any]", value), None, None))
        attrs.append(name)
        specs[name] = value
    if not columns:
        raise ArgumentError(f"{cls.__name__} declares __tablename__ but no mapped attributes")
    table = Table(str(tablename), metadata, *columns)
    cls_any.__table__ = table
    cls_any.__mapper__ = Mapper(cls, table, tuple(attrs), specs)
    for attr, column in zip(attrs, table.columns, strict=True):
        setattr(cls, attr, column)


def _column_for_attr(
    cls: type[object],
    attr: str,
    spec: MappedColumn[Any] | None,
    inner: object,
    optional: bool | None,
) -> Column:
    """Build the unbound ``Column`` for one mapped attribute."""
    type_marker = spec.type_ if spec is not None else None
    if type_marker is None:
        if not isinstance(inner, type):
            raise ArgumentError(
                f"Cannot infer a SQL type for attribute '{attr}' on {cls.__name__}; "
                "use mapped_column() with an explicit type"
            )
        type_marker = _TYPE_MARKERS.get(inner)
        if type_marker is None:
            raise ArgumentError(
                f"Cannot map Python type {inner!r} for attribute '{attr}' on "
                f"{cls.__name__} to a Catalyst column type"
            )
    nullable = spec.nullable if spec is not None else None
    if nullable is None:
        nullable = optional
    return Column(
        spec.name if spec is not None and spec.name is not None else attr,
        type_marker,
        primary_key=spec.primary_key if spec is not None else False,
        nullable=nullable,
        unique=spec.unique if spec is not None else False,
        default=spec.default if spec is not None else None,
        server_default=spec.server_default if spec is not None else None,
    )


class Session:
    """Minimal ORM facade providing SQLAlchemy's entity-hydration read path.

    ``Session.execute``/``Session.scalars`` hydrate ``select(Entity)`` results
    into mapped instances in one batched Rust call.  Unit-of-work state,
    ``add``/``commit`` semantics, and identity maps arrive with milestone 4.
    """

    def __init__(self, bind: Engine | Connection) -> None:
        self._owns_connection = isinstance(bind, Engine)
        self._connection = bind.connect() if isinstance(bind, Engine) else bind
        self._closed = False

    def execute(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CursorResult:
        """Execute a statement; ``select(Entity)`` rows hydrate to ``(entity,)``."""
        if self._closed:
            raise RuntimeError("Session is closed")
        result = self._connection.execute(statement, parameters)
        entity = getattr(statement, "entity", None)
        if entity is None:
            return result
        return CursorResult(result.native_result, entity=entity)

    def scalars(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> ScalarResult:
        """Execute a statement and return first-column values as a scalar result."""
        return ScalarResult(self.execute(statement, parameters))

    def close(self) -> None:
        """Release the underlying connection if this session owns it."""
        if not self._closed:
            if self._owns_connection:
                self._connection.close()
            self._closed = True

    def __enter__(self) -> Session:
        if self._closed:
            raise RuntimeError("Session is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
