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
import warnings
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
    NamedTuple,
    SupportsIndex,
    TypeVar,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from typing_extensions import dataclass_transform

from ._catalyst import (
    AmbiguousForeignKeysError,
    ArgumentError,
    DetachedInstanceError,
    InvalidRequestError,
    NativeUow,
    ObjectDeletedError,
    PendingRollbackError,
    StaleDataError,
    UnmappedInstanceError,
)
from .engine import (
    Connection,
    CursorResult,
    Engine,
    SavepointTransaction,
    ScalarResult,
    _hydrate_polymorphic,  # pyright: ignore[reportPrivateUsage]
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
    _polymorphic_criterion,  # pyright: ignore[reportPrivateUsage]
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .engine import TextClause

_T = TypeVar("_T")

# ``NativeUow.status`` values kept in sync with ``src/session.rs``.
_STATUS_PENDING = 1
_STATUS_PERSISTENT = 2
_STATUS_DELETED = 3

# ``__dict__`` marker stamped on detached objects: ``(pk, dirty, expired)`` —
# the equivalent of SQLAlchemy's surviving ``InstanceState.key``/``.modified``
# after expunge.  ``dirty``/``expired`` are tuples of attribute names.
_DETACHED = "_catalyst_detached"

# Sentinel for "no client-side column default" during bulk inserts.
_MISSING = object()


class SAWarning(Warning):
    """SQLAlchemy-parity warning category (e.g. missing polymorphic_identity)."""


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
        attr_columns: dict[str, Column] | None = None,
    ) -> None:
        self.class_ = class_
        self.local_table = table
        self.attrs = attrs
        self.specs = specs
        # Attribute-name -> bound Column lookup for unit-of-work SQL building.
        if attr_columns is not None:
            self.attr_columns = dict(attr_columns)
        else:
            self.attr_columns = dict(zip(attrs, table.columns, strict=True))
        self.columns = tuple(self.attr_columns.values())
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
        # Inheritance bookkeeping. ``inherits`` links a mapper to the parent
        # mapper it was derived from (single-table inheritance shares the
        # parent's Table); ``base_mapper`` is the hierarchy root.
        self.inherits: Mapper[Any] | None = None
        self.base_mapper: Mapper[Any] = self
        # Discriminator column + attribute for polymorphic hierarchies.
        self.polymorphic_on: Column | None = None
        self.polymorphic_on_attr: str | None = None
        # This class's ``polymorphic_identity`` (``None`` when omitted —
        # SQLAlchemy 2.x has no class-name default).
        self.polymorphic_identity: object = None
        # identity -> concrete class; shared across the whole hierarchy so
        # root queries resolve any descendant.
        self.polymorphic_map: dict[object, type[object]] = {}
        # This mapper plus every descendant, in registration order — the
        # identity subtree used by subclass SELECT/bulk-DML filters.
        self.self_and_descendants: list[Mapper[Any]] = [self]
        # Joined-table inheritance shape. ``table_levels`` lists the distinct
        # tables in the hierarchy chain root->leaf (one entry for ordinary
        # and STI mappers); ``select_*`` describes a whole-entity SELECT —
        # for JTI the root table leads the FROM with inner joins down the
        # chain and the columns/attrs pairs follow SQLAlchemy's ordering
        # (PK columns leaf->root, then non-PK columns root->leaf).
        self.table_levels: tuple[_TableLevel, ...] = ()
        self.select_columns: tuple[Column, ...] = tuple(self.attr_columns.values())
        self.select_attrs: tuple[str, ...] = attrs
        self.select_from: Table = table
        self.select_joins: tuple[tuple[object, ...], ...] = ()


class _TableLevel(NamedTuple):
    """One distinct table in an inheritance chain's DML/SELECT layout."""

    table: Table
    # The table's own primary-key column (its join key for JTI).
    pk_column: Column | None
    # Attribute name bound to ``pk_column`` at this level.
    pk_attr: str | None
    # Non-PK mapped attributes whose columns live on this table.
    attrs: tuple[str, ...]
    # For non-root levels: this table's column that joins to the previous
    # level (the PK/FK column under SQLAlchemy's JTI convention).
    join_column: Column | None


# -- relationships ---------------------------------------------------------

_MANYTOONE = "manytoone"
_ONETOMANY = "onetomany"
_MANYTOMANY = "manytomany"

