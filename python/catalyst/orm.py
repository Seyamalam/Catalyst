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
from collections.abc import Callable, Iterable, Iterator, Set
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

from ._catalyst import (
    ArgumentError,
    InvalidRequestError,
    NativeUow,
    PendingRollbackError,
    UnmappedInstanceError,
)
from .engine import (
    Connection,
    CursorResult,
    Engine,
    SavepointTransaction,
    ScalarResult,
    _statement_error,  # pyright: ignore[reportPrivateUsage]
)
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
    select,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .engine import TextClause

_T = TypeVar("_T")

# ``NativeUow.status`` values kept in sync with ``src/session.rs``.
_STATUS_PENDING = 1
_STATUS_PERSISTENT = 2
_STATUS_DELETED = 3


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
        # Attribute-name -> bound Column lookup for unit-of-work SQL building.
        self.attr_columns = dict(zip(attrs, table.columns, strict=True))
        self.pk_column = next((column for column in table.columns if column.primary_key), None)
        self.pk_attr = next(
            (attr for attr, column in self.attr_columns.items() if column.primary_key), None
        )


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

    def __setattr__(self, name: str, value: Any) -> None:
        object.__setattr__(self, name, value)
        # Set-on-write dirty tracking: notify the owning unit of work after
        # the value lands (like SQLAlchemy's ``_sa_instance_state`` hook).
        uow = self.__dict__.get("_catalyst_state")
        mapper = getattr(type(self), "__mapper__", None)
        if uow is not None and mapper is not None and name in mapper.attrs:
            uow.attr_set(self, name)


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
        # Bind the mapped attribute name onto the class-level Column so its
        # ``__get__`` resolves the correct ``__dict__`` key on instances (a
        # column name can differ from the attribute name).
        object.__setattr__(column, "key", attr)
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


def _mapper_of(instance_or_class: object) -> Mapper[Any]:
    """Return the ``__mapper__`` attached to a mapped class or instance."""
    cls = instance_or_class if isinstance(instance_or_class, type) else type(instance_or_class)
    return cast("Mapper[Any]", getattr(cls, "__mapper__"))  # noqa: B009


def _require_mapper(instance: object) -> Mapper[Any]:
    """Return the mapper for ``instance`` or raise ``UnmappedInstanceError``."""
    mapper = getattr(type(instance), "__mapper__", None)
    if mapper is None:
        cls = type(instance)
        raise UnmappedInstanceError(f"Class '{cls.__module__}.{cls.__qualname__}' is not mapped")
    return cast("Mapper[Any]", mapper)


def _state_of(instance: object) -> NativeUow | None:
    """Return the unit of work recorded on ``instance`` by a session, if any."""
    instance_dict = getattr(instance, "__dict__", None)
    if instance_dict is None:
        return None
    state = instance_dict.get("_catalyst_state")
    return state if isinstance(state, NativeUow) else None


def _expire_instance(instance: object) -> None:
    """Drop mapped attributes from ``__dict__`` so the next access reloads."""
    for attr in _mapper_of(instance).attrs:
        instance.__dict__.pop(attr, None)


class _StateSet(Set[object]):
    """Insertion-ordered, identity-keyed snapshot of a session collection.

    Mirrors SQLAlchemy's ``IdentitySet``: membership and iteration are by
    object identity (``id``), not ``__eq__``.
    """

    __slots__ = ("_items", "_ids")

    def __init__(self, items: list[object]) -> None:
        self._items = tuple(items)
        self._ids = {id(item) for item in self._items}

    def __contains__(self, item: object) -> bool:
        return id(item) in self._ids

    def __iter__(self) -> Iterator[object]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"{{{', '.join(repr(item) for item in self._items)}}}"


