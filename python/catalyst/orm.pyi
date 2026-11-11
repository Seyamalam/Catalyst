"""Type information for Catalyst's declarative ORM mapping surface."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Any, ClassVar, Generic, TypeVar, overload

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
    ) -> None: ...

def mapped_column(
    name_or_type: object = ...,
    type_: object = ...,
    *,
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
    def __init__(
        self,
        class_: type[_T],
        table: Table,
        attrs: tuple[str, ...],
        specs: dict[str, MappedColumn[Any] | None],
    ) -> None: ...

@dataclass_transform(
    kw_only_default=True,
    field_specifiers=(MappedColumn, mapped_column),
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
    def get(self, entity: type[_T], ident: object) -> _T | None: ...
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