# ``__dict__`` key holding each m2m relationship's committed remote-PK set —
# the flush-time diff baseline for association INSERT/DELETE emission.
_M2M_BASE = "_catalyst_m2m_base"

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
        "secondary",
        "secondary_local",
        "secondary_remote",
        "single_parent",
        "viewonly",
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
    # ``secondary`` is the association Table; ``secondary_local`` its column
    # FK-ing back to the declaring table, ``secondary_remote`` to the target.
    secondary: Table | None
    secondary_local: Column | None
    secondary_remote: Column | None
    single_parent: bool
    viewonly: bool


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
        "secondary",
        "primaryjoin",
        "secondaryjoin",
        "single_parent",
        "viewonly",
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
        secondary: Table | str | None = None,
        primaryjoin: Column | Expression | None = None,
        secondaryjoin: Column | Expression | None = None,
        single_parent: bool = False,
        viewonly: bool = False,
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
        self.secondary = secondary
        self.primaryjoin = primaryjoin
        self.secondaryjoin = secondaryjoin
        self.single_parent = single_parent
        self.viewonly = viewonly

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
        if prop.direction == _MANYTOMANY:
            # Replacing an unloaded many-to-many attribute loads the
            # committed membership first so the flush diff (and sibling
            # back-population) see the prior association rows.
            _ensure_loaded(instance, prop)
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
    secondary: Table | str | None = None,
    primaryjoin: Column | Expression | None = None,
    secondaryjoin: Column | Expression | None = None,
    single_parent: bool = False,
    viewonly: bool = False,
    **kw: object,
) -> Relationship[Any]:
    """Declare a relationship between mapped classes, mirroring SQLAlchemy's.

    ``argument`` may be a mapped class or its name; when omitted the target is
    resolved from the ``Mapped[...]`` annotation once the class exists
    (deferred mapper configuration).  ``secondary`` names an association
    table (or metadata table name) for many-to-many relationships;
    ``primaryjoin``/``secondaryjoin`` disambiguate its foreign-key paths
    (required for self-referential mappings); ``single_parent`` permits the
    ``delete-orphan`` cascade on a many-to-many; ``viewonly`` loads through
    the association without ever writing rows.
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
        secondary=secondary,
        primaryjoin=primaryjoin,
        secondaryjoin=secondaryjoin,
        single_parent=single_parent,
        viewonly=viewonly,
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
    # FK inference searches every inheritance-table level: relationships
    # conventionally reference the hierarchy root, but a joined-table child
    # can also carry the foreign key.
    local_names = {level.table.name for level in mapper.table_levels}
    target_names = {level.table.name for level in target_mapper.table_levels}
    secondary: object = decl.secondary
    if secondary is not None:
        # A string ``secondary`` resolves against the declaring mapper's
        # MetaData registry; an unresolved name defers like an unresolved
        # class target so later mapper passes can still succeed.
        # Runtime guard for out-of-contract callers passing non-Table objects.
        if not isinstance(secondary, (Table, str)):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ArgumentError(
                f"relationship() secondary= expected a Table or table name, got "
                f"{type(decl.secondary).__name__!r}"
            )
        if isinstance(secondary, str):
            secondary = mapper.local_table.metadata.tables.get(secondary)
            if secondary is None:
                return False
        return _configure_secondary(decl, mapper, target, target_mapper, secondary)
    a_fks = [
        column
        for level in mapper.table_levels
        for column in level.table.columns
        if column.foreign_key is not None and column.foreign_key.target_table in target_names
    ]
    b_fks = [
        column
        for level in target_mapper.table_levels
        for column in level.table.columns
        if column.foreign_key is not None and column.foreign_key.target_table in local_names
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
    prop.secondary = None
    prop.secondary_local = None
    prop.secondary_remote = None
    prop.single_parent = bool(decl.single_parent)
    prop.viewonly = bool(decl.viewonly)
    fk_col: Column | None = None
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
    _register_relationship(decl, mapper, prop, target, target_mapper)
    return True


def _register_relationship(
    decl: Relationship[Any],
    mapper: Mapper[Any],
    prop: RelationshipProperty,
    target: type[object],
    target_mapper: Mapper[Any],
) -> None:
    """Install a configured property and materialize any ``backref`` peer."""
    mapper.relationships[prop.key] = prop
    decl.configured = prop
    if decl in mapper.pending_relationships:
        mapper.pending_relationships.remove(decl)
    # ``backref`` materializes the reverse attribute on the target class.
    if decl.backref is not None and decl.backref not in target_mapper.relationships:
        if not any(d.key == decl.backref for d in target_mapper.pending_relationships):
            rev: Relationship[Any] = Relationship(None, back_populates=prop.key)
            # A m2m backref joins through the same association table with the
            # explicit join directions swapped, like SQLAlchemy.
            rev.secondary = prop.secondary
            rev.primaryjoin = decl.secondaryjoin
            rev.secondaryjoin = decl.primaryjoin
            rev.key = decl.backref
            rev.declaring_cls = target
            setattr(target, decl.backref, rev)
            target_mapper.relationship_attrs += (decl.backref,)
            target_mapper.pending_relationships.append(rev)
            _configure_relationship(rev, target_mapper)


def _join_node(clause: object) -> tuple[object, ...]:
    """Serialized AST node for a ``primaryjoin``/``secondaryjoin`` clause."""
    if isinstance(clause, Column):
        return clause.serialize()
    if isinstance(clause, Expression):
        return clause.node
    return ()


def _assoc_column_in(node: object, assoc_name: str) -> str | None:
    """First ``("column", assoc, name)`` leaf inside a serialized join node."""
    if isinstance(node, tuple):
        parts = cast("tuple[object, ...]", node)
        if parts and parts[0] == "column" and len(parts) >= 3 and parts[1] == assoc_name:
            return cast(str, parts[2])
        for part in parts:
            found = _assoc_column_in(part, assoc_name)
            if found is not None:
                return found
    return None


def _configure_secondary(
    decl: Relationship[Any],
    mapper: Mapper[Any],
    target: type[object],
    target_mapper: Mapper[Any],
    secondary: Table,
) -> bool:
    """Configure a ``secondary=`` declaration as a many-to-many relationship.

    The two foreign-key paths through the association table are inferred
    from each column's ``ForeignKey.target_table``; ``primaryjoin`` /
    ``secondaryjoin`` override the inference per side (and are required for
    self-referential mappings, where both paths are ambiguous).
    """
    table = mapper.local_table
    target_table = target_mapper.local_table
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
    prop.direction = _MANYTOMANY
    prop.secondary = secondary
    prop.secondary_local = None
    prop.secondary_remote = None
    prop.single_parent = bool(decl.single_parent)
    prop.viewonly = bool(decl.viewonly)
    if "delete-orphan" in decl.cascade and not prop.single_parent:
        raise ArgumentError(
            f"'{prop.key}' relationship with delete-orphan cascade on a "
            "many-to-many relationship must have single_parent=True set"
        )
    if mapper.pk_column is None or mapper.pk_attr is None:
        raise ArgumentError(f"'{mapper.class_.__name__}' has no primary key")
    if target_mapper.pk_column is None or target_mapper.pk_attr is None:
        raise ArgumentError(f"Target '{target.__name__}' has no primary key")
    # Association foreign keys conventionally target each hierarchy's root
    # table, so the join columns are the root-level primary keys.
    local_pk = cast("Column", mapper.table_levels[0].pk_column or mapper.pk_column)
    remote_pk = cast("Column", target_mapper.table_levels[0].pk_column or target_mapper.pk_column)
    prop.local_col = local_pk
    prop.local_attr = mapper.pk_attr
    prop.remote_col = remote_pk
    prop.remote_attr = target_mapper.pk_attr
    self_ref = table.name == target_table.name
    if decl.primaryjoin is not None:
        found = _assoc_column_in(_join_node(decl.primaryjoin), secondary.name)
        if found is not None:
            prop.secondary_local = next(
                column for column in secondary.columns if column.name == found
            )
    if decl.secondaryjoin is not None:
        found = _assoc_column_in(_join_node(decl.secondaryjoin), secondary.name)
        if found is not None:
            prop.secondary_remote = next(
                column for column in secondary.columns if column.name == found
            )
    if self_ref and (prop.secondary_local is None or prop.secondary_remote is None):
        raise AmbiguousForeignKeysError(
            f"Could not determine join condition between parent and child tables "
            f"on relationship '{mapper.class_.__name__}.{prop.key}' - there are "
            f"multiple foreign key paths linking the tables via secondary table "
            f"'{secondary.name}'. Specify the 'primaryjoin' and 'secondaryjoin' "
            "keyword arguments."
        )
    local_names = {level.table.name for level in mapper.table_levels}
    target_names = {level.table.name for level in target_mapper.table_levels}
    if prop.secondary_local is None:
        candidates = [
            column
            for column in secondary.columns
            if column.foreign_key is not None and column.foreign_key.target_table in local_names
        ]
        if len(candidates) != 1:
            raise AmbiguousForeignKeysError(
                f"Could not determine join condition between '{mapper.class_.__name__}' "
                f"and '{target.__name__}' via secondary table '{secondary.name}' "
                f"({len(candidates)} foreign key paths to '{table.name}')"
            )
        prop.secondary_local = candidates[0]
    if prop.secondary_remote is None:
        candidates = [
            column
            for column in secondary.columns
            if column.foreign_key is not None and column.foreign_key.target_table in target_names
        ]
        if len(candidates) != 1:
            raise AmbiguousForeignKeysError(
                f"Could not determine join condition between '{mapper.class_.__name__}' "
                f"and '{target.__name__}' via secondary table '{secondary.name}' "
                f"({len(candidates)} foreign key paths to '{target_table.name}')"
            )
        prop.secondary_remote = candidates[0]
    uselist = decl.uselist
    if uselist is None:
        if decl.container == "collection":
            uselist = True
        elif decl.container == "scalar":
            uselist = False
        else:
            # Many-to-many collections default to uselist=True on both sides.
            uselist = True
    prop.uselist = bool(uselist)
    _register_relationship(decl, mapper, prop, target, target_mapper)
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


def _pk_value(uow: NativeUow, obj: object) -> object:
    """Primary key of ``obj`` — uow-stored first, then the mapped attribute."""
    pk = uow.pk_of(obj)
    if pk is None:
        pk_attr = _mapper_of(obj).pk_attr
        if pk_attr is not None:
            pk = obj.__dict__.get(pk_attr)
    return pk


def _ensure_loaded(obj: object, prop: RelationshipProperty) -> None:
    """Materialize a persistent object's unloaded relationship attribute.

    SQLAlchemy lazy-loads relationship state before mutating it; doing the
    same keeps the in-memory collection authoritative and gives the m2m
    flush diff a committed baseline for pair deletes.
    """
    if prop.key in obj.__dict__:
        return
    uow = _state_of(obj)
    if uow is None or not uow.is_managed(obj):
        return
    if uow.status(obj) != _STATUS_PERSISTENT:
        return
    session = uow.session
    if session is not None:
        cast("Session", session)._lazy_load(obj, prop)  # pyright: ignore[reportPrivateUsage]


def _m2m_committed(instance_dict: dict[str, object], key: str) -> frozenset[object] | None:
    """Committed remote-PK set for a m2m attribute, or ``None`` if unobserved."""
    base = instance_dict.get(_M2M_BASE)
    if not isinstance(base, dict):
        return None
    return cast("dict[str, frozenset[object]]", base).get(key)


def _m2m_committed_set(instance_dict: dict[str, object], key: str, pks: frozenset[object]) -> None:
    """Record the committed remote-PK set for a m2m attribute."""
    base = instance_dict.get(_M2M_BASE)
    if not isinstance(base, dict):
        base = {}
        instance_dict[_M2M_BASE] = base
    cast("dict[str, frozenset[object]]", base)[key] = pks


def _is_deferred_bind(value: object) -> bool:
    """Whether ``value`` is a deferred ``("__catalyst_rowid__", dep)`` marker."""
    if not isinstance(value, tuple):
        return False
    pair = cast("tuple[object, ...]", value)
    return len(pair) == 2 and pair[0] == "__catalyst_rowid__"


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
    if uow is not None and not (prop.direction == _MANYTOMANY and prop.viewonly):
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
    if prop.direction == _MANYTOMANY and not prop.viewonly:
        uow = _state_of(owner)
        if uow is not None and uow.is_managed(owner):
            uow.attr_set(owner, prop.key)
    sibling = prop.sibling
    if sibling is not None:
        if sibling.uselist:
            if sibling.direction == _MANYTOMANY:
                _ensure_loaded(item, sibling)
            other = _collection_for(item, sibling)
            if owner not in other:
                list[object].append(other, owner)
        else:
            if sibling.direction == _MANYTOMANY:
                _ensure_loaded(item, sibling)
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
    if prop.direction == _MANYTOMANY and not prop.viewonly:
        uow = _state_of(owner)
        if uow is not None and uow.is_managed(owner):
            uow.attr_set(owner, prop.key)
    sibling = prop.sibling
    if sibling is not None:
        if sibling.uselist:
            if sibling.direction == _MANYTOMANY:
                _ensure_loaded(item, sibling)
            other = getattr(item, "__dict__", {}).get(sibling.key)
            if isinstance(other, list) and owner in other:
                list[object].remove(cast("list[object]", other), owner)
        else:
            if sibling.direction == _MANYTOMANY:
                _ensure_loaded(item, sibling)
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
        detached = instance_dict.get(_DETACHED)
        if detached is not None and prop.key in detached[2]:
            raise DetachedInstanceError(
                f"Instance '<{type(instance).__name__} at 0x{id(instance):x}>' "
                "is not bound to a Session; attribute refresh operation "
                "cannot proceed"
            )
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
    if prop.uselist and prop.direction in (_ONETOMANY, _MANYTOMANY):
        if item is None:
            if prop.direction == _MANYTOMANY:
                # Eager loaders stitch empty collections too — the loaded
                # (empty) membership is committed state.
                _collection_for(owner, prop)
                if _m2m_committed(instance_dict, prop.key) is None:
                    _m2m_committed_set(instance_dict, prop.key, frozenset())
            return
        collection = _collection_for(owner, prop)
        if item not in collection:
            list[object].append(collection, item)
        if prop.direction == _MANYTOMANY:
            # Eagerly loaded membership is committed state: fold the item's
            # pk into the association diff baseline.
            uow = _state_of(owner)
            if uow is not None:
                item_pk = _pk_value(uow, item)
                if item_pk is not None:
                    base = _m2m_committed(instance_dict, prop.key)
                    _m2m_committed_set(instance_dict, prop.key, (base or frozenset()) | {item_pk})
        if sibling is not None and not sibling.uselist:
            item.__dict__[sibling.key] = owner
    else:
        instance_dict[prop.key] = item
        if prop.direction == _MANYTOMANY:
            uow = _state_of(owner)
            item_pk = None if item is None or uow is None else _pk_value(uow, item)
            _m2m_committed_set(
                instance_dict,
                prop.key,
                frozenset() if item_pk is None else frozenset({item_pk}),
            )
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


def _first_mapped_ancestor(cls: type[object]) -> Mapper[Any] | None:
    """Nearest base class carrying its own ``__mapper__``, if any."""
    for base in cls.__mro__[1:]:
        mapper = base.__dict__.get("__mapper__")
        if isinstance(mapper, Mapper):
            return cast("Mapper[Any]", mapper)
    return None


def _inheritance_parent(cls: type[object], mapper_args: object) -> Mapper[Any] | None:
    """Resolve the inheritance parent: ``__mapper_args__['inherits']`` or MRO."""
    if isinstance(mapper_args, dict) and "inherits" in mapper_args:
        candidate: object = cast("dict[object, object]", mapper_args)["inherits"]
        resolved: object = cast(
            "object",
            candidate if isinstance(candidate, Mapper) else getattr(candidate, "__mapper__", None),
        )
        if not isinstance(resolved, Mapper):
            raise ArgumentError(
                f"__mapper_args__['inherits'] on {cls.__name__} does not resolve to a mapped class"
            )
        return cast("Mapper[Any]", resolved)
    return _first_mapped_ancestor(cls)


def _mapped_annotations(
    cls: type[object], stop_at_mapped: bool
) -> list[tuple[str, object, type[object]]]:
    """``(name, raw annotation, declaring class)`` triples along the MRO.

    The concrete class's own declarations come first, then each unmapped
    ancestor's in MRO order (SQLAlchemy's mixin/abstract column layout); the
    first declaration wins on collisions.  ``stop_at_mapped`` ends the walk
    at a base carrying its own ``__mapper__`` — its attributes are already
    mapped on the inheritance parent.
    """
    triples: list[tuple[str, object, type[object]]] = []
    seen: set[str] = set()
    for klass in cls.__mro__:
        if klass is object or klass is DeclarativeBase:
            continue
        if (
            klass is not cls
            and stop_at_mapped
            and isinstance(klass.__dict__.get("__mapper__"), Mapper)
        ):
            break
        annotations = cast("dict[str, object]", klass.__dict__.get("__annotations__", {}))
        for name, raw in annotations.items():
            if name in seen or name.startswith("_") or name == "metadata":
                continue
            seen.add(name)
            triples.append((name, raw, klass))
    return triples


def _bare_declared_members(
    cls: type[object], stop_at_mapped: bool
) -> list[tuple[str, object, type[object]]]:
    """``(name, value, declaring class)`` bare mapped members along the MRO."""
    triples: list[tuple[str, object, type[object]]] = []
    for klass in cls.__mro__:
        if klass is object or klass is DeclarativeBase:
            continue
        if (
            klass is not cls
            and stop_at_mapped
            and isinstance(klass.__dict__.get("__mapper__"), Mapper)
        ):
            break
        for name, value in cast("dict[str, object]", klass.__dict__).items():
            if not name.startswith("_") and isinstance(value, (MappedColumn, Relationship)):
                triples.append((name, cast("object", value), klass))
    return triples


def _collect_mapped_members(
    cls: type[object], hints: dict[str, object], stop_at_mapped: bool
) -> tuple[
    list[tuple[str, Column]],
    dict[str, MappedColumn[Any] | None],
    list[str],
    list[Relationship[Any]],
]:
    """Collect ``(attr, unbound Column)`` pairs, specs, and relationship decls.

    Annotations and bare ``mapped_column()``/``relationship()`` assignments
    are gathered from the class and its unmapped ancestors (abstract bases
    and mixins); ``stop_at_mapped`` stops the ancestor walk at a mapped
    base for single-table-inheritance subclasses.
    """
    attr_columns: list[tuple[str, Column]] = []
    specs: dict[str, MappedColumn[Any] | None] = {}
    rel_attrs: list[str] = []
    rel_decls: list[Relationship[Any]] = []
    for name, raw, declaring in _mapped_annotations(cls, stop_at_mapped):
        spec_value = declaring.__dict__.get(name)
        if name in cls.__dict__:
            spec_value = cls.__dict__[name]
        hint = hints.get(name)
        if get_origin(hint) is ClassVar:
            continue
        if isinstance(spec_value, Relationship):
            decl = cast("Relationship[Any]", spec_value)
            decl.key = name
            decl.declaring_cls = declaring
            decl.container, decl.target_hint = _rel_annotation_for(declaring, hint, raw)
            rel_attrs.append(name)
            rel_decls.append(decl)
            continue
        spec = cast(
            "MappedColumn[Any] | None",
            spec_value if isinstance(spec_value, MappedColumn) else None,
        )
        resolved = hint if hint is not None else _eval_annotation(declaring, raw)
        if resolved is not None and get_origin(resolved) is Mapped:
            args = get_args(resolved)
            optional: bool | None
            optional, inner = _unwrap_optional(args[0] if args else None)
        elif spec is None:
            if resolved is None or declaring is not cls:
                # Non-Mapped annotations on ancestors are ignored (SQLAlchemy
                # only maps Mapped[...] attributes); the class's own still
                # raise.
                continue
            raise ArgumentError(
                f"Attribute '{name}' on {cls.__name__} uses a non-Mapped annotation; "
                "declarative attributes must be annotated with Mapped[...]"
            )
        else:
            optional, inner = None, None
        attr_columns.append((name, _column_for_attr(cls, name, spec, inner, optional)))
        specs[name] = spec
    declared_names = {name for name, _column in attr_columns}
    for name, value, declaring in _bare_declared_members(cls, stop_at_mapped):
        if isinstance(value, MappedColumn):
            if name in declared_names:
                continue
            attr_columns.append(
                (
                    name,
                    _column_for_attr(cls, name, cast("MappedColumn[Any]", value), None, None),
                )
            )
            declared_names.add(name)
            specs[name] = value
        elif isinstance(value, Relationship) and name not in rel_attrs:
            decl_value = cast("Relationship[Any]", value)
            decl_value.key = name
            decl_value.declaring_cls = declaring
            rel_attrs.append(name)
            rel_decls.append(decl_value)
    return attr_columns, specs, rel_attrs, rel_decls


def _type_hints(cls: type[object]) -> dict[str, object]:
    try:
        return get_type_hints(cls)
    except Exception:
        # Forward references such as Mapped[list["Child"]] cannot resolve
        # eagerly; annotations are parsed per attribute and relationship
        # targets resolve at configuration time instead.
        return {}


def _polymorphic_on_column(mapper: Mapper[Any], spec: object) -> Column:
    """Resolve a ``polymorphic_on`` argument to a mapped ``Column``."""
    if isinstance(spec, Column):
        return spec
    if isinstance(spec, str):
        column = mapper.attr_columns.get(spec)
        if column is None:
            column = next((c for c in mapper.local_table.columns if c.name == spec), None)
        if column is not None:
            return column
    raise ArgumentError(
        f"Can't determine polymorphic_on value '{spec}' - no attribute is mapped to this name."
    )


def _configure_inheritance(mapper: Mapper[Any], mapper_args: dict[object, object]) -> None:
    """Apply ``__mapper_args__`` inheritance keys to a mapper.

    ``polymorphic_on`` names a mapped attribute holding the discriminator;
    ``polymorphic_identity`` registers this class in the hierarchy's
    identity map.  Like SQLAlchemy 2.x there is no default identity — a
    polymorphic mapper without one warns and stores ``None`` (INSERTs write
    NULL).
    """
    polymorphic_on = mapper_args.get("polymorphic_on")
    if polymorphic_on is not None:
        column = _polymorphic_on_column(mapper, polymorphic_on)
        mapper.polymorphic_on = column
        mapper.polymorphic_on_attr = _attr_for_column(mapper, column)
    if "polymorphic_identity" in mapper_args:
        mapper.polymorphic_identity = mapper_args["polymorphic_identity"]
    if mapper.polymorphic_on is None:
        return
    if mapper.polymorphic_identity is not None:
        mapper.polymorphic_map[mapper.polymorphic_identity] = mapper.class_
    elif "polymorphic_identity" not in mapper_args:
        warnings.warn(
            f"Mapper {mapper.class_.__name__} does not indicate a "
            "'polymorphic_identity', yet is part of an inheritance "
            "hierarchy that has a 'polymorphic_on' column. INSERTed rows "
            "will store NULL in the discriminator column.",
            SAWarning,
            stacklevel=2,
        )


def _map_sti_subclass(cls: type[object], parent_mapper: Mapper[Any], mapper_args: object) -> None:
    """Map ``cls`` into ``parent_mapper``'s table (single-table inheritance).

    The subclass shares the parent's ``Table``: its own mapped columns are
    appended (nullable for sibling rows) and its mapper inherits the
    parent's attributes plus its new ones — SQLAlchemy's STI layout.
    """
    cls_any = cast(Any, cls)
    table = parent_mapper.local_table
    cls_any.metadata = table.metadata
    cls_any.__table__ = table
    attr_columns, specs, rel_attrs, rel_decls = _collect_mapped_members(
        cls, _type_hints(cls), stop_at_mapped=True
    )
    # Subclass columns append to the shared table; re-declared names map
    # back onto the existing column.
    merged: dict[str, Column] = dict(parent_mapper.attr_columns)
    append_pairs: list[tuple[str, Column]] = []
    for name, column in attr_columns:
        existing = next((c for c in table.columns if c.name == column.name), None)
        if existing is not None:
            merged[name] = existing
        else:
            append_pairs.append((name, column))
    appended = table._append_columns(  # pyright: ignore[reportPrivateUsage]
        [column for _name, column in append_pairs]
    )
    for (name, _unbound), column in zip(append_pairs, appended, strict=True):
        merged[name] = column
    attrs = [
        *parent_mapper.attrs,
        *(name for name, _column in attr_columns if name not in parent_mapper.attr_columns),
    ]
    mapper = Mapper(cls, table, tuple(attrs), {**parent_mapper.specs, **specs}, merged)
    mapper.relationship_attrs = tuple(
        dict.fromkeys([*parent_mapper.relationship_attrs, *rel_attrs])
    )
    mapper.relationships = dict(parent_mapper.relationships)
    mapper.pending_relationships = list(rel_decls)
    mapper.class_registry = parent_mapper.class_registry
    mapper.inherits = parent_mapper
    mapper.base_mapper = parent_mapper.base_mapper
    mapper.polymorphic_on = parent_mapper.polymorphic_on
    mapper.polymorphic_on_attr = parent_mapper.polymorphic_on_attr
    # Every mapper in the hierarchy shares the root's identity map.
    mapper.polymorphic_map = parent_mapper.base_mapper.polymorphic_map
    mapper.self_and_descendants = [mapper]
    ancestor: Mapper[Any] | None = parent_mapper
    while ancestor is not None:
        ancestor.self_and_descendants.append(mapper)
        ancestor = ancestor.inherits
    # An STI subclass of a joined-table mapper still needs the joined
    # SELECT spec — it shares the leaf table but reads through the join.
    _init_table_spec(mapper)
    cls_any.__mapper__ = mapper
    registry = parent_mapper.class_registry
    registry[cls.__name__] = cls
    registry.setdefault(f"{cls.__module__}.{cls.__qualname__}", cls)
    _MAPPERS.append(mapper)
    for name, column in merged.items():
        if name in parent_mapper.attr_columns:
            continue
        object.__setattr__(column, "key", name)
        setattr(cls, name, column)
    _configure_inheritance(
        mapper,
        cast("dict[object, object]", mapper_args) if isinstance(mapper_args, dict) else {},
    )
    configure_mappers()


def _init_table_spec(mapper: Mapper[Any]) -> None:
    """Compute ``table_levels`` and the whole-entity SELECT spec for a mapper.

    The ``inherits`` chain is walked root -> leaf, collapsing links that share
    a table (STI subclasses contribute their attributes to the level they
    share).  With more than one level the mapper is a joined-table
    inheritance mapper: whole-entity SELECTs go ``FROM`` the root table with
    inner joins down the chain, ordered like SQLAlchemy — PK columns
    leaf -> root (later duplicates alias ``id_1`` ...) then non-PK columns
    root -> leaf.
    """
    chain: list[Mapper[Any]] = []
    link: Mapper[Any] | None = mapper
    while link is not None:
        chain.append(link)
        link = link.inherits
    chain.reverse()
    levels: list[_TableLevel] = []
    for link_mapper in chain:
        table = link_mapper.local_table
        if levels and levels[-1].table is table:
            # STI link sharing the previous level's table.
            continue
        pk_column = next((column for column in table.columns if column.primary_key), None)
        pk_attr = next(
            (attr for attr, column in link_mapper.attr_columns.items() if column is pk_column),
            None,
        )
        non_pk = tuple(
            attr
            for attr, column in mapper.attr_columns.items()
            if column.owner is table and column is not pk_column
        )
        join_column: Column | None = None
        if levels:
            parent_table = levels[-1].table
            join_column = next(
                (
                    column
                    for column in table.columns
                    if column.foreign_key is not None
                    and column.foreign_key.target_table == parent_table.name
                ),
                None,
            )
            if join_column is None:
                raise ArgumentError(
                    f"Could not determine join condition between parent "
                    f"table '{parent_table.name}' and child table "
                    f"'{table.name}' on joined-table-inheritance mapper "
                    f"{mapper.class_.__name__}; child tables must foreign-key "
                    "to the parent primary key"
                )
        levels.append(_TableLevel(table, pk_column, pk_attr, non_pk, join_column))
    mapper.table_levels = tuple(levels)
    if len(levels) == 1:
        mapper.select_columns = tuple(mapper.attr_columns.values())
        mapper.select_attrs = mapper.attrs
        mapper.select_from = levels[0].table
        mapper.select_joins = ()
        return
    columns: list[Column] = []
    attrs: list[str] = []
    for level in reversed(levels):
        if level.pk_column is not None:
            columns.append(cast("Column", level.pk_column))
            attrs.append(cast("str", level.pk_attr))
    for level in levels:
        for attr in level.attrs:
            columns.append(mapper.attr_columns[attr])
            attrs.append(attr)
    joins: list[tuple[object, ...]] = []
    for previous, level in zip(levels[:-1], levels[1:], strict=True):
        if previous.pk_column is None or level.join_column is None:
            continue
        joins.append(
            (
                "join",
                level.table.name,
                None,
                (
                    "binary",
                    cast("Column", previous.pk_column).serialize(),
                    "=",
                    cast("Column", level.join_column).serialize(),
                ),
            )
        )
    mapper.select_columns = tuple(columns)
    mapper.select_attrs = tuple(attrs)
    mapper.select_from = levels[0].table
    mapper.select_joins = tuple(joins)


def _deferred_level(mapper: Mapper[Any], attr: str, expired: Iterable[str]) -> _TableLevel | None:
    """The joined-table level ``attr`` lazy-loads from, or ``None``.

    A parent query that resolved to a subclass leaves exactly the subclass
    tables' attributes expired; accessing one loads just that child table's
    columns (SQLAlchemy's deferred subclass-column SELECT).  Any expired
    attribute on the hierarchy root means whole-row expiry — the refresh
    then reads through the full join instead.
    """
    levels = mapper.table_levels
    if len(levels) < 2:
        return None
    expired_set = set(expired)
    if expired_set & set(levels[0].attrs):
        return None
    for level in levels[1:]:
        if attr in expired_set and attr in level.attrs:
            return level
    return None


def _entity_select_columns(mapper: Mapper[Any]) -> tuple[Column | Expression, ...]:
    """SELECT items for a mapper's whole-entity column list.

    Marking each column ``entity_column`` makes the compiler label repeats
    ``id_1``-style like SQLAlchemy's ORM loads; plain repeated columns
    dedupe ``id__1``.
    """
    return tuple(
        Expression(
            (
                "entity_column",
                column.table_name,
                column.name,
                _column_type_tag(column.type),
            )
        )
        for column in mapper.select_columns
    )


def _map_jti_subclass(
    cls: type[object],
    parent_mapper: Mapper[Any],
    tablename: object,
    mapper_args: object,
) -> None:
    """Map ``cls`` onto its own table under ``parent_mapper`` (JTI).

    The subclass declares ``__tablename__`` plus a primary-key column that
    foreign-keys to the parent table — SQLAlchemy's joined-table
    inheritance.  Its mapper keeps the hierarchy's attributes in
    ``attr_columns`` (child declarations win name collisions) while
    ``table_levels`` tracks the per-table split for flush DML and the
    joined SELECT layout.
    """
    cls_any = cast(Any, cls)
    attr_columns, specs, rel_attrs, rel_decls = _collect_mapped_members(
        cls, _type_hints(cls), stop_at_mapped=True
    )
    if not attr_columns:
        raise ArgumentError(f"{cls.__name__} declares __tablename__ but no mapped attributes")
    metadata = parent_mapper.local_table.metadata
    cls_any.metadata = metadata
    table = Table(str(tablename), metadata, *(column for _name, column in attr_columns))
    cls_any.__table__ = table
    merged: dict[str, Column] = dict(parent_mapper.attr_columns)
    for (name, _unbound), column in zip(attr_columns, table.columns, strict=True):
        merged[name] = column
    attrs = [
        *parent_mapper.attrs,
        *(name for name, _column in attr_columns if name not in parent_mapper.attr_columns),
    ]
    mapper = Mapper(cls, table, tuple(attrs), {**parent_mapper.specs, **specs}, merged)
    mapper.relationship_attrs = tuple(
        dict.fromkeys([*parent_mapper.relationship_attrs, *rel_attrs])
    )
    mapper.relationships = dict(parent_mapper.relationships)
    mapper.pending_relationships = list(rel_decls)
    mapper.class_registry = parent_mapper.class_registry
    mapper.inherits = parent_mapper
    mapper.base_mapper = parent_mapper.base_mapper
    mapper.polymorphic_on = parent_mapper.polymorphic_on
    mapper.polymorphic_on_attr = parent_mapper.polymorphic_on_attr
    # Every mapper in the hierarchy shares the root's identity map.
    mapper.polymorphic_map = parent_mapper.base_mapper.polymorphic_map
    mapper.self_and_descendants = [mapper]
    ancestor: Mapper[Any] | None = parent_mapper
    while ancestor is not None:
        ancestor.self_and_descendants.append(mapper)
        ancestor = ancestor.inherits
    _init_table_spec(mapper)
    cls_any.__mapper__ = mapper
    registry = parent_mapper.class_registry
    registry[cls.__name__] = cls
    registry.setdefault(f"{cls.__module__}.{cls.__qualname__}", cls)
    _MAPPERS.append(mapper)
    for name, column in merged.items():
        if parent_mapper.attr_columns.get(name) is column:
            continue
        object.__setattr__(column, "key", name)
        setattr(cls, name, column)
    _configure_inheritance(
        mapper,
        cast("dict[object, object]", mapper_args) if isinstance(mapper_args, dict) else {},
    )
    configure_mappers()


def _map_declarative_class(cls: type[object]) -> None:
    """Build a ``Table`` and ``Mapper`` for a ``DeclarativeBase`` subclass."""
    cls_any = cast(Any, cls)
    tablename = cls.__dict__.get("__tablename__")
    mapper_args = cls.__dict__.get("__mapper_args__")
    abstract = bool(cls.__dict__.get("__abstract__", False))
    parent_mapper = _inheritance_parent(cls, mapper_args)
    if abstract or tablename is None:
        if not abstract and parent_mapper is not None:
            # A subclass without ``__tablename__`` under a mapped parent
            # joins the parent's single-table-inheritance hierarchy.
            _map_sti_subclass(cls, parent_mapper, mapper_args)
            return
        # Intermediate/abstract base: fresh metadata unless one is inherited.
        if not isinstance(getattr(cls, "metadata", None), MetaData):
            cls_any.metadata = MetaData()
        return
    if parent_mapper is not None:
        # A subclass declaring ``__tablename__`` under a mapped parent is
        # joined-table inheritance: the child table carries only its own
        # columns and links back via a primary-key foreign key.
        _map_jti_subclass(cls, parent_mapper, tablename, mapper_args)
        return
    metadata = getattr(cls, "metadata", None)
    if not isinstance(metadata, MetaData):
        metadata = MetaData()
        cls_any.metadata = metadata
    registry = _registry_for(metadata)
    attr_columns, specs, rel_attrs, rel_decls = _collect_mapped_members(
        cls, _type_hints(cls), stop_at_mapped=False
    )
    if not attr_columns:
        raise ArgumentError(f"{cls.__name__} declares __tablename__ but no mapped attributes")
    table = Table(str(tablename), metadata, *(column for _name, column in attr_columns))
    cls_any.__table__ = table
    mapper = Mapper(cls, table, tuple(name for name, _column in attr_columns), specs)
    mapper.relationship_attrs = tuple(rel_attrs)
    mapper.pending_relationships = list(rel_decls)
    mapper.class_registry = registry
    _init_table_spec(mapper)
    cls_any.__mapper__ = mapper
    registry[cls.__name__] = cls
    registry.setdefault(f"{cls.__module__}.{cls.__qualname__}", cls)
    _MAPPERS.append(mapper)
    for (attr, _unbound), column in zip(attr_columns, table.columns, strict=True):
        # Bind the mapped attribute name onto the class-level Column so its
        # ``__get__`` resolves the correct ``__dict__`` key on instances (a
        # column name can differ from the attribute name).
        object.__setattr__(column, "key", attr)
        setattr(cls, attr, column)
    if isinstance(mapper_args, dict):
        _configure_inheritance(mapper, cast("dict[object, object]", mapper_args))
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


def _require_class_mapper(cls: type[object]) -> Mapper[Any]:
    """Return the mapper for a mapped *class* (bulk APIs take the class)."""
    mapper = getattr(cls, "__mapper__", None)
    if mapper is None:
        raise UnmappedInstanceError(f"Class '{cls.__module__}.{cls.__qualname__}' is not mapped")
    return cast("Mapper[Any]", mapper)


def _detached_marker(
    instance_dict: dict[str, object],
) -> tuple[object, tuple[str, ...], tuple[str, ...]] | None:
    """The ``_catalyst_detached`` marker triple, or ``None``."""
    marker = instance_dict.get(_DETACHED)
    triple = cast("tuple[object, ...]", marker) if isinstance(marker, tuple) else ()
    return cast(
        "tuple[object, tuple[str, ...], tuple[str, ...]] | None",
        triple if len(triple) == 3 else None,
    )


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
    # The m2m committed-membership baseline is only valid while the
    # in-memory state reflects the database; expiry invalidates both.
    instance.__dict__.pop(_M2M_BASE, None)


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
        self._engine: Engine | None = bind if isinstance(bind, Engine) else None
        self._connection: Connection | None = bind.connect() if isinstance(bind, Engine) else bind
        self.autoflush = autoflush
        self.expire_on_commit = expire_on_commit
        self._uow = NativeUow(self)
        self._txn_active = False
        self._pending_rollback = False
        self._flushing = False
        # Objects bulk-DML marked deleted: the DELETE already ran, so flush
        # drops their state without re-emitting a per-row DELETE.
        self._synced_deleted: set[int] = set()

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
        if self._pending_rollback:
            raise PendingRollbackError(
                "This Session's transaction has been rolled back due to a previous "
                "exception during flush. To begin a new transaction with this "
                "Session, first issue Session.rollback()."
            )
        self._ensure_connection()

    def _ensure_connection(self) -> None:
        """Lazily re-acquire an engine checkout after ``close()`` (SA reuse)."""
        if self._connection is None:
            if self._engine is None:
                raise RuntimeError("Session is closed")
            self._connection = self._engine.connect()

    def _conn(self) -> Connection:
        """The live connection, re-acquired when the session was closed."""
        self._ensure_connection()
        assert self._connection is not None  # noqa: S101
        return self._connection

    def _mark_active(self) -> None:
        self._txn_active = True

    @staticmethod
    def _raise_bind_error(compiled: Compiled) -> None:
        if compiled.bind_error is not None:
            raise _statement_error(compiled.bind_error, compiled.string, ())

    # -- add / delete ------------------------------------------------------

    def add(self, instance: object) -> None:
        """Attach ``instance`` to the session as pending insertion.

        Re-adding an object previously expunged from this session (or detached
        elsewhere) restores it as *persistent* with its stored primary key —
        matching SQLAlchemy, whose detached ``InstanceState.key`` survives
        expunge so ``add`` resumes the same identity rather than inserting.
        """
        self._check_active()
        mapper = _require_mapper(instance)
        uow = _state_of(instance)
        if uow is not None and uow.is_managed(instance):
            if uow is self._uow:
                # Re-adding a persistent object is a no-op; re-adding one that
                # is marked deleted cancels the pending delete.
                if uow.status(instance) == _STATUS_DELETED:
                    uow.un_delete(instance)
                    self._synced_deleted.discard(id(instance))
                return
            raise InvalidRequestError(
                f"Object '{instance}' is already attached to session "
                f"'{id(uow.session) & 0xFF}' (this is '{id(self) & 0xFF}')"
            )
        detached = _detached_marker(instance.__dict__)
        if detached is not None:
            pk, dirty, expired = detached
            del instance.__dict__[_DETACHED]
            self._uow.attach_persistent(instance, type(instance), list(mapper.attrs), pk)
            instance._catalyst_state = self._uow  # type: ignore[attr-defined]
            col_expired = [attr for attr in expired if attr in mapper.attr_columns]
            if col_expired:
                self._uow.expire_attrs(instance, col_expired)
            for attr in dirty:
                self._uow.attr_set(instance, attr)
            self._mark_active()
            self._cascade_save_update(instance, {id(instance)})
            return
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
                if prop.direction == _MANYTOMANY:
                    # Removing the member emits the association-row DELETE at
                    # flush; delete-orphan additionally removes the child row.
                    if orphan:
                        item_uow = _state_of(item)
                        if item_uow is not None and item_uow is self._uow:
                            status = item_uow.status(item)
                            if status == _STATUS_PERSISTENT:
                                self._apply_delete_cascade(item)
                                if not prop.uselist:
                                    item_uow.mark_deleted(item)
                            elif status == _STATUS_PENDING:
                                item_uow.detach(item)
                    if prop.uselist:
                        collection = instance.__dict__.get(prop.key)
                        if isinstance(collection, list) and item in collection:
                            list[object].remove(cast("list[object]", collection), item)
                        _collection_remove(instance, prop, item)
                    else:
                        prop.descriptor.__set__(instance, None)
                elif orphan:
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

    # -- detach ------------------------------------------------------------

    def _stamp_detached(
        self, instance: object, info: tuple[object, int, list[str], list[str]]
    ) -> None:
        """Stamp the detached marker after native detach, like SA's key.

        The ``(pk, dirty, expired)`` triple preserves ``InstanceState.key`` /
        ``.modified`` semantics across expunge so ``merge(load=False)`` can
        still reject dirty objects and ``add`` can re-attach persistently.
        """
        pk, status, dirty, expired = info
        instance.__dict__.pop("_catalyst_state", None)
        if pk is None or status == _STATUS_PENDING:
            return
        mapper = _mapper_of(instance)
        instance_dict = instance.__dict__
        rel_expired = [k for k in mapper.relationship_attrs if k not in instance_dict]
        instance_dict[_DETACHED] = (pk, tuple(dirty), tuple(expired) + tuple(rel_expired))

    def expunge(self, instance: object) -> None:
        """Detach ``instance`` from this session (no autoflush)."""
        _require_mapper(instance)
        self._expunge_instance(instance, set())

    def _expunge_instance(self, instance: object, seen: set[int]) -> None:
        """Detach one instance, cascading ``"expunge"`` to loaded relationships."""
        uow = _state_of(instance)
        if uow is None or uow is not self._uow or not uow.is_managed(instance):
            raise InvalidRequestError(f"Instance '{instance}' is not present in this Session")
        if id(instance) in seen:
            return
        seen.add(id(instance))
        mapper = _mapper_of(instance)
        _configure_mapper(mapper)
        for prop in mapper.relationships.values():
            if "expunge" not in prop.cascade:
                continue
            # Only already-loaded state cascades — an unloaded relationship
            # is left alone (it will raise DetachedInstanceError on access).
            value = instance.__dict__.get(prop.key)
            if value is None:
                continue
            items = cast("list[object]", value) if prop.uselist else [value]
            for item in items:
                if item is None or id(item) in seen:
                    continue
                item_uow = _state_of(item)
                if item_uow is self._uow and item_uow is not None and item_uow.is_managed(item):
                    self._expunge_instance(item, seen)
        info = uow.detach(instance)
        if info is not None:
            self._stamp_detached(instance, info)
        self._synced_deleted.discard(id(instance))

    def expunge_all(self) -> None:
        """Detach every managed object; loaded values stay on the instances."""
        for info in self._uow.detach_all():
            obj, pk, status, dirty, expired = info
            self._stamp_detached(obj, (pk, status, dirty, expired))
        self._synced_deleted.clear()

    # -- merge --------------------------------------------------------------

    def merge(self, instance: _T, load: bool = True) -> _T:
        """Copy ``instance``'s state onto this session's persistent instance.

        Returns the session's own instance for the incoming object's identity
        (usually *not* the passed object).  Autoflushes pending work like any
        other load path — only when ``autoflush`` is enabled — and cascades
        through ``"merge"`` relationship cascades recursively.
        """
        self._check_active()
        _require_mapper(instance)
        # SA: Session.merge calls _autoflush() — honored only when enabled.
        if self.autoflush:
            self.flush()
        return cast(_T, self._merge_instance(instance, load, {}))

    def _merge_instance(self, instance: object, load: bool, cache: dict[int, object]) -> object:
        """Recursive worker for :meth:`merge`; ``cache`` dedupes source objects."""
        hit = cache.get(id(instance))
        if hit is not None:
            return hit
        mapper = _require_mapper(instance)
        _configure_mapper(mapper)
        cls = mapper.class_
        instance_dict = instance.__dict__
        # Resolve the source object's identity + modified flag across the
        # three storage forms (managed uow state, detached marker, transient).
        src_uow = _state_of(instance)
        managed = src_uow is not None and src_uow.is_managed(instance)
        detached = None if managed else _detached_marker(instance_dict)
        if managed:
            assert src_uow is not None
            status = src_uow.status(instance)
            if status == _STATUS_PENDING:
                has_key, pk = (
                    False,
                    (instance_dict.get(mapper.pk_attr) if mapper.pk_attr is not None else None),
                )
            else:
                has_key, pk = True, src_uow.pk_of(instance)
            modified = bool(src_uow.dirty_attrs_of(instance))
        elif detached is not None:
            has_key, pk = True, detached[0]
            modified = bool(detached[1])
        else:
            has_key, pk = (
                False,
                (instance_dict.get(mapper.pk_attr) if mapper.pk_attr is not None else None),
            )
            modified = False
        key_is_persistent = pk is not None
        if not has_key and not load:
            raise InvalidRequestError(
                "load=False option does not support merging transient objects"
            )
        merged: object | None = self._uow.identity_get(cls, pk) if key_is_persistent else None
        if merged is None:
            if not load:
                # Only key-having states reach here under load=False (transient
                # and pending raised above).  SA: clean detached merges
                # straight into a persistent slot with no SELECT; dirty rejects.
                if modified:
                    raise InvalidRequestError(
                        "load=False option does not support merging dirty objects"
                    )
                merged = self._attach_persistent_stub(cls, mapper, pk)
            elif key_is_persistent:
                merged = self.get(cls, pk)
        if merged is None:
            merged = object.__new__(cls)
            self._uow.attach_pending(merged, cls, list(mapper.attrs))
            merged._catalyst_state = self._uow  # type: ignore[attr-defined]
        cache[id(instance)] = merged
        if merged is instance:
            if not load:
                # Self-merge of an in-session dirty object commits its state.
                self._uow.mark_clean(merged)
            return merged
        merged_dict = merged.__dict__
        for attr in mapper.attrs:
            if attr not in instance_dict:
                continue
            value = instance_dict[attr]
            if load:
                if merged_dict.get(attr, _MISSING) != value:
                    setattr(merged, attr, value)  # dirty only on actual change
            else:
                object.__setattr__(merged, attr, value)
        for prop in mapper.relationships.values():
            if "merge" not in prop.cascade or prop.key not in instance_dict:
                continue
            value = instance_dict[prop.key]
            if prop.uselist:
                items = [
                    self._merge_instance(item, load, cache)
                    for item in cast("list[object]", value)
                    if item is not None
                ]
                collection = _collection_for(merged, prop)
                for item in list(collection):
                    if item not in items:
                        list[object].remove(collection, item)
                for item in items:
                    if item not in collection:
                        _stitch_loaded(merged, prop, item)
            else:
                item = None if value is None else self._merge_instance(value, load, cache)
                _stitch_loaded(merged, prop, item)
        return merged

    def _attach_persistent_stub(self, cls: type[object], mapper: Mapper[Any], pk: object) -> object:
        """Empty persistent slot for ``merge(load=False)`` — no SELECT."""
        merged = object.__new__(cls)
        self._uow.attach_persistent(merged, cls, list(mapper.attrs), pk)
        merged._catalyst_state = self._uow  # type: ignore[attr-defined]
        return merged

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
        # ``persisted`` carries the op index whose rowid supplies each new
        # object's PK — the root-table op for joined-table subclasses.
        persisted: list[tuple[object, object, int]] = []
        cleaned: list[object] = []
        dropped: list[object] = []
        # FK assignments resolved after the batch from an earlier op's rowid.
        deferred_fk: list[tuple[object, str, int]] = []
        insert_index: dict[int, int] = {}
        new_order = self._dependency_order(uow.new_objects())
        deleted_all = self._dependent_first_order(uow.deleted_objects())
        # Bulk-DML-synchronized deletes already ran their SQL: they drop out
        # of the session here without emitting a per-row DELETE again.
        synced_deleted = [obj for obj in deleted_all if id(obj) in self._synced_deleted]
        deleted_order = [obj for obj in deleted_all if id(obj) not in self._synced_deleted]
        for obj in new_order:
            mapper = _mapper_of(obj)
            pk_attr = mapper.pk_attr
            pk_value = obj.__dict__.get(pk_attr) if pk_attr is not None else None
            rel_fk = self._pending_relationship_refs(obj, mapper)
            pk_op_index = -1
            # One INSERT per inheritance-table level, root -> leaf (JTI
            # subclasses emit parent then child; ordinary mappers emit one).
            for level_index, level in enumerate(mapper.table_levels):
                values: dict[str, object] = {}
                deferred: list[tuple[str, str, int]] = []
                level_pk = cast("Column | None", level.pk_column)
                if level_pk is not None:
                    if pk_value is not None:
                        values[level_pk.name] = pk_value
                    elif level_index > 0:
                        # JTI child row: the PK arrives from the parent
                        # INSERT's rowid inside this same batch (deferred).
                        values[level_pk.name] = None
                for attr in level.attrs:
                    column = mapper.attr_columns[attr]
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
                discriminator = mapper.polymorphic_on
                if discriminator is not None and discriminator.table_name == level.table.name:
                    # Auto-write the mapper's polymorphic identity (SQLAlchemy
                    # stamps the discriminator at INSERT; an explicitly set
                    # value wins, like SA's flush).
                    identity = mapper.polymorphic_identity
                    current = values.get(discriminator.name)
                    if current is None:
                        values[discriminator.name] = identity
                        if mapper.polymorphic_on_attr is not None:
                            obj.__dict__[mapper.polymorphic_on_attr] = identity
                    elif identity is not None and current != identity:
                        warnings.warn(
                            f"Flushing object {obj!r} with incompatible "
                            f"polymorphic identity {current!r}; the row may not "
                            "load correctly",
                            SAWarning,
                            stacklevel=2,
                        )
                compiled = Insert(level.table).values(values).compile()
                self._raise_bind_error(compiled)
                ordered = list(compiled._ordered)
                for col_name, attr, dep in deferred:
                    ordered[compiled._bind_names.index(col_name)] = ("__catalyst_rowid__", dep)
                    deferred_fk.append((obj, attr, dep))
                if level_index > 0 and pk_value is None and level_pk is not None:
                    ordered[compiled._bind_names.index(level_pk.name)] = (
                        "__catalyst_rowid__",
                        pk_op_index,
                    )
                ops.append(
                    (
                        compiled.string,
                        ordered,
                        compiled._bind_types,
                        level_index == 0 and pk_value is None and pk_attr is not None,
                    )
                )
                if level_index == 0:
                    pk_op_index = len(ops) - 1
                    insert_index[id(obj)] = pk_op_index
            persisted.append((obj, pk_value, pk_op_index))
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
            # One UPDATE per table level holding dirty columns — joined-table
            # subclasses split parent/child changes like SQLAlchemy.
            for level in mapper.table_levels:
                level_pk = level.pk_column
                level_values = {
                    mapper.attr_columns[attr].name: changes[attr]
                    for attr in level.attrs
                    if attr in changes
                }
                if not level_values or level_pk is None:
                    continue
                compiled = Update(level.table).values(level_values).where(level_pk == pk).compile()
                self._raise_bind_error(compiled)
                ordered = list(compiled._ordered)
                for _fk_attr, (col_name, dep) in deferred_cols.items():
                    if col_name in compiled._bind_names:
                        ordered[compiled._bind_names.index(col_name)] = (
                            "__catalyst_rowid__",
                            dep,
                        )
                ops.append((compiled.string, ordered, compiled._bind_types, False))
            cleaned.append(obj)
        # Many-to-many association writes: deduped deletes then inserts,
        # all ahead of object deletes so delete-orphan ordering holds and
        # no association row outlives a member.
        assoc_adds: list[tuple[RelationshipProperty, object, object]] = []
        assoc_dels: list[tuple[RelationshipProperty, object, object]] = []
        seen_pairs: set[tuple[object, frozenset[object]]] = set()
        base_refresh: list[tuple[dict[str, object], str, list[object]]] = []
        persisted_pk: dict[int, object] = {id(obj): pk_value for obj, pk_value, _op in persisted}
        scan: list[tuple[object, int]] = (
            [(obj, _STATUS_PENDING) for obj in new_order]
            + [(obj, _STATUS_PERSISTENT) for obj in uow.dirty_objects()]
            + [(obj, _STATUS_DELETED) for obj in deleted_order]
        )
        for obj, status in scan:
            mapper = _mapper_of(obj)
            for prop in mapper.relationships.values():
                if prop.direction != _MANYTOMANY or prop.viewonly:
                    continue
                assoc = cast(Table, prop.secondary)
                local_fk = cast(Column, prop.secondary_local)
                remote_fk = cast(Column, prop.secondary_remote)
                instance_dict = obj.__dict__
                owner_key: object
                if status == _STATUS_PENDING:
                    pk = persisted_pk.get(id(obj))
                    owner_key = (
                        pk if pk is not None else ("__catalyst_rowid__", insert_index[id(obj)])
                    )
                else:
                    owner_key = _pk_value(uow, obj)
                if owner_key is None:
                    continue
                committed = _m2m_committed(instance_dict, prop.key)
                base: frozenset[object]
                if committed is None:
                    if status == _STATUS_PENDING:
                        # A new object has no committed association rows —
                        # and its owner key may still be a deferred marker.
                        base = frozenset()
                    elif status == _STATUS_PERSISTENT and prop.key not in uow.dirty_attrs_of(obj):
                        continue
                    else:
                        base = self._m2m_base_rows(prop, owner_key)
                else:
                    base = committed
                value = instance_dict.get(prop.key)
                if prop.uselist:
                    members = list(cast("list[object]", value)) if isinstance(value, list) else []
                else:
                    members = [value] if value is not None else []
                member_keys: list[tuple[object, object]] = []
                for item in members:
                    dep = insert_index.get(id(item))
                    if dep is not None:
                        pk = persisted_pk.get(id(item))
                        member_keys.append(
                            (item, pk if pk is not None else ("__catalyst_rowid__", dep))
                        )
                    else:
                        member_keys.append((item, self._related_pk(item)))
                member_key_set = {key for _item, key in member_keys}
                if status == _STATUS_DELETED:
                    remote_dels = set(base) | {
                        key for _item, key in member_keys if not _is_deferred_bind(key)
                    }
                    remote_adds: list[object] = []
                else:
                    remote_dels = {key for key in base if key not in member_key_set}
                    remote_adds = [key for _item, key in member_keys if key not in base]
                for remote in remote_adds:
                    if remote is None:
                        continue
                    pair = (
                        assoc.name,
                        frozenset({(local_fk.name, owner_key), (remote_fk.name, remote)}),
                    )
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        assoc_adds.append((prop, owner_key, remote))
                for remote in remote_dels:
                    pair = (
                        assoc.name,
                        frozenset({(local_fk.name, owner_key), (remote_fk.name, remote)}),
                    )
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        assoc_dels.append((prop, owner_key, remote))
                if status != _STATUS_DELETED:
                    base_refresh.append((instance_dict, prop.key, members))
        for prop, owner_key, remote in assoc_dels:
            assoc = cast(Table, prop.secondary)
            compiled = (
                Delete(assoc)
                .where(
                    cast(Column, prop.secondary_local) == owner_key,
                    cast(Column, prop.secondary_remote) == remote,
                )
                .compile()
            )
            self._raise_bind_error(compiled)
            ops.append((compiled.string, list(compiled._ordered), compiled._bind_types, False))
        insert_compiled: dict[int, Compiled] = {}
        for prop, owner_key, remote in assoc_adds:
            assoc = cast(Table, prop.secondary)
            local_fk = cast(Column, prop.secondary_local)
            remote_fk = cast(Column, prop.secondary_remote)
            compiled = insert_compiled.get(id(prop))
            if compiled is None:
                compiled = (
                    Insert(assoc).values({local_fk.name: None, remote_fk.name: None}).compile()
                )
                self._raise_bind_error(compiled)
                insert_compiled[id(prop)] = compiled
            ordered = list(compiled._ordered)
            ordered[compiled._bind_names.index(local_fk.name)] = owner_key
            ordered[compiled._bind_names.index(remote_fk.name)] = remote
            ops.append((compiled.string, ordered, compiled._bind_types, False))
        for obj in deleted_order:
            mapper = _mapper_of(obj)
            pk = uow.pk_of(obj)
            # Joined-table deletes run leaf -> root so the child row goes
            # before the parent it references (SQLAlchemy's delete order).
            for level in reversed(mapper.table_levels):
                if level.pk_column is None:
                    continue
                compiled = Delete(level.table).where(level.pk_column == pk).compile()
                self._raise_bind_error(compiled)
                ops.append((compiled.string, compiled._ordered, compiled._bind_types, False))
            dropped.append(obj)
        dropped.extend(synced_deleted)
        try:
            assigned = self._conn()._native.execute_flush(ops)
        except Exception:
            # A failed flush puts the session in partial rollback; every
            # operation must raise PendingRollbackError until rollback().
            self._pending_rollback = True
            raise
        final: list[tuple[object, object]] = []
        for obj, pk_value, op_index in persisted:
            mapper = _mapper_of(obj)
            pk = pk_value if pk_value is not None else assigned[op_index]
            if mapper.pk_attr is not None:
                object.__setattr__(obj, mapper.pk_attr, pk)
            final.append((obj, pk))
        # Deferred FK markers reference an op index — resolve each through
        # the op -> object map before reading the assigned rowid.
        persisted_by_op = {op: (obj, pk) for obj, pk, op in persisted}
        for obj, attr, dep in deferred_fk:
            _dep_obj, dep_pk = persisted_by_op[dep]
            obj.__dict__[attr] = dep_pk if dep_pk is not None else assigned[dep]
        uow.flush_transition(final, cleaned, dropped)
        self._synced_deleted.difference_update(id(obj) for obj in synced_deleted)
        # Association diffs succeeded: current membership is the new
        # committed baseline for every scanned relationship.
        for instance_dict, key, members in base_refresh:
            _m2m_committed_set(
                instance_dict,
                key,
                frozenset(
                    pk for pk in (_pk_value(uow, member) for member in members) if pk is not None
                ),
            )

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
        return _pk_value(self._uow, related)

    def _m2m_base_rows(self, prop: RelationshipProperty, owner_pk: object) -> frozenset[object]:
        """Committed association membership: remote PKs joined to ``owner_pk``."""
        assoc = cast(Table, prop.secondary)
        statement = Select((cast(Column, prop.secondary_remote),), assoc).where(
            cast(Column, prop.secondary_local) == owner_pk
        )
        rows = self._conn().execute(statement).fetchall()
        return frozenset(row[0] for row in rows)

    # -- relationship loading -----------------------------------------------

    def _lazy_load(self, instance: object, prop: RelationshipProperty) -> object:
        """Lazy-load one relationship attribute for a persistent instance."""
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
        if prop.direction == _MANYTOMANY:
            if prop.uselist:
                collection = InstrumentedList()
                collection.bind(instance, prop)
                member_pks: set[object] = set()
                if pk is not None:
                    for item in self._m2m_children(prop, pk):
                        list[object].append(collection, item)
                        item_pk = _pk_value(uow, item)
                        if item_pk is not None:
                            member_pks.add(item_pk)
                instance_dict[prop.key] = collection
                _m2m_committed_set(instance_dict, prop.key, frozenset(member_pks))
                uow.attr_loaded(instance, prop.key)
                return collection
            target = None
            member_pk: object = None
            if pk is not None:
                items = self._m2m_children(prop, pk)
                target = items[0] if items else None
                if target is not None:
                    member_pk = _pk_value(uow, target)
            instance_dict[prop.key] = target
            _m2m_committed_set(
                instance_dict,
                prop.key,
                frozenset() if member_pk is None else frozenset({member_pk}),
            )
            uow.attr_loaded(instance, prop.key)
            if target is not None and prop.sibling is not None and not prop.sibling.uselist:
                target.__dict__[prop.sibling.key] = instance
            return target
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

    def _reconcile_loaded(
        self, entity: type[object], target_mapper: Mapper[Any], instances: list[object]
    ) -> list[object]:
        """Reconcile loaded instances, expiring unfetched subclass columns.

        A query against an inheritance parent resolves subclass instances
        through the discriminator without fetching their joined-table
        columns; those attributes lazy-load on first access.
        """
        items = self._uow.reconcile(
            entity,
            cast(str, target_mapper.pk_attr),
            list(target_mapper.attrs),
            instances,
        )
        for instance in items:
            inst_mapper = getattr(type(instance), "__mapper__", None)
            if inst_mapper is None:
                continue
            # Attributes the SELECT never carried (a subclass's joined-table
            # columns) stay absent from ``__dict__``; mark them expired along
            # with anything already expired so the flags survive reconcile.
            missing = [
                attr
                for attr in inst_mapper.attrs
                if attr not in instance.__dict__ or self._uow.attr_expired(instance, attr)
            ]
            if missing:
                self._uow.expire_attrs(instance, missing)
        return items

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
            _entity_select_columns(target_mapper),
            target_mapper.select_from,
            predicate,
            (),
            None,
            None,
            None,
            target_mapper.select_joins,
        )
        result = self._conn().execute(statement)
        select_attrs = list(target_mapper.select_attrs)
        instances = result.native_result.hydrate(
            prop.entity, select_attrs, _hydrate_polymorphic(target_mapper, select_attrs)
        )
        return self._reconcile_loaded(prop.entity, target_mapper, instances)

    def _m2m_children(self, prop: RelationshipProperty, owner_pk: object) -> list[object]:
        """Lazy m2m SELECT: implicit comma join through the association table.

        Renders SQLAlchemy's bind-first shape —
        ``FROM <target>, <assoc> WHERE ? = <assoc>.<local_fk> AND
        <target>.<pk> = <assoc>.<remote_fk>``.
        """
        assoc = cast(Table, prop.secondary)
        local_fk = cast(Column, prop.secondary_local)
        remote_fk = cast(Column, prop.secondary_remote)
        target_mapper = prop.target_mapper
        target_pk = cast(Column, target_mapper.pk_column)
        predicate = Expression(
            (
                "and",
                (
                    (
                        "binary",
                        (
                            "literal",
                            owner_pk,
                            local_fk.name,
                            _column_type_tag(local_fk.type),
                        ),
                        "=",
                        local_fk.serialize(),
                    ),
                    ("binary", target_pk.serialize(), "=", remote_fk.serialize()),
                ),
            )
        )
        statement = Select(
            _entity_select_columns(target_mapper),
            target_mapper.select_from,
            predicate,
            (),
            None,
            None,
            None,
            target_mapper.select_joins + (("implicit_from", assoc.name),),
        )
        result = self._conn().execute(statement)
        select_attrs = list(target_mapper.select_attrs)
        instances = result.native_result.hydrate(
            prop.entity, select_attrs, _hydrate_polymorphic(target_mapper, select_attrs)
        )
        return self._reconcile_loaded(prop.entity, target_mapper, instances)

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
            raw = self._conn().execute(statement)
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
        specs: list[
            tuple[type[object], list[str], tuple[int, dict[object, type[object]]] | None]
        ] = [
            (
                entity,
                list(mapper.select_attrs),
                _hydrate_polymorphic(mapper, list(mapper.select_attrs)),
            )
        ]
        columns: list[Column | Expression] = list(_entity_select_columns(mapper))
        joins: list[tuple[object, ...]] = list(mapper.select_joins)
        joined_props: list[RelationshipProperty] = []
        for index, prop in enumerate(joined):
            target_mapper = prop.target_mapper
            alias = f"{target_mapper.select_from.name}_{index + 1}"
            left = prop.local_col.serialize()
            right = (
                "column",
                alias,
                prop.remote_col.name,
                _column_type_tag(prop.remote_col.type),
            )
            if prop.direction == _MANYTOMANY:
                # ``LEFT OUTER JOIN (assoc AS a_1 JOIN t AS t_1 ON
                # t_1.pk = a_1.remote) ON local.pk = a_1.local``.
                assoc = cast(Table, prop.secondary)
                local_fk = cast(Column, prop.secondary_local)
                remote_fk = cast(Column, prop.secondary_remote)
                assoc_alias = f"{assoc.name}_{index + 1}"
                inner_on = (
                    "binary",
                    (
                        "column",
                        alias,
                        prop.remote_col.name,
                        _column_type_tag(prop.remote_col.type),
                    ),
                    "=",
                    (
                        "column",
                        assoc_alias,
                        remote_fk.name,
                        _column_type_tag(remote_fk.type),
                    ),
                )
                outer_on = (
                    "binary",
                    left,
                    "=",
                    (
                        "column",
                        assoc_alias,
                        local_fk.name,
                        _column_type_tag(local_fk.type),
                    ),
                )
                joins.append(
                    (
                        "left_outer_join_nested",
                        assoc.name,
                        assoc_alias,
                        target_mapper.select_from.name,
                        alias,
                        inner_on,
                        outer_on,
                    )
                )
            else:
                joins.append(
                    (
                        "left_outer_join",
                        target_mapper.select_from.name,
                        alias,
                        ("binary", left, "=", right),
                    )
                )
            # Only the target's root table is joined (aliased); subclass-table
            # attributes are hydrated as missing and lazy-load on access.
            target_level = target_mapper.table_levels[0]
            target_attrs = [
                attr
                for attr, column in target_mapper.attr_columns.items()
                if column.owner is target_level.table
            ]
            for attr in target_attrs:
                column = target_mapper.attr_columns[attr]
                columns.append(
                    Expression(("entity_column", alias, column.name, _column_type_tag(column.type)))
                )
            specs.append(
                (
                    prop.entity,
                    target_attrs,
                    _hydrate_polymorphic(target_mapper, target_attrs),
                )
            )
            joined_props.append(prop)
        joined_stmt = Select(
            tuple(columns),
            mapper.select_from,
            cast("Column | Expression | None", statement.predicate),
            statement.ordering,
            statement.row_limit,
            statement.row_offset,
            entity,
            tuple(joins),
            (),
        )
        result = self._conn().execute(joined_stmt)
        rows = result.native_result.hydrate_entities(specs)
        # Reconcile every hydrated slice through the identity map so related
        # rows yield the same instance session.get() would return.
        canon: list[dict[int, object]] = []
        for spec_index, (cls, _attrs, _poly) in enumerate(specs):
            raw_items = [row[spec_index] for row in rows if row[spec_index] is not None]
            cls_mapper = _mapper_of(cls)
            reconciled = self._reconcile_loaded(cls, cls_mapper, raw_items)
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
        if prop.direction == _MANYTOMANY:
            # ``SELECT assoc.local_fk, t.* FROM assoc JOIN t ON t.pk =
            # assoc.remote_fk WHERE assoc.local_fk IN (...)`` — the
            # association table leads the FROM and its grouping column leads
            # the SELECT list, matching SQLAlchemy's selectin shape.
            assoc = cast(Table, prop.secondary)
            local_fk = cast(Column, prop.secondary_local)
            remote_fk = cast(Column, prop.secondary_remote)
            parents: dict[object, object] = {}
            seen_parents: set[int] = set()
            for (parent,) in entity_rows:
                if id(parent) in seen_parents:
                    continue
                seen_parents.add(id(parent))
                pk = self._uow.pk_of(parent)
                if pk is None and prop.mapper.pk_attr is not None:
                    pk = parent.__dict__.get(prop.mapper.pk_attr)
                if pk is not None:
                    parents[pk] = parent
            if not parents:
                return
            root_level = target_mapper.table_levels[0]
            target_pk = cast(Column, root_level.pk_column or target_mapper.pk_column)
            statement = Select(
                (Expression(local_fk.serialize()), *_entity_select_columns(target_mapper)),
                assoc,
                local_fk.in_(list(parents)),
                (),
                None,
                None,
                None,
                (
                    (
                        "join",
                        target_mapper.select_from.name,
                        None,
                        ("binary", target_pk.serialize(), "=", remote_fk.serialize()),
                    ),
                    *target_mapper.select_joins,
                ),
            )
            result = self._conn().execute(statement)
            # ``hydrate_entities`` slices rows positionally: a throwaway spec
            # consumes the grouping FK column so the real spec hydrates only
            # target columns (one FFI crossing for the whole batch).
            pk_attr = cast(str, target_mapper.pk_attr)
            select_attrs = list(target_mapper.select_attrs)
            rows = result.native_result.hydrate_entities(
                [
                    (prop.entity, [pk_attr], None),
                    (
                        prop.entity,
                        select_attrs,
                        _hydrate_polymorphic(target_mapper, select_attrs),
                    ),
                ]
            )
            owners: list[object] = []
            raw_items: list[object] = []
            for row in rows:
                stub, item = row[0], row[1]
                if item is None:
                    continue
                owners.append(getattr(stub, pk_attr) if stub is not None else None)
                raw_items.append(item)
            items = self._reconcile_loaded(prop.entity, target_mapper, raw_items)
            for owner_key, item in zip(owners, items, strict=True):
                parent = parents.get(owner_key)
                if parent is not None:
                    _stitch_loaded(parent, prop, item)
            # Parents with no association rows still hold committed state —
            # an empty collection — like SQLAlchemy's selectin load.
            for parent in parents.values():
                _stitch_loaded(parent, prop, None)
            return
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
            cols: list[Column | Expression] = [fk_col] + [
                Expression(
                    (
                        "entity_column",
                        column.table_name,
                        column.name,
                        _column_type_tag(column.type),
                    )
                )
                for column in target_mapper.select_columns
                if column is not fk_col
            ]
            attrs = [fk_attr] + [attr for attr in target_mapper.select_attrs if attr != fk_attr]
            statement = Select(
                tuple(cols),
                target_mapper.select_from,
                None,
                (),
                None,
                None,
                None,
                target_mapper.select_joins,
            ).where(fk_col.in_(list(parents)))
            result = self._conn().execute(statement)
            instances = result.native_result.hydrate(
                prop.entity, attrs, _hydrate_polymorphic(target_mapper, attrs)
            )
            instances = self._reconcile_loaded(prop.entity, target_mapper, instances)
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
            _entity_select_columns(target_mapper),
            target_mapper.select_from,
            None,
            (),
            None,
            None,
            None,
            target_mapper.select_joins,
        ).where(prop.remote_col.in_(fks))
        result = self._conn().execute(statement)
        select_attrs = list(target_mapper.select_attrs)
        instances = result.native_result.hydrate(
            prop.entity, select_attrs, _hydrate_polymorphic(target_mapper, select_attrs)
        )
        instances = self._reconcile_loaded(prop.entity, target_mapper, instances)
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
        return self._conn().begin_nested()

    def in_transaction(self) -> bool:
        """Whether the session currently has an open transaction."""
        return self._txn_active

    def commit(self) -> None:
        """Flush pending work then COMMIT the session's transaction."""
        self._check_active()
        try:
            self.flush()
            connection = self._connection
            if connection is not None:
                connection.commit()
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
        self._pending_rollback = False
        if self._connection is not None:
            self._connection.rollback()
        for obj in self._uow.rollback():
            _expire_instance(obj)
        self._synced_deleted.clear()
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

    def get(self, entity: type[_T], ident: object, populate_existing: bool = False) -> _T | None:
        """Return the instance for primary key ``ident``, or ``None``.

        The identity map is consulted before autoflush and the database so
        repeated calls return the identical object without emitting pending
        writes.  ``populate_existing`` always SELECTs and refreshes the
        already-mapped instance in place.
        """
        self._check_active()
        self._mark_active()
        if not populate_existing:
            # SA's identity-map lookup precedes autoflush: an identity hit
            # returns without emitting pending writes.
            cached = self._uow.identity_get(entity, ident)
            if cached is not None:
                return cast(_T, cached)
        if self.autoflush:
            self.flush()
        mapper = getattr(entity, "__mapper__", None)
        if mapper is None or cast("Mapper[Any]", mapper).pk_column is None:
            raise UnmappedInstanceError(
                f"Class '{entity.__module__}.{entity.__qualname__}' is not mapped"
            )
        mapper = cast("Mapper[Any]", mapper)
        # Select only the queried mapper's columns: on a shared STI table a
        # sibling's columns would otherwise shift positional hydration, and a
        # joined-table subclass reads through its parent/child join.  The PK
        # predicate targets the hierarchy root's column like SQLAlchemy.
        pk_column = mapper.base_mapper.pk_column or mapper.pk_column
        statement = Select((), mapper.select_from, entity=entity).where(pk_column == ident)
        compiled = statement.compile()
        self._raise_bind_error(compiled)
        row = self._conn().execute(compiled).fetchone()
        if row is None:
            return None
        # The discriminator resolves the concrete class before identity-map
        # keying (STI: get(Parent, id) can return a subclass instance).
        resolved = self._polymorphic_class(entity, mapper, row)
        resolved_mapper = _mapper_of(resolved)
        cached = self._uow.identity_get(resolved, ident)
        if cached is not None:
            # populate_existing: refresh the mapped instance, keep identity.
            for attr, value in zip(mapper.select_attrs, row, strict=False):
                object.__setattr__(cached, attr, value)
            self._uow.mark_clean(cached)
            self._expire_unloaded(cached, resolved_mapper, mapper)
            for key in resolved_mapper.relationship_attrs:
                cached.__dict__.pop(key, None)
            return cast(_T, cached)
        instance = resolved.__new__(resolved)
        for attr, value in zip(mapper.select_attrs, row, strict=False):
            object.__setattr__(instance, attr, value)
        self._uow.attach_persistent(instance, resolved, list(resolved_mapper.attrs), ident)
        instance._catalyst_state = self._uow  # type: ignore[attr-defined]
        self._expire_unloaded(instance, resolved_mapper, mapper)
        return instance

    @staticmethod
    def _polymorphic_class(
        entity: type[_T], mapper: Mapper[Any], row: Sequence[object]
    ) -> type[_T]:
        """Resolve a mapper-column row's discriminator to the concrete class."""
        attr = mapper.polymorphic_on_attr
        if attr is None or not mapper.polymorphic_map:
            return entity
        # The row layout follows the SELECT spec (``select_attrs``), which
        # differs from ``attrs`` on joined-table mappers.
        index = mapper.select_attrs.index(attr) if attr in mapper.select_attrs else -1
        if index < 0 or index >= len(row):
            return entity
        return cast("type[_T]", mapper.polymorphic_map.get(row[index], entity))

    def _expire_unloaded(
        self, instance: object, resolved: Mapper[Any], queried: Mapper[Any]
    ) -> None:
        """Mark concrete-subclass attributes the parent SELECT didn't fetch.

        A parent query hydrates a subclass instance with only the parent's
        columns; the subclass-only attributes load on first access, like
        SQLAlchemy's deferred subclass-column load.
        """
        if resolved is queried:
            return
        missing = [attr for attr in resolved.attrs if attr not in instance.__dict__]
        if missing:
            self._uow.expire_attrs(instance, missing)

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
        pk_column = mapper.base_mapper.pk_column or mapper.pk_column
        compiled = (
            Select((), mapper.select_from, entity=mapper.class_).where(pk_column == pk).compile()
        )
        self._raise_bind_error(compiled)
        row = self._conn().execute(compiled).fetchone()
        if row is None:
            raise ObjectDeletedError(self._deleted_message(instance))
        for attr, value in zip(mapper.select_attrs, row, strict=True):
            object.__setattr__(instance, attr, value)
        uow.mark_clean(instance)
        for key in mapper.relationship_attrs:
            instance.__dict__.pop(key, None)

    def _load_expired(self, instance: object, attr: str) -> None:
        """Refresh ``instance`` by primary key; called from ``Column.__get__``.

        Like SQLAlchemy's ``_load_expired``, one SELECT repopulates every
        attribute in the expired set at once and clears the whole set —
        sibling attributes stay consistent with the row snapshot rather than
        reloading separately.  Joined-table instances add one twist: when
        only subclass-table attributes are pending (a parent query resolved
        the subclass without fetching its child columns), the load narrows
        to that child table — SQLAlchemy's deferred subclass-column SELECT
        ``SELECT <child cols> FROM <child> WHERE ? = <child>.<pk>``.
        """
        if self._pending_rollback:
            raise PendingRollbackError(
                "This Session's transaction has been rolled back due to a previous "
                "exception during flush. To begin a new transaction with this "
                "Session, first issue Session.rollback()."
            )
        self._ensure_connection()
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
        level = _deferred_level(mapper, attr, uow.expired_attrs_of(instance))
        if level is not None:
            self._load_deferred_level(instance, uow, mapper, level, attr, pk)
            return
        pk_column = mapper.base_mapper.pk_column or mapper.pk_column
        compiled = (
            Select((), mapper.select_from, entity=mapper.class_).where(pk_column == pk).compile()
        )
        self._raise_bind_error(compiled)
        row = self._conn().execute(compiled).fetchone()
        self._mark_active()
        if row is None:
            raise ObjectDeletedError(self._deleted_message(instance))
        for expired in uow.expired_attrs_of(instance):
            object.__setattr__(instance, expired, row[mapper.select_attrs.index(expired)])
        uow.mark_loaded(instance)

    def _load_deferred_level(
        self,
        instance: object,
        uow: NativeUow,
        mapper: Mapper[Any],
        level: _TableLevel,
        attr: str,
        pk: object,
    ) -> None:
        """Deferred subclass-table load for a joined-table instance.

        ``select(Parent)`` resolves subclass instances without fetching the
        child table's columns; the first access loads the whole child-table
        attribute group in one SELECT keyed by the child primary key —
        SQLAlchemy's ``SELECT <child cols> FROM <child> WHERE ? = <pk>``.
        """
        expired = set(uow.expired_attrs_of(instance))
        load_attrs = [name for name in level.attrs if name in expired]
        pk_column = cast("Column | None", level.pk_column)
        if not load_attrs or pk_column is None:
            return
        predicate = Expression(
            (
                "binary",
                (
                    "literal",
                    pk,
                    pk_column.name,
                    _column_type_tag(pk_column.type),
                ),
                "=",
                pk_column.serialize(),
            )
        )
        statement = Select(
            tuple(mapper.attr_columns[name] for name in load_attrs),
            level.table,
            predicate,
        )
        row = self._conn().execute(statement).fetchone()
        self._mark_active()
        if row is None:
            # Orphaned parent row (e.g. after a bulk parent delete): SA's
            # deferred loader reports the missing child row as a KeyError.
            raise KeyError(f"Deferred loader for attribute '{attr}' failed to populate correctly")
        for name, value in zip(load_attrs, row, strict=True):
            object.__setattr__(instance, name, value)
            uow.attr_loaded(instance, name)

    @staticmethod
    def _deleted_message(instance: object) -> str:
        """SQLAlchemy's ``ObjectDeletedError`` message shape."""
        cls = type(instance)
        return (
            f"Instance '<{cls.__name__} at 0x{id(instance):x}>' has been "
            "deleted, or its row is otherwise not present."
        )

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
        if isinstance(statement, (Update, Delete)) and statement.entity is not None:
            return self._execute_orm_dml(statement)
        result = self._conn().execute(statement, parameters)
        entity = getattr(statement, "entity", None)
        if entity is None:
            return result
        return CursorResult(result.native_result, entity=entity, uow=self._uow)

    # -- ORM-enabled DML ----------------------------------------------------

    def _execute_orm_dml(self, statement: Update | Delete) -> CursorResult:
        """``session.execute(update(Entity)/delete(Entity))`` with sync.

        ``synchronize_session`` resolution mirrors SQLAlchemy: ``"auto"``
        (the default) evaluates simple predicates in memory and falls back to
        ``fetch``; ``"evaluate"`` requires an evaluable predicate; ``"fetch"``
        selects affected keys via ``RETURNING``; ``False`` executes without
        touching session state.
        """
        entity = cast("type[object]", statement.entity)
        mapper = _mapper_of(entity)
        # STI subclass bulk DML is restricted to the identity subtree
        # (``type IN (...)``); the base class stays unfiltered.
        criterion = _polymorphic_criterion(entity)
        if criterion is not None:
            statement = statement.where(criterion)
        mode = statement.exec_options.get("synchronize_session", "auto")
        if mode is True:
            mode = "evaluate"
        if mode is False or mode == "false":
            result = self._conn().execute(statement)
            return result
        evaluable = self._predicate_evaluable(getattr(statement, "predicate", None))
        if mode == "auto":
            mode = "evaluate" if evaluable else "fetch"
        elif mode == "evaluate" and not evaluable:
            raise InvalidRequestError(
                "Could not evaluate current criteria in Python. Specify 'fetch' "
                "or False for the synchronize_session execution option."
            )
        if mode == "fetch":
            pk_column = mapper.pk_column
            if pk_column is None:
                raise InvalidRequestError(
                    "Could not evaluate current criteria in Python. Specify 'fetch' "
                    "or False for the synchronize_session execution option."
                )
            compiled = statement.compile()
            self._raise_bind_error(compiled)
            returning = Compiled(
                f"{compiled.string} RETURNING {pk_column.name}",
                compiled.params,
                compiled.statement_kind,
                compiled._ordered,
                compiled._bind_types,
                compiled._bind_names,
                [_column_type_tag(pk_column.type)],
                entity,
            )
            result = self._conn().execute(returning)
            pks = {row[0] for row in result.fetchall()}
            self._sync_orm_dml(
                statement,
                mapper,
                lambda inst: self._uow.pk_of(inst) in pks,
            )
            return result
        result = self._conn().execute(statement)
        self._sync_orm_dml(statement, mapper, self._predicate_matcher(statement, mapper))
        return result

    def _sync_orm_dml(
        self,
        statement: Update | Delete,
        mapper: Mapper[Any],
        matches: Callable[[object], bool],
    ) -> None:
        """Apply an ORM DML statement to matching in-session instances."""
        col_to_attr = {column.name: attr for attr, column in mapper.attr_columns.items()}
        # Base-class DML affects subclass rows too: walk the identity subtree.
        candidates = [
            inst
            for sub_mapper in mapper.self_and_descendants
            for inst in self._uow.persistent_objects(sub_mapper.class_)
        ]
        for inst in candidates:
            if not matches(inst):
                continue
            if isinstance(statement, Delete):
                # The bulk DELETE already ran; matching objects move to the
                # deleted state (``session.deleted``, like SQLAlchemy's
                # synchronize_session) but flush drops them without a
                # follow-up per-row DELETE — joined-table child rows stay
                # orphaned rather than cascade-deleted.
                self._uow.mark_deleted(inst)
                self._synced_deleted.add(id(inst))
                continue
            for col_name, value in statement.values_map.items():
                attr = col_to_attr.get(col_name, col_name)
                if isinstance(value, (Column, Expression)):
                    # Expression values (e.g. col=col+1) can't be computed
                    # locally — expire so the next access reloads them.
                    inst.__dict__.pop(attr, None)
                    expired = list(self._uow.expired_attrs_of(inst))
                    if attr not in expired:
                        self._uow.expire_attrs(inst, expired + [attr])
                else:
                    inst.__dict__[attr] = value

    @staticmethod
    def _predicate_evaluable(predicate: Column | Expression | None) -> bool:
        """Whether the WHERE clause supports in-memory evaluation."""
        if predicate is None:
            return True
        node = predicate.serialize() if isinstance(predicate, Column) else predicate.node
        return _node_evaluable(node)

    def _predicate_matcher(
        self, statement: Update | Delete, mapper: Mapper[Any]
    ) -> Callable[[object], bool]:
        """Compile the WHERE clause into an instance predicate."""
        predicate = getattr(statement, "predicate", None)
        col_to_attr = {column.name: attr for attr, column in mapper.attr_columns.items()}
        if predicate is None:
            return lambda _inst: True
        node = predicate.serialize() if isinstance(predicate, Column) else predicate.node
        return lambda inst: _eval_node(node, inst.__dict__, col_to_attr)

    # -- bulk operations ----------------------------------------------------

    def bulk_insert_mappings(
        self,
        mapper: type[object],
        mappings: Iterable[Mapping[str, object]],
        return_defaults: bool = False,
        render_nulls: bool = False,
    ) -> None:
        """INSERT each mapping in one batch, bypassing the unit of work.

        No autoflush, no identity-map involvement, no PK write-back —
        matching SQLAlchemy's ``Session.bulk_insert_mappings``.
        """
        self._check_active()
        self._mark_active()
        cls_mapper = _require_class_mapper(mapper)
        rows: list[dict[str, object]] = []
        for mapping in mappings:
            row: dict[str, object] = {}
            for attr, column in cls_mapper.attr_columns.items():
                if attr in mapping:
                    value = mapping[attr]
                elif column.name in mapping:
                    value = mapping[column.name]
                elif attr == cls_mapper.pk_attr:
                    continue  # let the database assign the primary key
                else:
                    value = _client_default(cls_mapper, attr)
                    if value is _MISSING:
                        if render_nulls:
                            value = None
                        else:
                            continue
                row[column.name] = value
            rows.append(row)
        if not rows:
            return
        self._conn().execute(Insert(cls_mapper.local_table), rows)

    def bulk_update_mappings(
        self,
        mapper: type[object],
        mappings: Iterable[Mapping[str, object]],
    ) -> None:
        """One single-row UPDATE per mapping; ``StaleDataError`` on misses."""
        self._check_active()
        self._mark_active()
        cls_mapper = _require_class_mapper(mapper)
        pk_col = cls_mapper.pk_column
        pk_attr = cls_mapper.pk_attr
        for mapping in mappings:
            if (
                pk_col is None
                or pk_attr is None
                or (pk_attr not in mapping and pk_col.name not in mapping)
            ):
                raise InvalidRequestError(
                    f"No primary key value supplied for column(s) "
                    f"{cls_mapper.local_table.name}.{pk_col.name if pk_col else '?'}; "
                    "per-row ORM Bulk UPDATE by Primary Key requires that "
                    "records contain primary key values"
                )
            pk = mapping.get(pk_attr, mapping.get(pk_col.name))
            values: dict[str, object] = {}
            for key, value in mapping.items():
                attr = key if key in cls_mapper.attr_columns else _attr_for_name(cls_mapper, key)
                if attr is None or attr == pk_attr:
                    continue
                values[cls_mapper.attr_columns[attr].name] = value
            compiled = Update(cls_mapper.local_table).values(values).where(pk_col == pk).compile()
            self._raise_bind_error(compiled)
            result = self._conn().execute(compiled)
            if result.rowcount == 0:
                raise StaleDataError(
                    f"UPDATE statement on table '{cls_mapper.local_table.name}' "
                    f"expected to update 1 row(s); 0 were matched."
                )

    def bulk_save_objects(
        self,
        objects: Iterable[object],
        return_defaults: bool = False,
        update_changed_only: bool = True,
        preserve_order: bool = True,
    ) -> None:
        """INSERT/UPDATE objects without involving the unit of work.

        Objects are not added to the session, generated primary keys are not
        written back, and already-persistent dirty objects emit their UPDATE
        yet stay dirty (SQLAlchemy's legacy ``bulk_save_objects`` contract).
        """
        self._check_active()
        self._mark_active()
        for obj in objects:
            mapper = _require_mapper(obj)
            uow = _state_of(obj)
            if (
                uow is self._uow
                and uow is not None
                and uow.is_managed(obj)
                and uow.status(obj) == _STATUS_PERSISTENT
            ):
                attrs = list(uow.dirty_attrs_of(obj)) if update_changed_only else list(mapper.attrs)
                values = {
                    mapper.attr_columns[attr].name: obj.__dict__.get(attr)
                    for attr in attrs
                    if attr in mapper.attr_columns
                }
                if not values:
                    continue
                compiled = (
                    Update(mapper.local_table)
                    .values(values)
                    .where(mapper.pk_column == uow.pk_of(obj))
                    .compile()
                )
                self._raise_bind_error(compiled)
                self._conn().execute(compiled)
            else:
                values = {
                    mapper.attr_columns[attr].name: obj.__dict__.get(attr)
                    for attr in mapper.attrs
                    if attr != mapper.pk_attr or obj.__dict__.get(attr) is not None
                }
                compiled = Insert(mapper.local_table).values(values).compile()
                self._raise_bind_error(compiled)
                self._conn().execute(compiled)

    def scalars(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = None,
    ) -> ScalarResult:
        """Execute a statement and return first-column values as a scalar result."""
        return ScalarResult(self.execute(statement, parameters))

    def close(self) -> None:
        """Roll back uncommitted work, detach everything, and release resources.

        Mirrors SQLAlchemy: closing detaches objects rather than expiring
        them (loaded values stay readable on the instance), and the session
        remains usable — engine-bound sessions lazily re-acquire a connection
        on the next operation, while a ``Session`` built on a borrowed
        ``Connection`` keeps using that same connection.
        """
        self._pending_rollback = False
        connection = self._connection
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                pass
            if self._owns_connection:
                connection.close()
                self._connection = None
        for info in self._uow.detach_all():
            obj, pk, status, dirty, expired = info
            self._stamp_detached(obj, (pk, status, dirty, expired))
        self._synced_deleted.clear()
        self._txn_active = False

    def __enter__(self) -> Session:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


# -- ORM DML predicate evaluation --------------------------------------------
#
# ``synchronize_session="auto"/"evaluate"`` mirrors SQLAlchemy's Python-side
# evaluation of simple WHERE clauses: comparisons/IN/IS composed with AND/OR
# on column-versus-literal operands evaluate in memory; anything else falls
# back to the "fetch" (RETURNING) strategy.

_EVAL_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})