class Session:
    """SQLAlchemy-style unit of work over a checked-out connection.

    The identity map, per-object state (pending/dirty/deleted), stored primary
    keys, and hydration reconciliation all live in the native ``NativeUow``
    registry — one instance per session.  This class orchestrates the public
    semantics on top: autoflush, ``expire_on_commit``, exception types, and the
    transaction lifecycle.  Flush SQL is compiled per object but crosses the
    FFI boundary once via ``NativeConnection.execute_flush``.
    """

    def __init__(
        self,
        bind: Engine | Connection,
        autoflush: bool = True,
        expire_on_commit: bool = True,
    ) -> None:
        self._owns_connection = isinstance(bind, Engine)
        self._connection = bind.connect() if isinstance(bind, Engine) else bind
        self._closed = False
        self.autoflush = autoflush
        self.expire_on_commit = expire_on_commit
        self._uow = NativeUow(self)
        self._txn_active = False
        self._pending_rollback = False

    # -- state collections -------------------------------------------------

    @property
    def new(self) -> Set[object]:
        """Instances pending insertion, in add order."""
        return _StateSet(self._uow.new_objects())

    @property
    def dirty(self) -> Set[object]:
        """Persistent instances with unflushed attribute writes."""
        return _StateSet(self._uow.dirty_objects())

    @property
    def deleted(self) -> Set[object]:
        """Instances marked for deletion at the next flush."""
        return _StateSet(self._uow.deleted_objects())

    def is_modified(self, instance: object) -> bool:
        """Whether ``instance`` is pending or holds unflushed dirty attributes."""
        _require_mapper(instance)
        uow = _state_of(instance)
        if uow is not None and uow.is_managed(instance):
            return uow.is_modified(instance)
        # Transient and detached objects count as modified like SQLAlchemy.
        return True

    # -- guards ------------------------------------------------------------

    def _check_active(self) -> None:
        if self._closed:
            raise RuntimeError("Session is closed")
        if self._pending_rollback:
            raise PendingRollbackError(
                "This Session's transaction has been rolled back due to a previous "
                "exception during flush. To begin a new transaction with this "
                "Session, first issue Session.rollback()."
            )

    def _mark_active(self) -> None:
        self._txn_active = True

    @staticmethod
    def _raise_bind_error(compiled: Compiled) -> None:
        if compiled.bind_error is not None:
            raise _statement_error(compiled.bind_error, compiled.string, ())

    # -- add / delete ------------------------------------------------------

    def add(self, instance: object) -> None:
        """Attach ``instance`` to the session as pending insertion."""
        self._check_active()
        mapper = _require_mapper(instance)
        uow = _state_of(instance)
        if uow is not None and uow.is_managed(instance):
            if uow is self._uow:
                # Re-adding a persistent object is a no-op; re-adding one that
                # is marked deleted cancels the pending delete.
                if uow.status(instance) == _STATUS_DELETED:
                    uow.un_delete(instance)
                return
            raise InvalidRequestError(
                f"Object '{instance}' is already attached to session "
                f"'{id(uow.session) & 0xFF}' (this is '{id(self) & 0xFF}')"
            )
        self._uow.attach_pending(instance, type(instance), list(mapper.attrs))
        instance._catalyst_state = self._uow  # type: ignore[attr-defined]
        self._mark_active()

    def add_all(self, instances: Iterable[object]) -> None:
        """Add each instance in order."""
        for instance in instances:
            self.add(instance)

    def delete(self, instance: object) -> None:
        """Mark a persistent instance for deletion at the next flush."""
        self._check_active()
        _require_mapper(instance)
        uow = _state_of(instance)
        if uow is None:
            raise InvalidRequestError(f"Instance '{instance}' is not persisted")
        if not uow.is_managed(instance):
            return
        if uow is not self._uow:
            raise InvalidRequestError(f"Instance '{instance}' is not within this session")
        if uow.status(instance) != _STATUS_PERSISTENT:
            raise InvalidRequestError(f"Instance '{instance}' is not persisted")
        uow.mark_deleted(instance)
        self._mark_active()

    # -- flush -------------------------------------------------------------

    def flush(self) -> None:
        """Emit batched INSERT/UPDATE/DELETE for the unit of work.

        Ops are gathered Python-side and executed in one
        ``NativeConnection.execute_flush`` crossing inside the session's
        transaction; returned ``lastrowid`` values become primary keys.
        """
        self._check_active()
        self._mark_active()
        uow = self._uow
        if not uow.has_changes():
            return
        ops: list[tuple[str, list[object], list[str | None], bool]] = []
        persisted: list[tuple[object, object]] = []
        cleaned: list[object] = []
        dropped: list[object] = []
        for obj in uow.new_objects():
            mapper = _mapper_of(obj)
            pk_attr = mapper.pk_attr
            pk_value = obj.__dict__.get(pk_attr) if pk_attr is not None else None
            values: dict[str, object] = {}
            for attr in mapper.attrs:
                column = mapper.attr_columns[attr]
                if attr == pk_attr and pk_value is None:
                    # Autoincrement PKs are omitted so SQLite assigns them.
                    continue
                values[column.name] = obj.__dict__.get(attr)
            compiled = Insert(mapper.local_table).values(values).compile()
            self._raise_bind_error(compiled)
            ops.append(
                (
                    compiled.string,
                    compiled._ordered,
                    compiled._bind_types,
                    pk_value is None and pk_attr is not None,
                )
            )
            persisted.append((obj, pk_value))
        for obj in uow.dirty_objects():
            mapper = _mapper_of(obj)
            pk = uow.pk_of(obj)
            values = {
                mapper.attr_columns[attr].name: obj.__dict__.get(attr)
                for attr in uow.dirty_attrs_of(obj)
            }
            compiled = (
                Update(mapper.local_table).values(values).where(mapper.pk_column == pk).compile()
            )
            self._raise_bind_error(compiled)
            ops.append((compiled.string, compiled._ordered, compiled._bind_types, False))
            cleaned.append(obj)
        for obj in uow.deleted_objects():
            mapper = _mapper_of(obj)
            pk = uow.pk_of(obj)
            compiled = Delete(mapper.local_table).where(mapper.pk_column == pk).compile()
            self._raise_bind_error(compiled)
            ops.append((compiled.string, compiled._ordered, compiled._bind_types, False))
            dropped.append(obj)
        try:
            assigned = self._connection._native.execute_flush(ops)
        except Exception:
            # A failed flush puts the session in partial rollback; every
            # operation must raise PendingRollbackError until rollback().
            self._pending_rollback = True
            raise
        final: list[tuple[object, object]] = []
        for index, (obj, pk_value) in enumerate(persisted):
            mapper = _mapper_of(obj)
            pk = pk_value if pk_value is not None else assigned[index]
            if mapper.pk_attr is not None:
                object.__setattr__(obj, mapper.pk_attr, pk)
            final.append((obj, pk))
        uow.flush_transition(final, cleaned, dropped)

    # -- transactions ------------------------------------------------------

    def begin(self) -> None:
        """Begin an explicit transaction on this session."""
        self._check_active()
        if self._txn_active:
            raise InvalidRequestError("A transaction is already begun on this Session.")
        self._txn_active = True

    def begin_nested(self) -> SavepointTransaction:
        """Create a SAVEPOINT within the session's transaction."""
        self._check_active()
        self._mark_active()
        return self._connection.begin_nested()

    def in_transaction(self) -> bool:
        """Whether the session currently has an open transaction."""
        return self._txn_active

    def commit(self) -> None:
        """Flush pending work then COMMIT the session's transaction."""
        self._check_active()
        try:
            self.flush()
            self._connection.commit()
        except Exception:
            self._pending_rollback = True
            raise
        self._txn_active = False
        self._uow.commit_transition()
        if self.expire_on_commit:
            self._expire_managed()

    def _expire_managed(self) -> None:
        for obj in self._uow.expire_all():
            _expire_instance(obj)

    def rollback(self) -> None:
        """Roll back the open transaction and reset unit-of-work state."""
        if self._closed:
            return
        self._pending_rollback = False
        self._connection.rollback()
        for obj in self._uow.rollback():
            _expire_instance(obj)
        self._txn_active = False

    def expire(self, instance: object) -> None:
        """Expire all mapped attributes of ``instance``."""
        self._check_active()
        _require_mapper(instance)
        uow = _state_of(instance)
        if uow is not None and uow is self._uow and uow.is_managed(instance):
            uow.expire(instance)
            _expire_instance(instance)

    def expire_all(self) -> None:
        """Expire every persistent instance managed by this session."""
        self._check_active()
        self._expire_managed()

    # -- reads -------------------------------------------------------------

    def get(self, entity: type[_T], ident: object) -> _T | None:
        """Return the instance for primary key ``ident``, or ``None``.

        Autoflushes first; the identity map is consulted before the database
        so repeated calls return the identical object.
        """
        self._check_active()
        self._mark_active()
        if self.autoflush:
            self.flush()
        cached = self._uow.identity_get(entity, ident)
        if cached is not None:
            return cast(_T, cached)
        mapper = getattr(entity, "__mapper__", None)
        if mapper is None or cast("Mapper[Any]", mapper).pk_column is None:
            raise UnmappedInstanceError(
                f"Class '{entity.__module__}.{entity.__qualname__}' is not mapped"
            )
        mapper = cast("Mapper[Any]", mapper)
        compiled = select(mapper.local_table).where(mapper.pk_column == ident).compile()
        self._raise_bind_error(compiled)
        row = self._connection.execute(compiled).fetchone()
        if row is None:
            return None
        instance = entity.__new__(entity)
        for attr, value in zip(mapper.attrs, row, strict=True):
            object.__setattr__(instance, attr, value)
        self._uow.attach_persistent(instance, entity, list(mapper.attrs), ident)
        instance._catalyst_state = self._uow  # type: ignore[attr-defined]
        return instance

    def refresh(self, instance: object) -> None:
        """Reload all mapped attributes of ``instance`` by primary key."""
        self._check_active()
        _require_mapper(instance)
        uow = _state_of(instance)
        if uow is None or uow is not self._uow or uow.status(instance) != _STATUS_PERSISTENT:
            raise InvalidRequestError(
                f"Instance '{instance}' is not persistent within this Session"
            )
        if self.autoflush:
            self.flush()
        self._mark_active()
        mapper = _mapper_of(instance)
        pk = uow.pk_of(instance)
        compiled = select(mapper.local_table).where(mapper.pk_column == pk).compile()
        self._raise_bind_error(compiled)
        row = self._connection.execute(compiled).fetchone()
        if row is not None:
            for attr, value in zip(mapper.attrs, row, strict=True):
                object.__setattr__(instance, attr, value)
        uow.mark_clean(instance)

    def _load_expired(self, instance: object, attr: str) -> None:
        """Refresh ``instance`` by primary key; called from ``Column.__get__``.

        Like SQLAlchemy's ``_load_expired``, one SELECT repopulates every
        attribute in the expired set at once and clears the whole set —
        sibling attributes stay consistent with the row snapshot rather than
        reloading separately.
        """
        if self._closed:
            return
        if self._pending_rollback:
            raise PendingRollbackError(
                "This Session's transaction has been rolled back due to a previous "
                "exception during flush. To begin a new transaction with this "
                "Session, first issue Session.rollback()."
            )
        uow = _state_of(instance)
        if uow is None or uow is not self._uow:
            return
        mapper = _mapper_of(instance)
        if attr not in mapper.attr_columns:
            return
        pk = uow.pk_of(instance)
        if pk is None:
            return
        if self.autoflush:
            self.flush()
        compiled = select(mapper.local_table).where(mapper.pk_column == pk).compile()
        self._raise_bind_error(compiled)
        row = self._connection.execute(compiled).fetchone()
        self._mark_active()
        for expired in uow.expired_attrs_of(instance):
            value = row[mapper.attrs.index(expired)] if row is not None else None
            object.__setattr__(instance, expired, value)
        uow.mark_loaded(instance)

    def execute(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> CursorResult:
        """Execute a statement; ``select(Entity)`` rows hydrate to ``(entity,)``."""
        self._check_active()
        self._mark_active()
        if self.autoflush:
            self.flush()
        result = self._connection.execute(statement, parameters)
        entity = getattr(statement, "entity", None)
        if entity is None:
            return result
        return CursorResult(result.native_result, entity=entity, uow=self._uow)

    def scalars(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> ScalarResult:
        """Execute a statement and return first-column values as a scalar result."""
        return ScalarResult(self.execute(statement, parameters))

    def close(self) -> None:
        """Roll back uncommitted work and release the owned connection.

        Like SQLAlchemy, closing detaches objects rather than expiring them:
        values already loaded stay readable on the instance.
        """
        if self._closed:
            return
        self._pending_rollback = False
        try:
            self._connection.rollback()
        except Exception:
            pass
        self._uow.clear()
        if self._owns_connection:
            self._connection.close()
        self._txn_active = False
        self._closed = True

    def __enter__(self) -> Session:
        if self._closed:
            raise RuntimeError("Session is closed")
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()
