"""SQLite expression compiler parity against SQLAlchemy 2.1."""

from __future__ import annotations

import pytest
from catalyst import (
    ArgumentError,
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    not_,
    or_,
    select,
    update,
)
from sqlalchemy import (
    Boolean as SABoolean,
)
from sqlalchemy import (
    Column as SAColumn,
)
from sqlalchemy import (
    DateTime as SADateTime,
)
from sqlalchemy import (
    Float as SAFloat,
)
from sqlalchemy import (
    Integer as SAInteger,
)
from sqlalchemy import (
    MetaData as SAMetaData,
)
from sqlalchemy import (
    String as SAString,
)
from sqlalchemy import (
    Table as SATable,
)
from sqlalchemy import (
    Text as SAText,
)
from sqlalchemy import (
    and_ as sa_and,
)
from sqlalchemy import (
    delete as sa_delete,
)
from sqlalchemy import (
    func as sa_func,
)
from sqlalchemy import (
    insert as sa_insert,
)
from sqlalchemy import (
    not_ as sa_not,
)
from sqlalchemy import (
    or_ as sa_or,
)
from sqlalchemy import (
    select as sa_select,
)
from sqlalchemy import (
    update as sa_update,
)
from sqlalchemy.dialects import sqlite


def _tables() -> tuple[Table, SATable]:
    metadata = MetaData()
    table = Table(
        "records",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(30)),
        Column("score", Float),
        Column("enabled", Boolean),
        Column("created", DateTime),
        Column("body", Text),
    )
    sa_metadata = SAMetaData()
    sa_table = SATable(
        "records",
        sa_metadata,
        SAColumn("id", SAInteger, primary_key=True),
        SAColumn("name", SAString(30)),
        SAColumn("score", SAFloat),
        SAColumn("enabled", SABoolean),
        SAColumn("created", SADateTime),
        SAColumn("body", SAText),
    )
    return table, sa_table


def _assert_parity(catalyst_statement: object, sqlalchemy_statement: object) -> None:
    catalyst_compiled = catalyst_statement.compile()  # type: ignore[attr-defined]
    sa_compiled = sqlalchemy_statement.compile(  # type: ignore[attr-defined]
        dialect=sqlite.dialect(), compile_kwargs={"render_postcompile": True}
    )
    assert str(catalyst_compiled) == str(sa_compiled)
    assert catalyst_compiled.params == dict(sa_compiled.params)


@pytest.mark.parametrize(
    ("build", "sa_build"),
    [
        (
            lambda t: select(t.c.id, t.c.name),
            lambda t: sa_select(t.c.id, t.c.name),
        ),
        (
            lambda t: select(t.c.id).where(t.c.score > 10).where(t.c.enabled == True),  # noqa: E712
            lambda t: sa_select(t.c.id).where(t.c.score > 10).where(t.c.enabled == True),  # noqa: E712
        ),
        (
            lambda t: select(t.c.id).where(and_(t.c.id > 2, t.c.score < 9)),
            lambda t: sa_select(t.c.id).where(sa_and(t.c.id > 2, t.c.score < 9)),
        ),
        (
            lambda t: select(t.c.id).where(or_(t.c.id == 2, t.c.id == 3)),
            lambda t: sa_select(t.c.id).where(sa_or(t.c.id == 2, t.c.id == 3)),
        ),
        (
            lambda t: select(t.c.id).where(not_(t.c.id == 2)),
            lambda t: sa_select(t.c.id).where(sa_not(t.c.id == 2)),
        ),
        (
            lambda t: select(t.c.id).where(t.c.id.in_([1, 2, 3])),
            lambda t: sa_select(t.c.id).where(t.c.id.in_([1, 2, 3])),
        ),
        (
            lambda t: select(t.c.id).where(t.c.id + t.c.score * 2 > 10),
            lambda t: sa_select(t.c.id).where(t.c.id + t.c.score * 2 > 10),
        ),
        (
            lambda t: select(t.c.id).where(t.c.name.like("A%")),
            lambda t: sa_select(t.c.id).where(t.c.name.like("A%")),
        ),
        (
            lambda t: select(t.c.id).where(t.c.name.is_(None)),
            lambda t: sa_select(t.c.id).where(t.c.name.is_(None)),
        ),
        (
            lambda t: select(t.c.id).where(t.c.name.is_not(None)),
            lambda t: sa_select(t.c.id).where(t.c.name.is_not(None)),
        ),
        (
            lambda t: select(t.c.id).where(t.c.score.between(1, 8)),
            lambda t: sa_select(t.c.id).where(t.c.score.between(1, 8)),
        ),
        (
            lambda t: select(t.c.id).order_by(t.c.score.asc(), t.c.id.desc()),
            lambda t: sa_select(t.c.id).order_by(t.c.score.asc(), t.c.id.desc()),
        ),
        (
            lambda t: select(t.c.id).limit(10).offset(2),
            lambda t: sa_select(t.c.id).limit(10).offset(2),
        ),
        (
            lambda t: insert(t).values(id=1, name="Ada"),
            lambda t: sa_insert(t).values(id=1, name="Ada"),
        ),
        (
            lambda t: update(t).values(name="Ada").where(t.c.id == 1),
            lambda t: sa_update(t).values(name="Ada").where(t.c.id == 1),
        ),
        (
            lambda t: delete(t).where(t.c.id == 1),
            lambda t: sa_delete(t).where(t.c.id == 1),
        ),
        (
            lambda t: select(func.count(t.c.id)),
            lambda t: sa_select(sa_func.count(t.c.id)),
        ),
        (
            lambda t: select(func.lower(t.c.name)),
            lambda t: sa_select(sa_func.lower(t.c.name)),
        ),
    ],
    ids=[
        "simple-select",
        "where-chain",
        "and",
        "or",
        "not",
        "in",
        "arithmetic",
        "like",
        "is-null",
        "is-not-null",
        "between",
        "order-by-asc-desc",
        "limit-offset",
        "insert-values",
        "update-values-where",
        "delete-where",
        "func-count",
        "func-lower",
    ],
)
def test_statement_compilation_matches_sqlalchemy(build: object, sa_build: object) -> None:
    table, sa_table = _tables()
    _assert_parity(build(table), sa_build(sa_table))  # type: ignore[operator]


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    [("select", "order"), ("plain", "MixedCase"), ("has-dash", "odd-name")],
)
def test_identifier_quoting_matches_sqlalchemy(table_name: str, column_name: str) -> None:
    table = Table(table_name, MetaData(), Column(column_name, Integer))
    sa_table = SATable(table_name, SAMetaData(), SAColumn(column_name, SAInteger))
    _assert_parity(select(table.c[column_name]), sa_select(sa_table.c[column_name]))


