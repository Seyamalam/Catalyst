"""Type information for Catalyst's declarative ORM mapping surface."""

import builtins
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any, ClassVar, Generic, SupportsIndex, TypeVar, overload

from typing_extensions import dataclass_transform

from .engine import (
    Connection,
    CursorResult,
    Engine,
    SavepointTransaction,
    ScalarResult,
    TextClause,
)
from .expression import (
    Column,
    Compiled,
    Delete,
    Expression,
    ForeignKey,
    Insert,
    MetaData,
    Select,
    Table,
    Update,
)

_T = TypeVar("_T")

class Mapped(Generic[_T]):
    """Annotation marker declaring a mapped attribute's Python type.

    Type checkers model the mapped class attribute as a descriptor: instance
    access yields ``_T`` and class access yields the bound ``Column``.
    """

    @overload
    def __get__(self, instance: None, owner: object) -> Column: ...
    @overload
    def __get__(self, instance: object, owner: object) -> _T: ...
    def __set__(self, instance: object, value: _T) -> None: ...
    def __delete__(self, instance: object) -> None: ...

class MappedColumn(Mapped[_T]):
    __match_args__: tuple[str, ...]
    type_: object
    primary_key: bool
    nullable: bool | None
    unique: bool
    default: object
    default_factory: Callable[[], object] | None
    server_default: str | None
    name: str | None
    init: bool
    kw_only: bool
    foreign_key: ForeignKey | None
    def __init__(
        self,
        type_: object = ...,
        primary_key: bool = ...,
        nullable: bool | None = ...,
        unique: bool = ...,
        default: object = ...,
        default_factory: Callable[[], object] | None = ...,
        server_default: str | None = ...,
        name: str | None = ...,
        init: bool = ...,
        kw_only: bool = ...,
        foreign_key: ForeignKey | None = ...,
    ) -> None: ...

def mapped_column(
    name_or_type: object = ...,
    type_: object = ...,
    *args: object,
    primary_key: bool = ...,
    nullable: bool | None = ...,
    unique: bool = ...,
    default: _T = ...,
    default_factory: Callable[[], _T] | None = ...,
    server_default: str | None = ...,
    name: str | None = ...,
    init: bool = ...,
    kw_only: bool = ...,
) -> MappedColumn[Any]: ...

class Mapper(Generic[_T]):
    class_: type[_T]
    local_table: Table
    attrs: tuple[str, ...]
    columns: tuple[Column, ...]
    specs: dict[str, MappedColumn[Any] | None]
    attr_columns: dict[str, Column]
    pk_column: Column | None
    pk_attr: str | None
    relationships: dict[str, RelationshipProperty]
    pending_relationships: list[Relationship[Any]]
    relationship_attrs: tuple[str, ...]
    class_registry: dict[str, type[object]]
    def __init__(
        self,
        class_: type[_T],
        table: Table,
        attrs: tuple[str, ...],
        specs: dict[str, MappedColumn[Any] | None],
    ) -> None: ...

class RelationshipProperty:
    """A configured relationship: resolved target, direction, and join columns."""

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
    secondary: Table | None
    secondary_local: Column | None
    secondary_remote: Column | None
    single_parent: bool
    viewonly: bool

class Relationship(Generic[_T]):
    """Declarative relationship marker installed as a class attribute.

    Runtime class access yields :class:`InstrumentedAttribute` and instance
    access returns the stored/lazily-loaded value.
    """

    argument: object
    back_populates: str | None
    backref: str | None
    lazy: str
    uselist: bool | None
    cascade: object
    key: str | None
    declaring_cls: type[object] | None
    container: str | None
    target_hint: object
    configured: RelationshipProperty | None
    secondary: Table | str | None
    primaryjoin: Column | Expression | None
    secondaryjoin: Column | Expression | None
    single_parent: bool
    viewonly: bool
    def __init__(
        self,
        argument: object = ...,
        *,
        back_populates: str | None = ...,
        backref: str | None = ...,
        lazy: str = ...,
        uselist: bool | None = ...,
        cascade: object = ...,
        secondary: Table | str | None = ...,
        primaryjoin: Column | Expression | None = ...,
        secondaryjoin: Column | Expression | None = ...,
        single_parent: bool = ...,
        viewonly: bool = ...,
    ) -> None: ...
    def __get__(self, instance: object, owner: object = ...) -> object: ...
    def __set__(self, instance: object, value: object) -> None: ...
    def __delete__(self, instance: object) -> None: ...
    def configure(self) -> RelationshipProperty: ...
    @builtins.property
    def property(self) -> RelationshipProperty: ...