def _leaf(node: object) -> bool:
    return isinstance(node, tuple) and node[0] in ("column", "literal")


def _node_evaluable(node: object) -> bool:
    """Whether a WHERE expression node supports in-memory evaluation."""
    if not isinstance(node, tuple) or not node:
        return False
    children = cast("tuple[object, ...]", node)
    tag = children[0]
    if tag == "binary":
        return (
            len(children) == 4
            and children[2] in _EVAL_OPS
            and _leaf(children[1])
            and _leaf(children[3])
        )
    if tag in ("and", "or"):
        return all(_node_evaluable(child) for child in cast("Iterable[object]", children[1]))
    if tag in ("in", "not_in"):
        return _leaf(children[1]) and all(
            _leaf(item) for item in cast("Iterable[object]", children[2])
        )
    if tag in ("between", "not_between"):
        return len(children) == 4 and all(_leaf(item) for item in children[1:4])
    if tag in ("is", "is_not"):
        return _leaf(children[1])
    if tag in ("is_scalar", "is_not_scalar"):
        return _leaf(children[1]) and _leaf(children[2])
    if tag == "not":
        return _node_evaluable(children[1])
    if tag in ("boolean_word", "column"):
        return True
    return False


def _operand_value(
    node: tuple[object, ...],
    instance_dict: dict[str, object],
    col_to_attr: dict[str, str],
) -> object:
    """Resolve a column/literal operand against an instance's ``__dict__``."""
    if node[0] == "column":
        return instance_dict.get(col_to_attr.get(cast(str, node[2]), cast(str, node[2])))
    return node[1]  # literal