def test_select_table_expands_all_columns() -> None:
    table, sa_table = _tables()
    _assert_parity(select(table), sa_select(sa_table))


def test_chained_values_accumulate_and_match_sqlalchemy() -> None:
    table, sa_table = _tables()
    _assert_parity(
        insert(table).values(id=1).values(name="Ada"),
        sa_insert(sa_table).values(id=1).values(name="Ada"),
    )
    _assert_parity(
        update(table).values(id=1).values(name="Ada"),
        sa_update(sa_table).values(id=1).values(name="Ada"),
    )


@pytest.mark.parametrize(
    ("clause", "sa_clause"),
    [
        (lambda t: t.c.id.in_([1, 2]), lambda t: t.c.id.in_([1, 2])),
        (
            lambda t: and_(t.c.id.in_([1, 2]), t.c.id == 5),
            lambda t: sa_and(t.c.id.in_([1, 2]), t.c.id == 5),
        ),
        (
            lambda t: and_(t.c.id == 5, t.c.name.in_(["a", "b"])),
            lambda t: sa_and(t.c.id == 5, t.c.name.in_(["a", "b"])),
        ),
        (
            lambda t: and_(t.c.id.in_([1, 2]), t.c.id.in_([3])),
            lambda t: sa_and(t.c.id.in_([1, 2]), t.c.id.in_([3])),
        ),
    ],
    ids=["in-alone", "in-then-eq", "eq-then-in", "two-in-groups"],
)
def test_in_expansion_bind_names_match_sqlalchemy(clause: object, sa_clause: object) -> None:
    table, sa_table = _tables()
    _assert_parity(
        select(table.c.id).where(clause(table)),
        sa_select(sa_table.c.id).where(sa_clause(sa_table)),
    )  # type: ignore[operator]


def test_is_scalar_comparisons_match_sqlalchemy() -> None:
    table, sa_table = _tables()
    for catalyst_clause, sqlalchemy_clause in (
        (table.c.id.is_(5), sa_table.c.id.is_(5)),
        (table.c.id.is_not(5), sa_table.c.id.is_not(5)),
        (not_(table.c.id.is_(5)), sa_not(sa_table.c.id.is_(5))),
        (not_(table.c.id.is_not(5)), sa_not(sa_table.c.id.is_not(5))),
    ):
        _assert_parity(
            select(table.c.id).where(catalyst_clause),
            sa_select(sa_table.c.id).where(sqlalchemy_clause),
        )


