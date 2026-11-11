"""Declarative ORM mapping: ``Mapped[T]``, ``mapped_column``, ``relationship``.

Declarative classes bind Python annotations to Core :class:`Table` metadata so
that ``select(User)`` compiles to the same SQL as ``select(User.__table__)``.
Hydration of buffered result rows into instances happens natively in Rust
through :meth:`NativeResult.hydrate`, keeping one FFI crossing per batch.

Relationship declarations are configured lazily (like SQLAlchemy's
``configure_mappers``): string annotations such as ``Mapped[list["Child"]]``
register pending declarations at class creation and resolve against the
declarative class registry once the target exists.
"""

from __future__ import annotations

import datetime
import decimal
import re
import sys
import types
import typing
import weakref
from collections.abc import (
    Callable,
    Collection,
    Iterable,
    Iterator,
    MutableSequence,
    Sequence,
    Set,
)
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Generic,
    SupportsIndex,
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
    Expression,
    ForeignKey,
    Insert,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Select,
    String,
    Table,
    Update,
    _column_type_tag,  # pyright: ignore[reportPrivateUsage]
    select,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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
    foreign_key: ForeignKey | None = None


def mapped_column(
    name_or_type: object = None,
    type_: object = None,
    *args: object,
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
    (``mapped_column(String(30))``), a column name string
    (``mapped_column("user_id")``), or a :class:`ForeignKey`
    (``mapped_column(ForeignKey("parent.id"))``).
    """
    resolved_name = name
    resolved_type = type_
    foreign_key = None
    if isinstance(name_or_type, ForeignKey):
        foreign_key = name_or_type
        name_or_type = None
    for arg in args:
        if isinstance(arg, ForeignKey) and foreign_key is None:
            foreign_key = arg
        else:
            raise ArgumentError(f"mapped_column() got an unexpected positional argument {arg!r}")
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
        foreign_key=foreign_key,
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
        # Relationship state: configured properties plus declarations still
        # awaiting resolution (deferred like SQLAlchemy's configure_mappers).
        self.relationships: dict[str, RelationshipProperty] = {}
        self.pending_relationships: list[Relationship[Any]] = []
        # Declared relationship attribute names (configured or not) — used by
        # expiry and ``__init__`` kwargs handling.
        self.relationship_attrs: tuple[str, ...] = ()
        # Name -> mapped class registry shared across one declarative base.
        self.class_registry: dict[str, type[object]] = {}


# -- relationships ---------------------------------------------------------

_MANYTOONE = "manytoone"
_ONETOMANY = "onetomany"

# SQLAlchemy's ``all`` cascade token expands to these operations; the separate
# ``delete-orphan`` token opts into deleting removed children.
_CASCADE_ALL_TOKENS = frozenset({"save-update", "merge", "refresh-expire", "expunge", "delete"})


def _parse_cascade(text: object) -> frozenset[str]:
    """Parse a SQLAlchemy cascade string such as ``"all, delete-orphan"``."""
    if text is None or isinstance(text, bool):
        return _CASCADE_ALL_TOKENS if text is True else frozenset()
    tokens = {part.strip().lower() for part in str(text).split(",") if part.strip()}
    if "none" in tokens:
        tokens.discard("none")
    if "all" in tokens:
        tokens.discard("all")
        tokens |= _CASCADE_ALL_TOKENS
    return frozenset(tokens)


class RelationshipProperty:
    """A configured relationship: resolved target, direction, and join columns.

    ``local_*`` refers to the attribute/column on the declaring side that
    feeds the join; ``remote_*`` to the matching target-side attribute/column.
    For ``manytoone`` the local side carries the foreign key; for
    ``onetomany`` the remote side does.
    """

    __slots__ = (
        "key",
        "mapper",
        "entity",
        "target_mapper",
        "direction",
        "uselist",
        "lazy",
        "cascade",
        "back_populates",
        "sibling",
        "descriptor",
        "local_attr",
        "local_col",
        "remote_attr",
        "remote_col",
    )

    key: str
    mapper: Mapper[Any]
    entity: type[object]
    target_mapper: Mapper[Any]
    direction: str
    uselist: bool
    lazy: str
    cascade: frozenset[str]
    back_populates: str | None
    sibling: RelationshipProperty | None
    descriptor: Relationship[Any]
    local_attr: str
    local_col: Column
    remote_attr: str
    remote_col: Column


class Relationship(Generic[_T]):
    """Declarative relationship marker installed as a class attribute.

    Acts as the descriptor for the mapped attribute: class access yields an
    :class:`InstrumentedAttribute`, instance access returns the stored value
    or lazily loads it through the owning session.
    """

    __slots__ = (
        "argument",
        "back_populates",
        "backref",
        "lazy",
        "uselist",
        "cascade",
        "key",
        "declaring_cls",
        "container",
        "target_hint",
        "configured",
    )

    def __init__(
        self,
        argument: object = None,
        *,
        back_populates: str | None = None,
        backref: str | None = None,
        lazy: str = "select",
        uselist: bool | None = None,
        cascade: object = "merge,save-update",
    ) -> None:
        self.argument = argument
        self.back_populates = back_populates
        self.backref = backref
        self.lazy = lazy
        self.uselist = uselist
        self.cascade = _parse_cascade(cascade)
        self.key: str | None = None
        self.declaring_cls: type[object] | None = None
        self.container: str | None = None
        self.target_hint: object = None
        self.configured: RelationshipProperty | None = None

    def __get__(self, instance: object, owner: object = None) -> object:
        if instance is None:
            return InstrumentedAttribute(
                cast("type[object]", owner) if isinstance(owner, type) else self.declaring_cls,
                self,
            )
        prop = self.configure()
        instance_dict = getattr(instance, "__dict__", None)
        if instance_dict is not None and prop.key in instance_dict:
            return instance_dict[prop.key]
        return _load_relationship(instance, prop)

    def __set__(self, instance: object, value: object) -> None:
        prop = self.configure()
        if prop.uselist:
            _set_collection(instance, prop, value)
        else:
            _set_scalar_rel(instance, prop, value)

    def __delete__(self, instance: object) -> None:
        self.__set__(instance, None)

    @property
    def property(self) -> RelationshipProperty:
        """The configured :class:`RelationshipProperty` for this declaration."""
        return self.configure()

    def configure(self) -> RelationshipProperty:
        """Resolve this declaration into its :class:`RelationshipProperty`."""
        if self.configured is None:
            configure_mappers()
        if self.configured is None:
            owner = self.declaring_cls
            target = self.argument if isinstance(self.argument, str) else self.target_hint
            raise InvalidRequestError(
                f"Mapper '{owner.__name__ if owner else '?'}.{self.key}' relationship "
                f"target '{target}' could not be resolved to a mapped class"
            )
        return self.configured


def relationship(
    argument: object = None,
    *,
    back_populates: str | None = None,
    backref: str | None = None,
    lazy: str = "select",
    uselist: bool | None = None,
    cascade: object = "merge,save-update",
    **kw: object,
) -> Relationship[Any]:
    """Declare a relationship between mapped classes, mirroring SQLAlchemy's.

    ``argument`` may be a mapped class or its name; when omitted the target is
    resolved from the ``Mapped[...]`` annotation once the class exists
    (deferred mapper configuration).
    """
    if kw:
        raise ArgumentError(f"relationship() got unexpected keyword argument(s): {sorted(kw)}")
    return Relationship(
        argument,
        back_populates=back_populates,
        backref=backref,
        lazy=lazy,
        uselist=uselist,
        cascade=cascade,
    )


class InstrumentedAttribute:
    """Class-level view of a mapped relationship attribute.

    Returned by ``Parent.children`` at the class level; loader options consume
    its ``.property`` (a configured :class:`RelationshipProperty`).
    """

    __slots__ = ("_owner", "_decl")

    def __init__(self, owner: type[object] | None, decl: Relationship[Any]) -> None:
        self._owner = owner
        self._decl = decl

    @property
    def key(self) -> str | None:
        return self._decl.key

    # Named last on purpose: once ``property`` exists in the class namespace
    # it shadows the decorator for anything defined after it.
    @property
    def property(self) -> RelationshipProperty:
        return self._decl.configure()


class InstrumentedList(list[object]):
    """List subclass tracking collection membership for back-population.

    Appends/removals synchronize the paired relationship attribute and apply
    the ``save-update``/``delete-orphan`` cascades; unbound instances behave
    as plain lists (mirroring SQLAlchemy's instrumented collections).
    """

    __slots__ = ("owner", "_prop")

    def __init__(self, iterable: Iterable[object] = ()) -> None:
        super().__init__(iterable)
        self.owner: object | None = None
        self._prop: RelationshipProperty | None = None

    def bind(self, instance: object, prop: RelationshipProperty) -> InstrumentedList:
        """Attach this collection to ``instance`` for ``prop`` back-population."""
        self.owner = instance
        self._prop = prop
        return self

    def _emit_append(self, item: object) -> None:
        if self.owner is not None and self._prop is not None:
            _collection_append(self.owner, self._prop, item)

    def _emit_remove(self, item: object) -> None:
        if self.owner is not None and self._prop is not None:
            _collection_remove(self.owner, self._prop, item)

    def append(self, item: object) -> None:
        super().append(item)
        self._emit_append(item)

    def extend(self, items: Iterable[object]) -> None:
        for item in items:
            self.append(item)

    def insert(self, index: SupportsIndex, item: object) -> None:
        super().insert(index, item)
        self._emit_append(item)

    def remove(self, item: object) -> None:
        super().remove(item)
        self._emit_remove(item)

    def pop(self, index: SupportsIndex = -1) -> object:
        item = self[index]
        super().pop(index)
        self._emit_remove(item)
        return item

    def clear(self) -> None:
        for item in list(self):
            super().remove(item)
            self._emit_remove(item)

    def __setitem__(self, index: object, value: object) -> None:
        if isinstance(index, slice):
            sl = cast("slice[int | None, int | None, int | None]", index)
            removed = self[sl]
            items = cast(Iterable[object], value)
            list[object].__setitem__(self, sl, items)
            for item in removed:
                self._emit_remove(item)
            for item in items:
                self._emit_append(item)
        else:
            idx = cast(int, index)
            removed = self[idx]
            list[object].__setitem__(self, idx, value)
            if removed is not value:
                self._emit_remove(removed)
                self._emit_append(value)

    def __delitem__(self, index: object) -> None:
        if isinstance(index, slice):
            sl = cast("slice[int | None, int | None, int | None]", index)
            removed = self[sl]
            list[object].__delitem__(self, sl)
            for item in removed:
                self._emit_remove(item)
        else:
            idx = cast(int, index)
            removed = self[idx]
            list[object].__delitem__(self, idx)
            self._emit_remove(removed)

    def __iadd__(self, items: Iterable[object]) -> InstrumentedList:
        self.extend(items)
        return self


@dataclass(frozen=True, slots=True)
class _LoaderOption:
    """One ``Select.options()`` loader directive."""

    strategy: str
    attr: object


def joinedload(attr: object) -> _LoaderOption:
    """Eagerly load a relationship via ``LEFT OUTER JOIN`` in the parent query.

    Collection-valued joined loads require ``Result.unique()`` before
    materializing rows, matching SQLAlchemy 2.x semantics.
    """
    return _LoaderOption("joined", attr)


def selectinload(attr: object) -> _LoaderOption:
    """Eagerly load a relationship via a second batched ``IN`` query."""
    return _LoaderOption("selectin", attr)


# Every mapped class ever scanned, in mapping order; configure_mappers walks
# the list so pending declarations resolve once their target exists.
_MAPPERS: list[Mapper[Any]] = []

# One name->class registry per MetaData (the unit shared across a declarative
# base); weak keys keep dropped metadata from leaking.
_REGISTRIES: weakref.WeakKeyDictionary[MetaData, dict[str, type[object]]] = (
    weakref.WeakKeyDictionary()
)


def _registry_for(metadata: MetaData) -> dict[str, type[object]]:
    return _REGISTRIES.setdefault(metadata, {})


def configure_mappers() -> None:
    """Resolve pending relationship declarations (like ``configure_mappers``)."""
    for mapper in list(_MAPPERS):
        _configure_mapper(mapper)


def _configure_mapper(mapper: Mapper[Any]) -> None:
    for decl in list(mapper.pending_relationships):
        _configure_relationship(decl, mapper)
    for prop in mapper.relationships.values():
        _pair_sibling(prop)


def _resolve_class_name(name: str, mapper: Mapper[Any]) -> type[object] | None:
    """Resolve a relationship target name against the class registry."""
    short = name.split(".")[-1]
    found = mapper.class_registry.get(name) or mapper.class_registry.get(short)
    if found is not None:
        return found
    # Module-global fallback for classes mapped on a different base.
    module = sys.modules.get(mapper.class_.__module__)
    candidate = getattr(module, short, None) if module is not None else None
    return candidate if isinstance(candidate, type) else None


def _attr_for_column(mapper: Mapper[Any], column: Column) -> str:
    """Find the attribute name bound to ``column`` on ``mapper``."""
    for attr, bound in mapper.attr_columns.items():
        if bound is column or bound.name == column.name:
            return attr
    raise ArgumentError(f"No mapped attribute found for column '{column.name}'")


def _configure_relationship(decl: Relationship[Any], mapper: Mapper[Any]) -> bool:
    """Resolve one declaration into a :class:`RelationshipProperty`."""
    if decl.configured is not None:
        return True
    target = decl.argument if decl.argument is not None else decl.target_hint
    if isinstance(target, str):
        target = _resolve_class_name(target, mapper)
    if not isinstance(target, type):
        return False
    target_mapper = cast("Mapper[Any] | None", getattr(target, "__mapper__", None))
    if not isinstance(target_mapper, Mapper):
        return False
    table = mapper.local_table
    target_table = target_mapper.local_table
    a_fks = [
        column
        for column in table.columns
        if column.foreign_key is not None and column.foreign_key.target_table == target_table.name
    ]
    b_fks = [
        column
        for column in target_table.columns
        if column.foreign_key is not None and column.foreign_key.target_table == table.name
    ]
    if a_fks and b_fks:
        raise ArgumentError(
            f"Ambiguous join condition between '{mapper.class_.__name__}' and "
            f"'{target.__name__}': both tables carry foreign keys to each other"
        )
    prop = RelationshipProperty()
    prop.key = cast(str, decl.key)
    prop.mapper = mapper
    prop.entity = target
    prop.target_mapper = target_mapper
    prop.descriptor = decl
    prop.lazy = decl.lazy
    prop.cascade = decl.cascade
    prop.back_populates = decl.back_populates
    prop.sibling = None
    if a_fks:
        prop.direction = _MANYTOONE
        fk_col = a_fks[0]
        prop.local_col = fk_col
        prop.local_attr = _attr_for_column(mapper, fk_col)
        if target_mapper.pk_column is None or target_mapper.pk_attr is None:
            raise ArgumentError(f"Target '{target.__name__}' has no primary key")
        prop.remote_col = target_mapper.pk_column
        prop.remote_attr = target_mapper.pk_attr
    elif b_fks:
        prop.direction = _ONETOMANY
        fk_col = b_fks[0]
        prop.remote_col = fk_col
        prop.remote_attr = _attr_for_column(target_mapper, fk_col)
        if mapper.pk_column is None or mapper.pk_attr is None:
            raise ArgumentError(f"'{mapper.class_.__name__}' has no primary key")
        prop.local_col = mapper.pk_column
        prop.local_attr = mapper.pk_attr
    else:
        raise ArgumentError(
            f"Could not determine join condition between '{mapper.class_.__name__}' "
            f"and '{target.__name__}' (no foreign key links their tables)"
        )
    uselist = decl.uselist
    if uselist is None:
        if decl.container == "collection":
            uselist = True
        elif decl.container == "scalar":
            uselist = False
        elif prop.direction == _ONETOMANY:
            uselist = not fk_col.unique
        else:
            uselist = False
    prop.uselist = bool(uselist)
    mapper.relationships[prop.key] = prop
    decl.configured = prop
    if decl in mapper.pending_relationships:
        mapper.pending_relationships.remove(decl)
    # ``backref`` materializes the reverse attribute on the target class.
    if decl.backref is not None and decl.backref not in target_mapper.relationships:
        if not any(d.key == decl.backref for d in target_mapper.pending_relationships):
            rev: Relationship[Any] = Relationship(None, back_populates=prop.key)
            rev.key = decl.backref
            rev.declaring_cls = target
            setattr(target, decl.backref, rev)
            target_mapper.relationship_attrs += (decl.backref,)
            target_mapper.pending_relationships.append(rev)
            _configure_relationship(rev, target_mapper)
    return True


def _pair_sibling(prop: RelationshipProperty) -> None:
    """Link a configured property to its ``back_populates`` counterpart."""
    if prop.sibling is not None or prop.back_populates is None:
        return
    sibling = prop.target_mapper.relationships.get(prop.back_populates)
    if sibling is None:
        return
    prop.sibling = sibling
    sibling.sibling = prop


def _collection_for(instance: object, prop: RelationshipProperty) -> InstrumentedList:
    """Return (creating if needed) the instrumented collection on ``instance``."""
    instance_dict = instance.__dict__
    current = instance_dict.get(prop.key)
    if isinstance(current, InstrumentedList):
        if current.owner is None:
            current.bind(instance, prop)
        return current
    collection = InstrumentedList(
        cast("list[object]", current) if isinstance(current, list) else ()
    )
    collection.bind(instance, prop)
    instance_dict[prop.key] = collection
    return collection


def _set_scalar_rel(obj: object, prop: RelationshipProperty, value: object) -> None:
    """Assign a scalar relationship attribute with back-population.

    Object references sync immediately; the FK column value syncs at flush.
    """
    instance_dict = obj.__dict__
    old = instance_dict.get(prop.key)
    if prop.key in instance_dict and old is value:
        return
    instance_dict[prop.key] = value
    uow = _state_of(obj)
    if uow is not None:
        uow.attr_set(obj, prop.key)
    sibling = prop.sibling
    if sibling is not None:
        if old is not None and old is not value:
            if sibling.uselist:
                other = getattr(old, "__dict__", {}).get(sibling.key)
                if isinstance(other, list) and obj in other:
                    list[object].remove(cast("list[object]", other), obj)
            else:
                _set_scalar_rel(old, sibling, None)
        if value is not None:
            if sibling.uselist:
                collection = _collection_for(value, sibling)
                if obj not in collection:
                    list[object].append(collection, obj)
            else:
                _set_scalar_rel(value, sibling, obj)
    if value is not None and "save-update" in prop.cascade:
        if uow is not None and uow.is_managed(obj):
            session = uow.session
            if session is not None:
                value_uow = _state_of(value)
                if value_uow is None or not value_uow.is_managed(value):
                    cast("Session", session).add(value)


def _collection_append(owner: object, prop: RelationshipProperty, item: object) -> None:
    sibling = prop.sibling
    if sibling is not None:
        if sibling.uselist:
            other = _collection_for(item, sibling)
            if owner not in other:
                list[object].append(other, owner)
        else:
            _set_scalar_rel(item, sibling, owner)
    if "save-update" in prop.cascade:
        uow = _state_of(owner)
        if uow is not None and uow.is_managed(owner):
            session = uow.session
            if session is not None:
                item_uow = _state_of(item)
                if item_uow is None or not item_uow.is_managed(item):
                    cast("Session", session).add(item)


def _collection_remove(owner: object, prop: RelationshipProperty, item: object) -> None:
    sibling = prop.sibling
    if sibling is not None:
        if sibling.uselist:
            other = getattr(item, "__dict__", {}).get(sibling.key)
            if isinstance(other, list) and owner in other:
                list[object].remove(cast("list[object]", other), owner)
        else:
            _set_scalar_rel(item, sibling, None)
    if "delete-orphan" in prop.cascade:
        item_uow = _state_of(item)
        if item_uow is not None and item_uow.is_managed(item):
            status = item_uow.status(item)
            if status == _STATUS_PERSISTENT:
                item_uow.mark_deleted(item)
            elif status == _STATUS_PENDING:
                item_uow.detach(item)


def _set_collection(obj: object, prop: RelationshipProperty, value: object) -> None:
    """Replace a collection's membership, emitting per-item events."""
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        raise ArgumentError(f"'{prop.key}' expected a collection, got {type(value).__name__!r}")
    items = list(cast(Iterable[object], value))
    collection = _collection_for(obj, prop)
    for item in list(collection):
        if item not in items:
            collection.remove(item)
    for item in items:
        if item not in collection:
            collection.append(item)


def _load_relationship(instance: object, prop: RelationshipProperty) -> object:
    """Lazy-load the relationship attribute, or return the unloaded value."""
    uow = _state_of(instance)
    instance_dict = instance.__dict__
    if uow is None or not uow.is_managed(instance):
        # Transient/detached: an empty collection or the stored scalar.
        if prop.uselist:
            return _collection_for(instance, prop)
        return instance_dict.get(prop.key)
    status = uow.status(instance)
    if status == _STATUS_PENDING:
        if prop.uselist:
            return _collection_for(instance, prop)
        return instance_dict.get(prop.key)
    session = uow.session
    if session is None:
        if prop.uselist:
            return _collection_for(instance, prop)
        return instance_dict.get(prop.key)
    return cast("Session", session)._lazy_load(instance, prop)  # pyright: ignore[reportPrivateUsage]


def _stitch_loaded(owner: object, prop: RelationshipProperty, item: object) -> None:
    """Populate a loaded relationship value without marking anything dirty.

    ``item`` is the reconciled (identity-map) related instance or ``None``.
    """
    instance_dict = owner.__dict__
    sibling = prop.sibling
    if prop.direction == _ONETOMANY and prop.uselist:
        if item is None:
            return
        collection = _collection_for(owner, prop)
        if item not in collection:
            list[object].append(collection, item)
        if sibling is not None and not sibling.uselist:
            item.__dict__[sibling.key] = owner
    else:
        instance_dict[prop.key] = item
        if item is not None and sibling is not None:
            if sibling.uselist:
                collection = _collection_for(item, sibling)
                if owner not in collection:
                    list[object].append(collection, owner)
            else:
                item.__dict__[sibling.key] = owner


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
        mapper = type(self).__mapper__
        for attr, spec in mapper.specs.items():
            if attr in kwargs:
                value = kwargs.pop(attr)
            elif spec is not None and spec.default_factory is not None:
                value = spec.default_factory()
            elif spec is not None and spec.default is not None:
                value = spec.default
            else:
                value = None
            setattr(self, attr, value)
        # Relationship kwargs go through the descriptor so back-population
        # and cascade bookkeeping run like any other assignment.
        for attr in mapper.relationship_attrs:
            if attr in kwargs:
                setattr(self, attr, kwargs.pop(attr))
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


# ``Head[...]``-shaped annotation text, e.g. ``Mapped[list[CChild]]``.
_TYPED_HEAD = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*\[(?P<inner>.*)\]\s*$", re.DOTALL)

# ``get_origin`` results considered collection-typed for uselist inference.
_COLLECTION_ORIGINS: frozenset[object] = frozenset(
    {list, set, frozenset, dict, Sequence, Iterable, MutableSequence, Collection}
)
_COLLECTION_NAMES = frozenset(
    {
        "list",
        "List",
        "set",
        "Set",
        "frozenset",
        "FrozenSet",
        "dict",
        "Dict",
        "Sequence",
        "Iterable",
        "MutableSequence",
        "Collection",
        "AbstractSet",
    }
)


def _split_top(text: str, sep: str = ",") -> list[str]:
    """Split ``text`` on ``sep`` ignoring separators inside ``[]``/``()``."""
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == sep and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _mapped_inner_text(text: str) -> str | None:
    """Return the inner text of ``Mapped[...]`` annotation text, if it is one."""
    match = _TYPED_HEAD.match(text)
    if match is None or match.group(1).split(".")[-1] != "Mapped":
        return None
    return match.group("inner")


def _rel_annotation(annotation: object) -> tuple[str | None, object | None]:
    """Destructure a resolved annotation into (container, target).

    ``container`` is ``"collection"`` for ``list[T]``-shaped annotations and
    ``"scalar"`` otherwise; ``target`` is the element class or its name.
    """
    if annotation is None:
        return None, None
    if isinstance(annotation, str):
        return _rel_annotation_text(annotation)
    if isinstance(annotation, typing.ForwardRef):
        return "scalar", annotation.__forward_arg__
    origin = get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return _rel_annotation(non_none[0])
        return "scalar", annotation
    if origin in _COLLECTION_ORIGINS:
        args = get_args(annotation)
        _container, target = _rel_annotation(args[0] if args else None)
        return "collection", target
    if origin is typing.Annotated:
        args = get_args(annotation)
        return _rel_annotation(args[0] if args else None)
    return "scalar", annotation


def _rel_annotation_text(text: str) -> tuple[str | None, str | None]:
    """Parse unresolved annotation text into (container, target-name)."""
    text = text.strip()
    match = _TYPED_HEAD.match(text)
    if match is not None:
        head = match.group(1).split(".")[-1]
        inner = match.group("inner")
        if head in {"Mapped", "Optional"}:
            return _rel_annotation_text(inner)
        if head == "Annotated":
            return _rel_annotation_text(_split_top(inner)[0])
        if head == "Union":
            parts = [part for part in _split_top(inner) if part.strip() not in {"None", "NoneType"}]
            if len(parts) == 1:
                return _rel_annotation_text(parts[0])
            return "scalar", text
        if head in _COLLECTION_NAMES:
            parts = _split_top(inner)
            _container, target = _rel_annotation_text(parts[0])
            return "collection", target
        return "scalar", text
    parts = [
        part.strip() for part in _split_top(text, "|") if part.strip() not in {"None", "NoneType"}
    ]
    if len(parts) > 1:
        return "scalar", text
    text = parts[0] if parts else text
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return "scalar", text[1:-1].strip()
    return "scalar", text


def _rel_annotation_for(cls: type[object], hint: object, raw: object) -> tuple[str | None, object]:
    """Resolve a relationship attribute's annotation to (container, target)."""
    annotation = hint
    if annotation is None:
        if isinstance(raw, str):
            inner = _mapped_inner_text(raw)
            return _rel_annotation_text(inner if inner is not None else raw)
        annotation = raw
    if annotation is not None and get_origin(annotation) is Mapped:
        args = get_args(annotation)
        annotation = args[0] if args else None
    return _rel_annotation(annotation)


def _eval_annotation(cls: type[object], raw: object) -> object:
    """Evaluate a string annotation in the declaring class's module globals.

    Unresolvable text (forward references to not-yet-defined classes) is
    returned unchanged so relationship declarations can defer resolution.
    """
    if raw is None or not isinstance(raw, str):
        return raw
    module = sys.modules.get(cls.__module__)
    namespace = dict(getattr(module, "__dict__", {}) or {})
    try:
        return eval(raw, namespace)  # noqa: S307 - annotation text is author code
    except (NameError, TypeError, AttributeError):
        return raw


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
    try:
        hints = get_type_hints(cls)
    except Exception:
        # Forward references such as Mapped[list["Child"]] cannot resolve
        # eagerly; annotations are parsed per attribute and relationship
        # targets resolve at configuration time instead.
        hints = {}
    registry = _registry_for(metadata)
    attrs: list[str] = []
    specs: dict[str, MappedColumn[Any] | None] = {}
    columns: list[Column] = []
    rel_attrs: list[str] = []
    rel_decls: list[Relationship[Any]] = []
    raw_annotations = cast("dict[str, object]", cls.__dict__.get("__annotations__", {}))
    for name in raw_annotations:
        if name.startswith("_") or name in {"metadata"}:
            continue
        raw = raw_annotations[name]
        spec_value = cls.__dict__.get(name)
        hint = hints.get(name)
        if get_origin(hint) is ClassVar:
            continue
        if isinstance(spec_value, Relationship):
            decl = cast("Relationship[Any]", spec_value)
            decl.key = name
            decl.declaring_cls = cls
            decl.container, decl.target_hint = _rel_annotation_for(cls, hint, raw)
            rel_attrs.append(name)
            rel_decls.append(decl)
            continue
        spec = cast(
            "MappedColumn[Any] | None",
            spec_value if isinstance(spec_value, MappedColumn) else None,
        )
        resolved = hint if hint is not None else _eval_annotation(cls, raw)
        if resolved is not None and get_origin(resolved) is Mapped:
            args = get_args(resolved)
            optional: bool | None
            optional, inner = _unwrap_optional(args[0] if args else None)
        elif spec is None:
            if resolved is None:
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
    # Bare mapped_column()/relationship() assignments carry no annotation.
    for name, value in cls.__dict__.items():
        if name.startswith("_"):
            continue
        if isinstance(value, MappedColumn) and name not in specs:
            columns.append(
                _column_for_attr(cls, name, cast("MappedColumn[Any]", value), None, None)
            )
            attrs.append(name)
            specs[name] = value
        elif isinstance(value, Relationship) and name not in rel_attrs:
            decl_value = cast("Relationship[Any]", value)
            decl_value.key = name
            decl_value.declaring_cls = cls
            rel_attrs.append(name)
            rel_decls.append(decl_value)
    if not columns:
        raise ArgumentError(f"{cls.__name__} declares __tablename__ but no mapped attributes")
    table = Table(str(tablename), metadata, *columns)
    cls_any.__table__ = table
    mapper = Mapper(cls, table, tuple(attrs), specs)
    mapper.relationship_attrs = tuple(rel_attrs)
    mapper.pending_relationships = list(rel_decls)
    mapper.class_registry = registry
    cls_any.__mapper__ = mapper
    registry[cls.__name__] = cls
    registry.setdefault(f"{cls.__module__}.{cls.__qualname__}", cls)
    _MAPPERS.append(mapper)
    for attr, column in zip(attrs, table.columns, strict=True):
        # Bind the mapped attribute name onto the class-level Column so its
        # ``__get__`` resolves the correct ``__dict__`` key on instances (a
        # column name can differ from the attribute name).
        object.__setattr__(column, "key", attr)
        setattr(cls, attr, column)
    configure_mappers()


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
        foreign_key=spec.foreign_key if spec is not None else None,
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
    mapper = _mapper_of(instance)
    for attr in mapper.attrs:
        instance.__dict__.pop(attr, None)
    for key in mapper.relationship_attrs:
        instance.__dict__.pop(key, None)


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
        self._flushing = False

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
        self._cascade_save_update(instance, {id(instance)})

    def _cascade_save_update(self, instance: object, seen: set[int]) -> None:
        """Attach relationship-referenced objects under ``save-update``."""
        mapper = _mapper_of(instance)
        _configure_mapper(mapper)
        for prop in mapper.relationships.values():
            if "save-update" not in prop.cascade:
                continue
            value = instance.__dict__.get(prop.key)
            if value is None:
                continue
            if prop.uselist and isinstance(value, list):
                items = cast("list[object]", value)
            else:
                items = [value]
            for item in items:
                if item is None or id(item) in seen:
                    continue
                seen.add(id(item))
                item_uow = _state_of(item)
                if item_uow is None or not item_uow.is_managed(item):
                    self.add(item)
                elif item_uow is self._uow:
                    self._cascade_save_update(item, seen)

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
        self._apply_delete_cascade(instance)
        uow.mark_deleted(instance)
        self._mark_active()

    def _apply_delete_cascade(self, instance: object) -> None:
        """Apply delete semantics to related items before marking a delete.

        With ``delete``/``delete-orphan`` cascades the related items are
        deleted too (children first); under the default cascade the related
        reference is cleared so the FK is nulled at flush.
        """
        mapper = _mapper_of(instance)
        _configure_mapper(mapper)
        for prop in mapper.relationships.values():
            orphan = "delete-orphan" in prop.cascade or "delete" in prop.cascade
            for item in self._related_items(instance, prop):
                if orphan:
                    item_uow = _state_of(item)
                    if item_uow is not None and item_uow is self._uow:
                        status = item_uow.status(item)
                        if status == _STATUS_PERSISTENT:
                            self._apply_delete_cascade(item)
                            item_uow.mark_deleted(item)
                        elif status == _STATUS_PENDING:
                            item_uow.detach(item)
                else:
                    sibling = prop.sibling
                    if sibling is None:
                        continue
                    if sibling.uselist:
                        other = item.__dict__.get(sibling.key)
                        if isinstance(other, list) and instance in other:
                            list[object].remove(cast("list[object]", other), instance)
                    else:
                        _set_scalar_rel(item, sibling, None)

    def _related_items(self, instance: object, prop: RelationshipProperty) -> list[object]:
        """Return the related object(s) for ``prop`` on ``instance``."""
        instance_dict = instance.__dict__
        if prop.key in instance_dict:
            value = instance_dict[prop.key]
        else:
            value = prop.descriptor.__get__(instance, type(instance))
        if value is None:
            return []
        if isinstance(value, list):
            return list(cast("list[object]", value))
        return [value]

    # -- flush -------------------------------------------------------------

    def flush(self) -> None:
        """Emit batched INSERT/UPDATE/DELETE for the unit of work.

        Ops are gathered Python-side and executed in one
        ``NativeConnection.execute_flush`` crossing inside the session's
        transaction; returned ``lastrowid`` values become primary keys.
        Relationship references resolve to foreign-key values here (SA's
        "synchronize FKs at flush"), and inserts are dependency-ordered so
        parent rows exist before dependent children.
        """
        if self._flushing:
            return
        self._check_active()
        self._mark_active()
        uow = self._uow
        if not uow.has_changes():
            return
        self._flushing = True
        try:
            self._flush_changes()
        finally:
            self._flushing = False

    def _flush_changes(self) -> None:
        """Gather and execute one flush batch; reconcile uow state after."""
        uow = self._uow
        ops: list[tuple[str, list[object], list[str | None], bool]] = []
        persisted: list[tuple[object, object]] = []
        cleaned: list[object] = []
        dropped: list[object] = []
        # FK assignments resolved after the batch from an earlier op's rowid.
        deferred_fk: list[tuple[object, str, int]] = []
        insert_index: dict[int, int] = {}
        for obj in self._dependency_order(uow.new_objects()):
            mapper = _mapper_of(obj)
            pk_attr = mapper.pk_attr
            pk_value = obj.__dict__.get(pk_attr) if pk_attr is not None else None
            rel_fk = self._pending_relationship_refs(obj, mapper)
            values: dict[str, object] = {}
            deferred: list[tuple[str, str, int]] = []
            for attr in mapper.attrs:
                column = mapper.attr_columns[attr]
                if attr == pk_attr and pk_value is None:
                    # Autoincrement PKs are omitted so SQLite assigns them.
                    continue
                if attr in rel_fk:
                    related = rel_fk[attr]
                    if related is None:
                        values[column.name] = None
                    else:
                        dep = insert_index.get(id(related))
                        if dep is not None:
                            values[column.name] = None
                            deferred.append((column.name, attr, dep))
                        else:
                            values[column.name] = self._related_pk(related)
                            obj.__dict__[attr] = values[column.name]
                else:
                    values[column.name] = obj.__dict__.get(attr)
            compiled = Insert(mapper.local_table).values(values).compile()
            self._raise_bind_error(compiled)
            ordered = list(compiled._ordered)
            for col_name, attr, dep in deferred:
                ordered[compiled._bind_names.index(col_name)] = ("__catalyst_rowid__", dep)
                deferred_fk.append((obj, attr, dep))
            ops.append(
                (
                    compiled.string,
                    ordered,
                    compiled._bind_types,
                    pk_value is None and pk_attr is not None,
                )
            )
            insert_index[id(obj)] = len(ops) - 1
            persisted.append((obj, pk_value))
        for obj in uow.dirty_objects():
            mapper = _mapper_of(obj)
            pk = uow.pk_of(obj)
            dirty = set(uow.dirty_attrs_of(obj))
            changes: dict[str, object] = {}
            deferred_cols: dict[str, tuple[str, int]] = {}
            # Relationship assignment syncs the FK column value at flush.
            for prop in mapper.relationships.values():
                if prop.direction != _MANYTOONE or prop.key not in dirty:
                    continue
                related = obj.__dict__.get(prop.key)
                fk_attr = prop.local_attr
                if related is None:
                    if obj.__dict__.get(fk_attr) is not None:
                        changes[fk_attr] = None
                    continue
                dep = insert_index.get(id(related))
                if dep is not None:
                    changes[fk_attr] = None
                    deferred_cols[fk_attr] = (
                        mapper.attr_columns[fk_attr].name,
                        dep,
                    )
                    deferred_fk.append((obj, fk_attr, dep))
                else:
                    resolved = self._related_pk(related)
                    if resolved != obj.__dict__.get(fk_attr):
                        changes[fk_attr] = resolved
                    obj.__dict__[fk_attr] = resolved
            for attr in dirty:
                if attr in mapper.attr_columns and attr not in changes:
                    changes[attr] = obj.__dict__.get(attr)
            if not changes:
                cleaned.append(obj)
                continue
            values = {mapper.attr_columns[attr].name: changes[attr] for attr in changes}
            compiled = (
                Update(mapper.local_table).values(values).where(mapper.pk_column == pk).compile()
            )
            self._raise_bind_error(compiled)
            ordered = list(compiled._ordered)
            for _fk_attr, (col_name, dep) in deferred_cols.items():
                ordered[compiled._bind_names.index(col_name)] = ("__catalyst_rowid__", dep)
            ops.append((compiled.string, ordered, compiled._bind_types, False))
            cleaned.append(obj)
        for obj in self._dependent_first_order(uow.deleted_objects()):
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
        for obj, attr, dep in deferred_fk:
            _dep_obj, dep_pk = persisted[dep]
            obj.__dict__[attr] = dep_pk if dep_pk is not None else assigned[dep]
        uow.flush_transition(final, cleaned, dropped)

    def _dependency_order(self, objects: list[object]) -> list[object]:
        """Order objects so many-to-one FK dependencies come first.

        Parents insert before the children that reference them; a referenced
        object outside the batch does not constrain ordering.
        """
        ids = {id(obj) for obj in objects}
        deps: dict[int, list[object]] = {}
        for obj in objects:
            mapper = _mapper_of(obj)
            for prop in mapper.relationships.values():
                if prop.direction != _MANYTOONE:
                    continue
                related = obj.__dict__.get(prop.key)
                if related is not None and id(related) in ids:
                    deps.setdefault(id(obj), []).append(related)
        ordered: list[object] = []
        visited: set[int] = set()

        def visit(obj: object) -> None:
            if id(obj) in visited:
                return
            visited.add(id(obj))
            for dep in deps.get(id(obj), ()):
                visit(dep)
            ordered.append(obj)

        for obj in objects:
            visit(obj)
        return ordered

    def _dependent_first_order(self, objects: list[object]) -> list[object]:
        """Delete order: dependents before the objects they reference."""
        return list(reversed(self._dependency_order(objects)))

    def _pending_relationship_refs(self, obj: object, mapper: Mapper[Any]) -> dict[str, object]:
        """FK attribute -> assigned related object for pending inserts."""
        refs: dict[str, object] = {}
        for prop in mapper.relationships.values():
            if prop.direction != _MANYTOONE:
                continue
            if prop.key in obj.__dict__:
                refs[prop.local_attr] = obj.__dict__.get(prop.key)
        return refs

    def _related_pk(self, related: object) -> object:
        """Primary key of a related object (uow-stored, else attribute)."""
        pk = self._uow.pk_of(related)
        if pk is None:
            pk_attr = _mapper_of(related).pk_attr
            if pk_attr is not None:
                pk = related.__dict__.get(pk_attr)
        return pk

    # -- relationship loading -----------------------------------------------

    def _lazy_load(self, instance: object, prop: RelationshipProperty) -> object:
        """Lazy-load one relationship attribute for a persistent instance."""
        if self._closed:
            if prop.uselist:
                return _collection_for(instance, prop)
            return instance.__dict__.get(prop.key)
        self._check_active()
        if prop.lazy == "raise":
            raise InvalidRequestError(
                f"'{_mapper_of(instance).class_.__name__}.{prop.key}' is not "
                "available due to lazy='raise'"
            )
        uow = self._uow
        instance_dict = instance.__dict__
        if self.autoflush and not self._flushing:
            self.flush()
        self._mark_active()
        if prop.direction == _MANYTOONE:
            # getattr (not __dict__) so an expired FK column reloads first.
            fk_value = getattr(instance, prop.local_attr, None)
            target = None if fk_value is None else self.get(prop.entity, fk_value)
            instance_dict[prop.key] = target
            uow.attr_loaded(instance, prop.key)
            return target
        pk = uow.pk_of(instance)
        if pk is None and prop.mapper.pk_attr is not None:
            pk = instance_dict.get(prop.mapper.pk_attr)
        if prop.uselist:
            collection = InstrumentedList()
            collection.bind(instance, prop)
            if pk is not None:
                for item in self._relationship_children(prop, pk):
                    list[object].append(collection, item)
                    sibling = prop.sibling
                    if sibling is not None and not sibling.uselist:
                        item.__dict__[sibling.key] = instance
            instance_dict[prop.key] = collection
            uow.attr_loaded(instance, prop.key)
            return collection
        target = None
        if pk is not None:
            items = self._relationship_children(prop, pk)
            target = items[0] if items else None
        instance_dict[prop.key] = target
        uow.attr_loaded(instance, prop.key)
        if target is not None and prop.sibling is not None and not prop.sibling.uselist:
            target.__dict__[prop.sibling.key] = instance
        return target

    def _relationship_children(self, prop: RelationshipProperty, value: object) -> list[object]:
        """One lazy-load SELECT: rows of the target table keyed by the FK."""
        target_mapper = prop.target_mapper
        fk_col = prop.remote_col
        predicate = Expression(
            (
                "binary",
                ("literal", value, fk_col.name, _column_type_tag(fk_col.type)),
                "=",
                fk_col.serialize(),
            )
        )
        statement = Select(
            tuple(target_mapper.local_table.columns), target_mapper.local_table, predicate
        )
        result = self._connection.execute(statement)
        instances = result.native_result.hydrate(prop.entity, list(target_mapper.attrs))
        return self._uow.reconcile(
            prop.entity,
            cast(str, target_mapper.pk_attr),
            list(target_mapper.attrs),
            instances,
        )

    def _execute_loader_select(self, statement: Select) -> CursorResult:
        """Execute a ``select(Entity)`` carrying joinedload/selectinload options."""
        entity = cast("type[object]", statement.entity)
        mapper = _mapper_of(entity)
        configure_mappers()
        joined: list[RelationshipProperty] = []
        selectin: list[RelationshipProperty] = []
        for option in statement.loader_options:
            if not isinstance(option, _LoaderOption):
                raise ArgumentError(f"Unsupported loader option {option!r}")
            prop = self._resolve_option_property(option.attr)
            if prop.mapper.class_ is not entity:
                raise ArgumentError(
                    f"Loader option for '{prop.key}' does not apply to entity {entity.__name__}"
                )
            (joined if option.strategy == "joined" else selectin).append(prop)
        if joined:
            result = self._joined_select(statement, entity, mapper, joined)
        else:
            raw = self._connection.execute(statement)
            result = CursorResult(raw.native_result, entity=entity, uow=self._uow)
        if selectin:
            for prop in selectin:
                self._selectin_load(result.entity_rows or [], prop)
        return result

    @staticmethod
    def _resolve_option_property(attr: object) -> RelationshipProperty:
        if isinstance(attr, InstrumentedAttribute):
            return attr.property
        if isinstance(attr, Relationship):
            return attr.configure()
        raise ArgumentError(f"Loader option expected a relationship attribute, got {attr!r}")

    def _joined_select(
        self,
        statement: Select,
        entity: type[object],
        mapper: Mapper[Any],
        joined: list[RelationshipProperty],
    ) -> CursorResult:
        """Single LEFT OUTER JOIN query hydrating parent and related slices."""
        specs: list[tuple[type[object], list[str]]] = [(entity, list(mapper.attrs))]
        columns: list[Column | Expression] = list(mapper.local_table.columns)
        joins: list[tuple[str, str, tuple[object, ...]]] = []
        joined_props: list[RelationshipProperty] = []
        for index, prop in enumerate(joined):
            target_mapper = prop.target_mapper
            alias = f"{target_mapper.local_table.name}_{index + 1}"
            left = (
                "column",
                mapper.local_table.name,
                prop.local_col.name,
                _column_type_tag(prop.local_col.type),
            )
            right = (
                "column",
                alias,
                prop.remote_col.name,
                _column_type_tag(prop.remote_col.type),
            )
            joins.append((target_mapper.local_table.name, alias, ("binary", left, "=", right)))
            for column in target_mapper.local_table.columns:
                columns.append(
                    Expression(("column", alias, column.name, _column_type_tag(column.type)))
                )
            specs.append((prop.entity, list(target_mapper.attrs)))
            joined_props.append(prop)
        joined_stmt = Select(
            tuple(columns),
            mapper.local_table,
            cast("Column | Expression | None", statement.predicate),
            statement.ordering,
            statement.row_limit,
            statement.row_offset,
            entity,
            tuple(joins),
            (),
        )
        result = self._connection.execute(joined_stmt)
        rows = result.native_result.hydrate_entities(specs)
        # Reconcile every hydrated slice through the identity map so related
        # rows yield the same instance session.get() would return.
        canon: list[dict[int, object]] = []
        for spec_index, (cls, _attrs) in enumerate(specs):
            raw_items = [row[spec_index] for row in rows if row[spec_index] is not None]
            cls_mapper = _mapper_of(cls)
            reconciled = self._uow.reconcile(
                cls, cast(str, cls_mapper.pk_attr), list(cls_mapper.attrs), raw_items
            )
            canon.append({id(raw): item for raw, item in zip(raw_items, reconciled, strict=True)})
        entity_rows: list[tuple[object, ...]] = []
        for row in rows:
            parent = canon[0][id(row[0])]
            entity_rows.append((parent,))
            for spec_index, prop in enumerate(joined_props, start=1):
                raw_item = row[spec_index]
                item = canon[spec_index][id(raw_item)] if raw_item is not None else None
                _stitch_loaded(parent, prop, item)
        return CursorResult(
            result.native_result,
            entity=entity,
            entity_rows=entity_rows,
            requires_unique=any(prop.uselist for prop in joined_props),
        )

    def _selectin_load(
        self, entity_rows: list[tuple[object, ...]], prop: RelationshipProperty
    ) -> None:
        """Second-query eager load: one batched ``IN`` SELECT per relationship."""
        if not entity_rows:
            return
        target_mapper = prop.target_mapper
        if prop.direction == _ONETOMANY:
            parents: dict[object, object] = {}
            seen: set[int] = set()
            for (parent,) in entity_rows:
                if id(parent) in seen:
                    continue
                seen.add(id(parent))
                pk = self._uow.pk_of(parent)
                if pk is None and prop.mapper.pk_attr is not None:
                    pk = parent.__dict__.get(prop.mapper.pk_attr)
                if pk is not None:
                    parents[pk] = parent
            if not parents:
                return
            fk_col = prop.remote_col
            fk_attr = prop.remote_attr
            # SA selects the foreign key first in the batch query.
            cols = [fk_col] + [
                column for column in target_mapper.local_table.columns if column is not fk_col
            ]
            attrs = [fk_attr] + [attr for attr in target_mapper.attrs if attr != fk_attr]
            statement = Select(tuple(cols), target_mapper.local_table).where(
                fk_col.in_(list(parents))
            )
            result = self._connection.execute(statement)
            instances = result.native_result.hydrate(prop.entity, attrs)
            instances = self._uow.reconcile(
                prop.entity, cast(str, target_mapper.pk_attr), list(target_mapper.attrs), instances
            )
            for item in instances:
                parent = parents.get(item.__dict__.get(fk_attr))
                if parent is not None:
                    _stitch_loaded(parent, prop, item)
            return
        # many-to-one selectinload: batch-load parents for the loaded FK set.
        fks: list[object] = []
        seen_fk: set[object] = set()
        children: list[tuple[object, object]] = []
        for (child,) in entity_rows:
            fk_value = getattr(child, prop.local_attr, None)
            children.append((child, fk_value))
            if fk_value is not None and fk_value not in seen_fk:
                seen_fk.add(fk_value)
                fks.append(fk_value)
        if not fks:
            for child, _fk in children:
                child.__dict__[prop.key] = None
            return
        statement = Select(
            tuple(target_mapper.local_table.columns), target_mapper.local_table
        ).where(prop.remote_col.in_(fks))
        result = self._connection.execute(statement)
        instances = result.native_result.hydrate(prop.entity, list(target_mapper.attrs))
        instances = self._uow.reconcile(
            prop.entity, cast(str, target_mapper.pk_attr), list(target_mapper.attrs), instances
        )
        pk_attr = cast(str, target_mapper.pk_attr)
        by_pk = {instance.__dict__.get(pk_attr): instance for instance in instances}
        for child, fk_value in children:
            item = by_pk.get(fk_value)
            if item is not None:
                _stitch_loaded(child, prop, item)
            else:
                child.__dict__[prop.key] = None

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
        for key in mapper.relationship_attrs:
            instance.__dict__.pop(key, None)

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
        if (
            isinstance(statement, Select)
            and statement.entity is not None
            and statement.loader_options
        ):
            return self._execute_loader_select(statement)
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