def _eval_node(
    node: object,
    instance_dict: dict[str, object],
    col_to_attr: dict[str, str],
) -> bool:
    """Evaluate an evaluable WHERE node against an instance."""

    def operand(child: object) -> object:
        return _operand_value(cast("tuple[object, ...]", child), instance_dict, col_to_attr)

    if not isinstance(node, tuple) or not node:
        return False
    children = cast("tuple[object, ...]", node)
    tag = children[0]
    if tag == "binary":
        left = operand(children[1])
        right = operand(children[3])
        op = children[2]
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        if left is None or right is None:
            return False
        if op == "<":
            return cast(Any, left) < cast(Any, right)
        if op == "<=":
            return cast(Any, left) <= cast(Any, right)
        if op == ">":
            return cast(Any, left) > cast(Any, right)
        if op == ">=":
            return cast(Any, left) >= cast(Any, right)
        return False
    if tag == "and":
        return all(
            _eval_node(child, instance_dict, col_to_attr)
            for child in cast("Iterable[object]", children[1])
        )
    if tag == "or":
        return any(
            _eval_node(child, instance_dict, col_to_attr)
            for child in cast("Iterable[object]", children[1])
        )
    if tag in ("in", "not_in"):
        value = operand(children[1])
        values = [operand(item) for item in cast("Iterable[object]", children[2])]
        result = value in values
        return not result if tag == "not_in" else result
    if tag in ("between", "not_between"):
        value = operand(children[1])
        lo = operand(children[2])
        hi = operand(children[3])
        result = (
            lo is not None
            and hi is not None
            and value is not None
            and cast(Any, lo) <= value <= cast(Any, hi)
        )
        return not result if tag == "not_between" else result
    if tag == "is":
        return operand(children[1]) is None
    if tag == "is_not":
        return operand(children[1]) is not None
    if tag == "is_scalar":
        return operand(children[1]) == operand(children[2])
    if tag == "is_not_scalar":
        return operand(children[1]) != operand(children[2])
    if tag == "not":
        return not _eval_node(children[1], instance_dict, col_to_attr)
    if tag == "boolean_word":
        return bool(children[1])
    if tag == "column":
        return bool(_operand_value(children, instance_dict, col_to_attr))
    return False


def _client_default(mapper: Mapper[Any], attr: str) -> object:
    """The mapped client-side default for ``attr``, or ``_MISSING``."""
    spec = mapper.specs.get(attr)
    if isinstance(spec, MappedColumn):
        if spec.default_factory is not None:
            return spec.default_factory()
        if spec.default is not None:
            return spec.default
    default = mapper.attr_columns[attr].default
    if default is None:
        return _MISSING
    return default() if callable(default) else default


def _attr_for_name(mapper: Mapper[Any], name: str) -> str | None:
    """Map a column *name* back to its attribute (bulk dict keys)."""
    for attr, column in mapper.attr_columns.items():
        if column.name == name:
            return attr
    return None
