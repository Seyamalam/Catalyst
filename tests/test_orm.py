"""Declarative ORM mapping and batched hydration parity against SQLAlchemy 2.0."""

from __future__ import annotations

import datetime
from typing import Any

import pytest
from catalyst import (
    ArgumentError,
    Column,
    DeclarativeBase,
    Mapped,
    MultipleResultsFound,
    NoResultFound,
    Session,
    Table,
    create_engine,
    declarative_base,
    delete,
    insert,
    mapped_column,
    select,
    update,
)
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlalchemy.orm import DeclarativeBase as SADeclarativeBase
from sqlalchemy.orm import Mapped as SAMapped
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import mapped_column as sa_mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "orm_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    nick: Mapped[str | None]
    active: Mapped[bool]
    score: Mapped[float]
    created: Mapped[datetime.datetime]
    day: Mapped[datetime.date]
    payload: Mapped[bytes]


class SABase(SADeclarativeBase):
    pass


class SAUser(SABase):
    __tablename__ = "orm_users"
    id: SAMapped[int] = sa_mapped_column(primary_key=True)
    name: SAMapped[str]
    nick: SAMapped[str | None]
    active: SAMapped[bool]
    score: SAMapped[float]
    created: SAMapped[datetime.datetime]
    day: SAMapped[datetime.date]
    payload: SAMapped[bytes]


_ROWS: list[dict[str, Any]] = [
    {
        "id": 1,
        "name": "ada",
        "nick": None,
        "active": True,
        "score": 9.5,
        "created": datetime.datetime(2024, 1, 1, 12, 30, 45),
        "day": datetime.date(2024, 1, 1),
        "payload": b"\x00\x01",
    },
    {
        "id": 2,
        "name": "grace",
        "nick": "g",
        "active": False,
        "score": 7.25,
        "created": datetime.datetime(2024, 2, 3, 4, 5, 6),
        "day": datetime.date(2024, 2, 3),
        "payload": b"blob",
    },
]


def _attr_snapshot(user: Any) -> tuple[Any, ...]:
    return (
        user.id,
        user.name,
        user.nick,
        user.active,
        user.score,
        user.created,
        user.day,
        user.payload,
    )