def relationship(
    argument: object = ...,
    *,
    back_populates: str | None = ...,
    backref: str | None = ...,
    lazy: str = ...,
    uselist: bool | None = ...,
    cascade: object = ...,
    secondary: Table | str | None = ...,
    primaryjoin: Column | Expression | None = ...,
    secondaryjoin: Column | Expression | None = ...,
    single_parent: bool = ...,
    viewonly: bool = ...,
    **kw: object,
) -> Relationship[Any]: ...

class InstrumentedAttribute:
    """Class-level view of a mapped relationship attribute."""

    def __init__(self, owner: type[object] | None, decl: Relationship[Any]) -> None: ...
    @builtins.property
    def key(self) -> str | None: ...
    @builtins.property
    def property(self) -> RelationshipProperty: ...

class InstrumentedList(list[object]):
    """List subclass tracking membership for back-population."""

    owner: object | None
    def __init__(self, iterable: Iterable[object] = ...) -> None: ...
    def bind(self, instance: object, prop: RelationshipProperty) -> InstrumentedList: ...
    def append(self, item: object) -> None: ...
    def extend(self, items: Iterable[object]) -> None: ...
    def insert(self, index: SupportsIndex, item: object) -> None: ...
    def remove(self, item: object) -> None: ...
    def pop(self, index: SupportsIndex = ...) -> object: ...
    def clear(self) -> None: ...

def joinedload(attr: object) -> object: ...
def selectinload(attr: object) -> object: ...
def configure_mappers() -> None: ...

@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(MappedColumn, mapped_column, relationship),
)
class DeclarativeBase:
    metadata: ClassVar[MetaData]
    __table__: ClassVar[Table]
    __mapper__: ClassVar[Mapper[Any]]
    __dataclass_transform__: ClassVar[dict[str, object]]
    def __init__(self, **kwargs: Any) -> None: ...
    def __setattr__(self, name: str, value: Any) -> None: ...
    def __init_subclass__(cls, **kwargs: Any) -> None: ...

def declarative_base() -> type[DeclarativeBase]: ...

class Session:
    autoflush: bool
    expire_on_commit: bool
    def __init__(
        self,
        bind: Engine | Connection,
        autoflush: bool = ...,
        expire_on_commit: bool = ...,
    ) -> None: ...
    @property
    def new(self) -> AbstractSet[object]: ...
    @property
    def dirty(self) -> AbstractSet[object]: ...
    @property
    def deleted(self) -> AbstractSet[object]: ...
    def is_modified(self, instance: object) -> bool: ...
    def add(self, instance: object) -> None: ...
    def add_all(self, instances: Iterable[object]) -> None: ...
    def delete(self, instance: object) -> None: ...
    def flush(self) -> None: ...
    def begin(self) -> None: ...
    def begin_nested(self) -> SavepointTransaction: ...
    def in_transaction(self) -> bool: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def expire(self, instance: object) -> None: ...
    def expire_all(self) -> None: ...
    def expunge(self, instance: object) -> None: ...
    def expunge_all(self) -> None: ...
    def merge(self, instance: _T, load: bool = ...) -> _T: ...
    def bulk_insert_mappings(
        self,
        mapper: type[object],
        mappings: Iterable[Mapping[str, object]],
        return_defaults: bool = ...,
        render_nulls: bool = ...,
    ) -> None: ...
    def bulk_update_mappings(
        self,
        mapper: type[object],
        mappings: Iterable[Mapping[str, object]],
    ) -> None: ...
    def bulk_save_objects(
        self,
        objects: Iterable[object],
        return_defaults: bool = ...,
        update_changed_only: bool = ...,
        preserve_order: bool = ...,
    ) -> None: ...
    def get(self, entity: type[_T], ident: object, populate_existing: bool = ...) -> _T | None: ...
    def refresh(self, instance: object) -> None: ...
    def execute(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = ...,
    ) -> CursorResult: ...
    def scalars(
        self,
        statement: str | TextClause | Compiled | Select | Insert | Update | Delete,
        parameters: Sequence[object] | Mapping[str, object] | None = ...,
    ) -> ScalarResult: ...
    def close(self) -> None: ...
    def __enter__(self) -> Session: ...
    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...
