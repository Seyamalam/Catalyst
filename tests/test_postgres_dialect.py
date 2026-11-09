"""PostgreSQL dialect compilation parity against SQLAlchemy's PG dialect.

Catalyst's native PostgreSQL driver uses numeric ``$n`` placeholders while
SQLAlchemy's default psycopg dialect renders ``%(name)s``; comparisons
normalize SQLAlchemy placeholders to ``$n`` by first appearance and compare
bind values as dictionaries.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import catalyst
import pytest
import sqlalchemy as sa
from catalyst import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    desc,
    func,
    insert,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import postgresql

PG = postgresql.dialect()


def _normalize(sql: str) -> str:
    indexes: dict[str, int] = {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in indexes:
            indexes[name] = len(indexes) + 1
        return f"${indexes[name]}"

    return re.sub(r"%\((\w+)\)s", replace, sql)


def _table() -> tuple[Any, Any]:
    sa_meta = sa.MetaData()
    sa_table = sa.Table(
        "items",
        sa_meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(30)),
        sa.Column("body", sa.Text),
        sa.Column("flag", sa.Boolean),
        sa.Column("score", sa.Float),
        sa.Column("created", sa.DateTime),
        sa.Column("day", sa.Date),
        sa.Column("payload", sa.LargeBinary),
    )
    meta = MetaData()
    table = Table(
        "items",
        meta,
        Column("id", Integer, primary_key=True),
        Column("name", String(30)),
        Column("body", Text),
        Column("flag", Boolean),
        Column("score", Float),
        Column("created", DateTime),
        Column("day", Date),
        Column("payload", LargeBinary),
    )
    return table, sa_table


CASES: list[tuple[str, Callable[[Any], Any], Callable[[Any], Any]]] = [
    (
        "select-where-int",
        lambda t: select(t.c.id).where(t.c.id == 5),
        lambda t: sa.select(t.c.id).where(t.c.id == 5),
    ),
    (
        "select-where-string",
        lambda t: select(t.c.name).where(t.c.name == "x"),
        lambda t: sa.select(t.c.name).where(t.c.name == "x"),
    ),
    (
        "boolean-equality",
        lambda t: select(t.c.id).where(t.c.flag == True),  # noqa: E712
        lambda t: sa.select(t.c.id).where(t.c.flag == True),  # noqa: E712
    ),
    (
        "bare-true",
        lambda t: select(t.c.id).where(True),
        lambda t: sa.select(t.c.id).where(sa.true()),
    ),
    (
        "bare-false",
        lambda t: select(t.c.id).where(False),
        lambda t: sa.select(t.c.id).where(sa.false()),
    ),
    (
        "and-or-group",
        lambda t: select(t.c.id).where(and_(t.c.id == 1, or_(t.c.id == 2, t.c.id == 3))),
        lambda t: sa.select(t.c.id).where(sa.and_(t.c.id == 1, sa.or_(t.c.id == 2, t.c.id == 3))),
    ),
    (
        "not-group",
        lambda t: select(t.c.id).where(not_(and_(t.c.id == 1, t.c.id == 2))),
        lambda t: sa.select(t.c.id).where(sa.not_(sa.and_(t.c.id == 1, t.c.id == 2))),
    ),
    (
        "in-values",
        lambda t: select(t.c.id).where(t.c.id.in_([1, 2, 3])),
        lambda t: sa.select(t.c.id).where(t.c.id.in_([1, 2, 3])),
    ),
    (
        "not-in-values",
        lambda t: select(t.c.id).where(not_(t.c.id.in_([1, 2]))),
        lambda t: sa.select(t.c.id).where(t.c.id.not_in([1, 2])),
    ),
    (
        "between",
        lambda t: select(t.c.id).where(t.c.id.between(1, 9)),
        lambda t: sa.select(t.c.id).where(t.c.id.between(1, 9)),
    ),
    (
        "is-null",
        lambda t: select(t.c.id).where(t.c.name.is_(None)),
        lambda t: sa.select(t.c.id).where(t.c.name.is_(None)),
    ),
    (
        "is-not-null",
        lambda t: select(t.c.id).where(t.c.name.is_not(None)),
        lambda t: sa.select(t.c.id).where(t.c.name.is_not(None)),
    ),
    (
        "like-escape",
        lambda t: select(t.c.id).where(t.c.name.like("%a%", escape="/")),
        lambda t: sa.select(t.c.id).where(t.c.name.like("%a%", escape="/")),
    ),
    (
        "int-division",
        lambda t: select(t.c.id / t.c.id),
        lambda t: sa.select(t.c.id / t.c.id),
    ),
    (
        "float-division",
        lambda t: select(t.c.score / t.c.score),
        lambda t: sa.select(t.c.score / t.c.score),
    ),
    (
        "arithmetic-precedence",
        lambda t: select((t.c.id + 1) * 2),
        lambda t: sa.select((t.c.id + 1) * 2),
    ),
    (
        "string-concat",
        lambda t: select(t.c.name + t.c.name),
        lambda t: sa.select(t.c.name + t.c.name),
    ),
    (
        "negation",
        lambda t: select(-t.c.id),
        lambda t: sa.select(-t.c.id),
    ),
    (
        "order-limit-offset",
        lambda t: select(t.c.id).order_by(desc(t.c.id)).limit(10).offset(2),
        lambda t: sa.select(t.c.id).order_by(t.c.id.desc()).limit(10).offset(2),
    ),
    (
        "limit-only",
        lambda t: select(t.c.id).limit(10),
        lambda t: sa.select(t.c.id).limit(10),
    ),
    (
        "offset-only",
        lambda t: select(t.c.id).offset(2),
        lambda t: sa.select(t.c.id).offset(2),
    ),
    (
        "count",
        lambda t: select(func.count()),
        lambda t: sa.select(sa.func.count()),
    ),
    (
        "lower",
        lambda t: select(func.lower(t.c.name)),
        lambda t: sa.select(sa.func.lower(t.c.name)),
    ),
    (
        "insert-values",
        lambda t: insert(t).values(id=1, name="x", flag=True),
        lambda t: t.insert().values(id=1, name="x", flag=True),
    ),
    (
        "insert-empty",
        lambda t: insert(t),
        lambda t: t.insert(),
    ),
    (
        "update",
        lambda t: update(t).where(t.c.id == 1).values(name="y"),
        lambda t: t.update().where(t.c.id == 1).values(name="y"),
    ),
    (
        "delete",
        lambda t: delete(t).where(t.c.id == 1),
        lambda t: t.delete().where(t.c.id == 1),
    ),
]


@pytest.mark.parametrize(
    ("label", "catalyst_stmt", "sa_stmt"), CASES, ids=[case[0] for case in CASES]
)
def test_pg_compilation_matches_sqlalchemy(
    label: str,
    catalyst_stmt: Callable[[Any], Any],
    sa_stmt: Callable[[Any], Any],
) -> None:
    table, sa_table = _table()
    compiled = catalyst_stmt(table).compile(dialect="postgresql")
    sa_compiled = sa_stmt(sa_table).compile(dialect=PG, compile_kwargs={"render_postcompile": True})
    assert compiled.string == _normalize(str(sa_compiled)), label
    assert compiled.params == dict(sa_compiled.params), label


EMPTY_IN_CASES = [
    (
        "empty-in",
        lambda t: select(t.c.id).where(t.c.id.in_([])),
        lambda t: sa.select(t.c.id).where(t.c.id.in_([])),
    ),
    (
        "empty-not-in",
        lambda t: select(t.c.id).where(not_(t.c.id.in_([]))),
        lambda t: sa.select(t.c.id).where(t.c.id.not_in([])),
    ),
]


@pytest.mark.parametrize(
    ("label", "catalyst_stmt", "sa_stmt"), EMPTY_IN_CASES, ids=[c[0] for c in EMPTY_IN_CASES]
)
def test_pg_empty_in_matches_sqlalchemy(
    label: str,
    catalyst_stmt: Callable[[Any], Any],
    sa_stmt: Callable[[Any], Any],
) -> None:
    table, sa_table = _table()
    compiled = catalyst_stmt(table).compile(dialect="postgresql")
    sa_compiled = sa_stmt(sa_table).compile(dialect=PG, compile_kwargs={"render_postcompile": True})
    assert compiled.string == _normalize(str(sa_compiled)), label
    assert compiled.params == {}


def _ddl_table(meta: Any, sa_meta: Any, name: str = "items") -> tuple[Any, Any]:
    sa_table = sa.Table(
        name,
        sa_meta,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(30)),
        sa.Column("body", sa.Text),
        sa.Column("flag", sa.Boolean),
        sa.Column("score", sa.Float),
        sa.Column("created", sa.DateTime),
        sa.Column("day", sa.Date),
        sa.Column("payload", sa.LargeBinary),
    )
    table = Table(
        name,
        meta,
        Column("id", Integer, primary_key=True),
        Column("name", String(30)),
        Column("body", Text),
        Column("flag", Boolean),
        Column("score", Float),
        Column("created", DateTime),
        Column("day", Date),
        Column("payload", LargeBinary),
    )
    return table, sa_table


def test_pg_create_table_matches_sqlalchemy() -> None:
    table, sa_table = _ddl_table(MetaData(), sa.MetaData())
    assert table.compile_ddl(dialect="postgresql").string == str(
        sa.schema.CreateTable(sa_table).compile(dialect=PG)
    )
    assert table.compile_drop(dialect="postgresql").string == str(
        sa.schema.DropTable(sa_table).compile(dialect=PG)
    )


DDL_VARIANTS = [
    (
        "composite-pk",
        [Column("x", Integer, primary_key=True), Column("y", Integer, primary_key=True)],
        [
            sa.Column("x", sa.Integer, primary_key=True),
            sa.Column("y", sa.Integer, primary_key=True),
        ],
    ),
    (
        "string-pk",
        [Column("x", String(10), primary_key=True)],
        [sa.Column("x", sa.String(10), primary_key=True)],
    ),
    (
        "server-default-unique",
        [
            Column("x", Integer),
            Column("s", String(5), server_default="hi", unique=True),
        ],
        [
            sa.Column("x", sa.Integer),
            sa.Column("s", sa.String(5), server_default="hi", unique=True),
        ],
    ),
    (
        "no-columns",
        [],
        [],
    ),
]


@pytest.mark.parametrize(
    ("label", "columns", "sa_columns"), DDL_VARIANTS, ids=[c[0] for c in DDL_VARIANTS]
)
def test_pg_ddl_variants_match_sqlalchemy(
    label: str, columns: list[Any], sa_columns: list[Any]
) -> None:
    table = Table("variant", MetaData(), *columns)
    sa_table = sa.Table("variant", sa.MetaData(), *sa_columns)
    assert table.compile_ddl(dialect="postgresql").string == str(
        sa.schema.CreateTable(sa_table).compile(dialect=PG)
    ), label


def test_pg_reserved_identifiers_are_quoted() -> None:
    table = Table("order", MetaData(), Column("select", Integer), Column("MixedCase", Integer))
    sa_table = sa.Table(
        "order",
        sa.MetaData(),
        sa.Column("select", sa.Integer),
        sa.Column("MixedCase", sa.Integer),
    )
    compiled = select(table.c.select).where(table.c.MixedCase == 1).compile(dialect="postgresql")
    sa_compiled = sa.select(sa_table.c.select).where(sa_table.c.MixedCase == 1).compile(dialect=PG)
    assert compiled.string == _normalize(str(sa_compiled))
    assert table.compile_ddl(dialect="postgresql").string == str(
        sa.schema.CreateTable(sa_table).compile(dialect=PG)
    )


def test_pg_cache_is_dialect_scoped() -> None:
    table, _ = _table()
    statement = select(table.c.id).where(table.c.id == 5)
    sqlite_sql = statement.compile().string
    pg_sql = statement.compile(dialect="postgresql").string
    assert "?" in sqlite_sql
    assert "$1" in pg_sql
    assert statement.compile().string == sqlite_sql
    assert statement.compile(dialect="postgresql").string == pg_sql


def test_unknown_dialect_rejected() -> None:
    table, _ = _table()
    with pytest.raises(catalyst.ArgumentError):
        select(table.c.id).compile(dialect="oracle")


def test_sqlite_string_concat_now_matches_sqlalchemy() -> None:
    table, sa_table = _table()
    compiled = select(table.c.name + table.c.body).compile()
    sa_compiled = sa.select(sa_table.c.name + sa_table.c.body).compile()
    assert compiled.string == str(sa_compiled)
    assert compiled.params == dict(sa_compiled.params)