def _catalyst_engine() -> Any:
    engine = create_engine(":memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(User.__table__), _ROWS)
    return engine


def _sa_engine() -> Any:
    engine = sa_create_engine("sqlite://")
    SABase.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(sa_insert(SAUser.__table__), _ROWS)
    return engine


def test_declarative_mapping_binds_columns_to_table() -> None:
    table = User.__table__
    assert isinstance(table, Table)
    assert table.name == "orm_users"
    assert table.metadata is Base.metadata
    assert [column.name for column in table.columns] == [
        "id",
        "name",
        "nick",
        "active",
        "score",
        "created",
        "day",
        "payload",
    ]
    assert isinstance(User.id, Column)
    assert User.id.table_name == "orm_users"
    assert User.__mapper__.class_ is User
    assert User.__mapper__.attrs == tuple(column.name for column in table.columns)


def test_declarative_nullable_inference_matches_sqlalchemy() -> None:
    catalyst = {column.name: column.nullable for column in User.__table__.columns}
    expected = {column.name: column.nullable for column in SAUser.__table__.columns}
    assert catalyst == expected


def test_declarative_ddl_matches_sqlalchemy() -> None:
    from sqlalchemy.schema import CreateTable

    catalyst_sql = str(User.__table__.compile_ddl())
    sa_dialect = sa_create_engine("sqlite://").dialect
    sa_sql = str(CreateTable(SAUser.__table__).compile(dialect=sa_dialect))
    # Both render the same CREATE TABLE modulo whitespace conventions.
    assert " ".join(catalyst_sql.split()) == " ".join(sa_sql.split())


def test_declarative_base_factory_maps_into_fresh_metadata() -> None:
    OtherBase = declarative_base()

    class Item(OtherBase):  # noqa: F811
        __tablename__ = "items"
        id: Mapped[int] = mapped_column(primary_key=True)

    assert Item.__table__.metadata is OtherBase.metadata
    assert OtherBase.metadata is not Base.metadata
    assert "items" in OtherBase.metadata.tables
    assert "items" not in Base.metadata.tables


def test_select_entity_sql_matches_table_select_and_sqlalchemy() -> None:
    assert str(select(User).compile()) == str(select(User.__table__).compile())
    sa_sql = str(
        sa_select(SAUser).compile(
            dialect=sa_create_engine("sqlite://").dialect,
            compile_kwargs={"literal_binds": True},
        )
    )
    assert " ".join(str(select(User).compile()).split()) == " ".join(sa_sql.split())


def test_class_attributes_form_expressions() -> None:
    statement = select(User).where(User.name == "ada")
    compiled = statement.compile()
    assert "users.name = " in compiled.string.replace("orm_users", "users")
    assert compiled.params == {"name_1": "ada"}


def test_core_execute_returns_raw_tuples_for_entity_select() -> None:
    engine = _catalyst_engine()
    try:
        with engine.connect() as connection:
            result = connection.execute(select(User))
            assert result.keys == tuple(column.name for column in User.__table__.columns)
            rows = result.all()
        assert rows == [tuple(row.values()) for row in _ROWS]
        # Core scalars() yields first-column values like SQLAlchemy.
        with engine.connect() as connection:
            assert connection.execute(select(User)).scalars().all() == [1, 2]
    finally:
        engine.dispose()


def test_session_scalars_hydrate_instances_matching_sqlalchemy() -> None:
    engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(engine) as session:
            users = session.scalars(select(User)).all()
        with SASession(sa_engine) as sa_session:
            sa_users = sa_session.scalars(sa_select(SAUser)).all()
        assert [type(user) for user in users] == [User, User]
        assert [_attr_snapshot(user) for user in users] == [
            _attr_snapshot(user) for user in sa_users
        ]
    finally:
        engine.dispose()
        sa_engine.dispose()


def test_session_execute_returns_entity_rows_and_keys() -> None:
    engine = _catalyst_engine()
    sa_engine = _sa_engine()
    try:
        with Session(engine) as session:
            result = session.execute(select(User))
            assert result.keys == ("User",)
            rows = result.all()
        with SASession(sa_engine) as sa_session:
            sa_rows = sa_session.execute(sa_select(SAUser)).all()
        assert len(rows) == 2
        assert all(len(row) == 1 and isinstance(row[0], User) for row in rows)
        assert [_attr_snapshot(row[0]) for row in rows] == [
            _attr_snapshot(row[0]) for row in sa_rows
        ]
    finally:
        engine.dispose()
        sa_engine.dispose()


def test_entity_survives_where_order_limit_chaining() -> None:
    engine = _catalyst_engine()
    try:
        statement = (
            select(User)
            .where(User.active == False)  # noqa: E712
            .order_by(User.id.desc())
            .limit(1)
        )
        with Session(engine) as session:
            users = session.scalars(statement).all()
        assert len(users) == 1
        assert _attr_snapshot(users[0])[0] == 2
    finally:
        engine.dispose()


def test_compiled_entity_select_hydrates_in_session() -> None:
    engine = _catalyst_engine()
    try:
        compiled = select(User).compile()
        with Session(engine) as session:
            users = session.scalars(compiled).all()
        assert [user.id for user in users] == [1, 2]
    finally:
        engine.dispose()


def test_scalar_result_one_first_one_or_none() -> None:
    engine = _catalyst_engine()
    try:
        with Session(engine) as session:
            user = session.scalars(select(User).where(User.id == 1)).one()
            assert _attr_snapshot(user)[1] == "ada"
            assert session.scalars(select(User).where(User.id == 99)).one_or_none() is None
            assert session.scalars(select(User)).first().id == 1  # type: ignore[union-attr]
            with pytest.raises(NoResultFound):
                session.scalars(select(User).where(User.id == 99)).one()
            with pytest.raises(MultipleResultsFound):
                session.scalars(select(User)).one()
            with pytest.raises(MultipleResultsFound):
                session.scalars(select(User)).one_or_none()
    finally:
        engine.dispose()


def test_mapped_init_matches_sqlalchemy_instance_semantics() -> None:
    user = User(id=5, name="hopper")
    assert user.id == 5
    assert user.name == "hopper"
    assert user.nick is None
    sa_user = SAUser(id=5, name="hopper")
    assert sa_user.nick is None
    with pytest.raises(TypeError, match="invalid keyword argument"):
        User(bogus=1)
    with pytest.raises(TypeError, match="invalid keyword argument"):
        SAUser(bogus=1)


def test_mapped_column_custom_name_and_defaults() -> None:
    class Renamed(Base):
        __tablename__ = "renamed"
        ident: Mapped[int] = mapped_column("user_id", primary_key=True)
        tag: Mapped[str] = mapped_column(default="fresh")

    assert [c.name for c in Renamed.__table__.columns] == ["user_id", "tag"]
    assert Renamed.__mapper__.attrs == ("ident", "tag")
    assert Renamed().tag == "fresh"

    engine = create_engine(":memory:")
    try:
        Renamed.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(insert(Renamed.__table__).values(user_id=1, tag="x"))
        with Session(engine) as session:
            renamed = session.scalars(select(Renamed)).one()
        assert renamed.ident == 1
        assert renamed.tag == "x"
    finally:
        engine.dispose()


def test_declarative_mapping_rejects_non_mapped_annotation() -> None:
    with pytest.raises(ArgumentError):

        class Bad(Base):  # noqa: F811
            __tablename__ = "bad"
            id: int = mapped_column(primary_key=True)


def test_declarative_requires_table_or_annotations() -> None:
    with pytest.raises(ArgumentError):

        class Empty(Base):  # noqa: F811
            __tablename__ = "empty"


def test_insert_update_delete_accept_mapped_class() -> None:
    engine = _catalyst_engine()
    try:
        with engine.begin() as connection:
            connection.execute(update(User).where(User.id == 2).values(nick="g2"))
        with engine.connect() as connection:
            rows = connection.execute(select(User.__table__).where(User.id == 2)).all()
        assert rows[0][2] == "g2"
        with engine.begin() as connection:
            connection.execute(delete(User).where(User.id == 2))
            connection.execute(
                insert(User).values(
                    id=3,
                    name="katherine",
                    nick=None,
                    active=True,
                    score=10.0,
                    created=datetime.datetime(2024, 3, 1),
                    day=datetime.date(2024, 3, 1),
                    payload=b"x",
                )
            )
        with engine.connect() as connection:
            assert connection.execute(select(User.__table__.c.id)).fetchall() == [
                (1,),
                (3,),
            ]
    finally:
        engine.dispose()


def test_session_execute_plain_statements_unaffected() -> None:
    engine = _catalyst_engine()
    try:
        with Session(engine) as session:
            rows = session.execute(
                select(User.__table__.c.name).order_by(User.__table__.c.id)
            ).all()
        assert rows == [("ada",), ("grace",)]
    finally:
        engine.dispose()