@pytest.mark.parametrize(
    ("clause", "sa_clause"),
    [
        (lambda t: t.c.id == 5, lambda t: t.c.id == 5),
        (lambda t: t.c.id < 5, lambda t: t.c.id < 5),
        (lambda t: t.c.id.in_([1, 2]), lambda t: t.c.id.in_([1, 2])),
        (lambda t: t.c.name.like("a%"), lambda t: t.c.name.like("a%")),
        (lambda t: t.c.id.between(1, 5), lambda t: t.c.id.between(1, 5)),
        (lambda t: t.c.id.is_(None), lambda t: t.c.id.is_(None)),
    ],
    ids=["eq", "lt", "in", "like", "between", "is"],
)
def test_not_negation_family_matches_sqlalchemy(clause: object, sa_clause: object) -> None:
    table, sa_table = _tables()
    _assert_parity(
        select(table.c.id).where(not_(clause(table))),
        sa_select(sa_table.c.id).where(sa_not(sa_clause(sa_table))),
    )  # type: ignore[operator]


def test_like_escape_matches_sqlalchemy_including_quoted_escape() -> None:
    table, sa_table = _tables()
    for escape in ("/", "'"):
        _assert_parity(
            select(table.c.name).where(table.c.name.like("a/%", escape=escape)),
            sa_select(sa_table.c.name).where(sa_table.c.name.like("a/%", escape=escape)),
        )


def test_duplicate_plain_columns_use_sqlalchemy_labels() -> None:
    table, sa_table = _tables()
    _assert_parity(
        select(table.c.id, table.c.id, table.c.name, table.c.name),
        sa_select(sa_table.c.id, sa_table.c.id, sa_table.c.name, sa_table.c.name),
    )


def test_boolean_word_compilation_matches_sqlalchemy() -> None:
    table, sa_table = _tables()
    compiled = select(table.c.id).where(True).compile()
    assert str(compiled).endswith("WHERE 1 = 1")
    sa_compiled = (
        sa_select(sa_table.c.id)
        .where(True)
        .compile(dialect=sqlite.dialect(), compile_kwargs={"render_postcompile": True})
    )
    # SQLAlchemy renders a bare Python bool predicate as a constant comparison.
    assert str(sa_compiled).endswith("WHERE 1 = 1")


def test_empty_in_expansion_matches_sqlalchemy() -> None:
    table, sa_table = _tables()
    _assert_parity(
        select(table.c.id).where(table.c.id.in_([])),
        sa_select(sa_table.c.id).where(sa_table.c.id.in_([])),
    )
    _assert_parity(
        select(table.c.id).where(not_(table.c.id.in_([]))),
        sa_select(sa_table.c.id).where(sa_not(sa_table.c.id.in_([]))),
    )


def test_in_rejects_strings() -> None:
    table, _ = _tables()
    with pytest.raises(ArgumentError, match="sequence of scalar values"):
        table.c.id.in_("123")  # type: ignore[arg-type]


def test_deep_public_expression_raises_recursion_error() -> None:
    table, _ = _tables()
    expression = table.c.id == 1
    for _ in range(1200):
        expression = and_(expression)
    with pytest.raises(RecursionError):
        select(table.c.id).where(expression).compile()


def test_direct_ffi_expression_depth_guard_is_catchable() -> None:
    from catalyst._catalyst import compile_expression

    node: tuple[object, ...] = ("column", "records", "id")
    for _ in range(2001):
        node = ("not", node)
    with pytest.raises(ArgumentError, match="expression nesting too deep"):
        compile_expression(("select", (node,), "records", None, (), None, None))


def test_compile_parameters_and_execution_rowcount() -> None:
    table = Table(
        "items", MetaData(), Column("id", Integer, primary_key=True), Column("name", String)
    )
    statement = insert(table).values(id=1, name="Ada")
    compiled = statement.compile()
    assert str(compiled) == "INSERT INTO items (id, name) VALUES (?, ?)"
    assert compiled.params == {"id": 1, "name": "Ada"}

    from catalyst import create_engine

    engine = create_engine(":memory:")
    try:
        with engine.connect() as connection:
            connection.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT)")
            assert connection.execute(statement).rowcount == 1
            assert (
                connection.execute(
                    update(table).values(name="Grace").where(table.c.id == 1)
                ).rowcount
                == 1
            )
            assert connection.execute(select(table.c.name).where(table.c.id == 1)).fetchall() == [
                ("Grace",)
            ]
            assert connection.execute(delete(table).where(table.c.id == 1)).rowcount == 1
    finally:
        engine.dispose()
