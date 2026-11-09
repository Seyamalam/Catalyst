"""SQLite typed binding and result coercion parity against SQLAlchemy 2.1."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from catalyst import ProgrammingError
from catalyst.engine import create_engine as catalyst_create_engine
from catalyst.expression import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from catalyst.expression import (
    LargeBinary as CatalystLargeBinary,
)
from sqlalchemy import (
    Boolean as SABoolean,
)
from sqlalchemy import (
    Column as SAColumn,
)
from sqlalchemy import (
    Date as SADate,
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
    LargeBinary as SALargeBinary,
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
    create_engine as sa_create_engine,
)
from sqlalchemy import (
    insert as sa_insert,
)
from sqlalchemy import (
    select as sa_select,
)
from sqlalchemy import (
    update as sa_update,
)


@pytest.mark.parametrize(
    "values",
    [
        (
            7,
            "Ada",
            "A long note",
            True,
            1.25,
            datetime(2024, 2, 3, 4, 5, 6, 123456),
            date(2024, 2, 3),
            b"\x00bytes",
            None,
        ),
        (
            8,
            "Grace",
            "",
            False,
            2,
            datetime(2024, 2, 3, 4, 5, 6, 1, tzinfo=timezone(timedelta(hours=5, minutes=30))),
            date(2024, 2, 4),
            b"",
            None,
        ),
    ],
    ids=["naive-microseconds", "aware-and-false"],
)
def test_typed_roundtrip_matches_sqlalchemy(values: tuple[object, ...]) -> None:
    catalyst_metadata = MetaData()
    catalyst_table = Table(
        "typed_values",
        catalyst_metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String(40)),
        Column("note", Text),
        Column("enabled", Boolean),
        Column("ratio", Float),
        Column("created", DateTime),
        Column("day", Date),
        Column("payload", CatalystLargeBinary),
        Column("optional", String),
    )
    sa_metadata = SAMetaData()
    sa_table = SATable(
        "typed_values",
        sa_metadata,
        SAColumn("id", SAInteger, primary_key=True),
        SAColumn("name", SAString(40)),
        SAColumn("note", SAText),
        SAColumn("enabled", SABoolean),
        SAColumn("ratio", SAFloat),
        SAColumn("created", SADateTime),
        SAColumn("day", SADate),
        SAColumn("payload", SALargeBinary),
        SAColumn("optional", SAString),
    )
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_metadata.create_all(sqlalchemy)
        row = dict(zip((column.name for column in catalyst_table.columns), values, strict=True))
        with catalyst.begin() as connection:
            connection.execute(insert(catalyst_table).values(row))
            catalyst_rows = connection.execute(select(catalyst_table)).all()
        with sqlalchemy.begin() as connection:
            connection.execute(sa_insert(sa_table), row)
            sqlalchemy_rows = connection.execute(sa_select(sa_table)).all()
        assert catalyst_rows == sqlalchemy_rows
        actual = catalyst_rows[0]
        assert type(actual[0]) is int
        assert type(actual[1]) is str
        assert type(actual[2]) is str
        assert type(actual[3]) is bool
        assert type(actual[4]) is float
        assert type(actual[5]) is datetime
        assert type(actual[6]) is date
        assert type(actual[7]) is bytes
        assert actual[8] is None
        assert actual[5].tzinfo is None
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_datetime_type_accepts_date_and_discards_timezone_on_storage() -> None:
    day = date(2020, 1, 2)
    aware = datetime(2020, 1, 2, 3, 4, 5, 1234, tzinfo=timezone(timedelta(hours=-4)))
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    catalyst_table = Table("dates", MetaData(), Column("value", DateTime))
    sa_metadata = SAMetaData()
    sa_table = SATable("dates", sa_metadata, SAColumn("value", SADateTime))
    try:
        catalyst_table.create(catalyst)
        sa_metadata.create_all(sqlalchemy)
        for value in (day, aware):
            with catalyst.begin() as connection:
                connection.execute(insert(catalyst_table).values(value=value))
            with sqlalchemy.begin() as connection:
                connection.execute(sa_insert(sa_table).values(value=value))
        with catalyst.connect() as connection:
            actual = connection.execute(select(catalyst_table.c.value)).all()
        with sqlalchemy.connect() as connection:
            expected = connection.execute(sa_select(sa_table.c.value)).all()
        assert actual == expected
        assert actual == [(datetime(2020, 1, 2),), (datetime(2020, 1, 2, 3, 4, 5, 1234),)]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_typed_update_and_datetime_where_binds_match_sqlalchemy() -> None:
    instant = datetime(2024, 3, 4, 5, 6, 7, 123456)
    catalyst_table = Table(
        "typed_update", MetaData(), Column("id", Integer), Column("created", DateTime)
    )
    sa_table = SATable(
        "typed_update",
        SAMetaData(),
        SAColumn("id", SAInteger),
        SAColumn("created", SADateTime),
    )
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            connection.execute(insert(catalyst_table).values(id=1, created=instant))
            rowcount = connection.execute(
                update(catalyst_table)
                .values(created=datetime(2025, 1, 2))
                .where(catalyst_table.c.created == instant)
            ).rowcount
            rows = connection.execute(select(catalyst_table)).all()
        with sqlalchemy.begin() as connection:
            connection.execute(sa_insert(sa_table).values(id=1, created=instant))
            sa_rowcount = connection.execute(
                sa_update(sa_table)
                .values(created=datetime(2025, 1, 2))
                .where(sa_table.c.created == instant)
            ).rowcount
            sa_rows = connection.execute(sa_select(sa_table)).all()
        assert rowcount == sa_rowcount == 1
        assert rows == sa_rows == [(1, datetime(2025, 1, 2))]
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_malformed_datetime_result_matches_sqlalchemy_value_error() -> None:
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    catalyst_table = Table("bad_dates", MetaData(), Column("value", DateTime))
    sa_table = SATable("bad_dates", SAMetaData(), SAColumn("value", SADateTime))
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.connect() as connection:
            connection.execute("INSERT INTO bad_dates VALUES (?)", ["not-a-date"])
            with pytest.raises(ValueError, match="Invalid isoformat string") as catalyst_error:
                connection.execute(select(catalyst_table.c.value)).all()
        with sqlalchemy.connect() as connection:
            connection.exec_driver_sql("INSERT INTO bad_dates VALUES (?)", ("not-a-date",))
            with pytest.raises(ValueError, match="Invalid isoformat string") as sa_error:
                connection.execute(sa_select(sa_table.c.value)).all()
        assert type(catalyst_error.value) is type(sa_error.value)
        assert str(catalyst_error.value) == str(sa_error.value)
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@pytest.mark.parametrize(
    "malformed",
    ["not-a-date", "2020-1-1 1:2:3", "2020-01-01 01:02:03."],
)
def test_malformed_datetime_result_matches_sqlalchemy_for_common_forms(malformed: str) -> None:
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    catalyst_table = Table("more_bad_dates", MetaData(), Column("value", DateTime))
    sa_table = SATable("more_bad_dates", SAMetaData(), SAColumn("value", SADateTime))
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.connect() as connection:
            connection.execute("INSERT INTO more_bad_dates VALUES (?)", [malformed])
            with pytest.raises(ValueError) as catalyst_error:
                connection.execute(select(catalyst_table.c.value)).all()
        with sqlalchemy.connect() as connection:
            connection.exec_driver_sql("INSERT INTO more_bad_dates VALUES (?)", (malformed,))
            with pytest.raises(ValueError) as sa_error:
                connection.execute(sa_select(sa_table.c.value)).all()
        assert type(catalyst_error.value) is type(sa_error.value)
        assert str(catalyst_error.value) == str(sa_error.value)
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (
            "2020-01-01 05:00:00.000000+05:30",
            datetime.fromisoformat("2020-01-01 05:00:00.000000+05:30"),
        ),
        (
            "2020-01-01 05:00:00.000000-04:45",
            datetime.fromisoformat("2020-01-01 05:00:00.000000-04:45"),
        ),
    ],
    ids=["positive-offset", "negative-offset"],
)
def test_offset_datetime_result_matches_sqlalchemy(stored: str, expected: datetime) -> None:
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    catalyst_table = Table("offset_dates", MetaData(), Column("value", DateTime))
    sa_table = SATable("offset_dates", SAMetaData(), SAColumn("value", SADateTime))
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.connect() as connection:
            connection.execute("INSERT INTO offset_dates VALUES (?)", [stored])
            actual = connection.execute(select(catalyst_table.c.value)).all()
        with sqlalchemy.connect() as connection:
            connection.exec_driver_sql("INSERT INTO offset_dates VALUES (?)", (stored,))
            expected_rows = connection.execute(sa_select(sa_table.c.value)).all()
        assert actual == expected_rows == [(expected,)]
        assert actual[0][0].utcoffset() == expected.utcoffset()
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_malformed_utf8_boundary_datetime_result_raises_value_error() -> None:
    catalyst = catalyst_create_engine(":memory:")
    table = Table("utf8_dates", MetaData(), Column("value", DateTime))
    try:
        table.create(catalyst)
        with catalyst.connect() as connection:
            connection.execute("INSERT INTO utf8_dates VALUES (?)", ["2020-01-02中"])
            with pytest.raises(ValueError, match="Invalid isoformat string: '2020-01-02中'"):
                connection.execute(select(table.c.value)).all()
    finally:
        catalyst.dispose()


def test_negated_column_does_not_shift_later_result_type_tags() -> None:
    instant = datetime(2020, 1, 2, 3, 4, 5)
    table = Table("negated_tags", MetaData(), Column("id", Integer), Column("created", DateTime))
    engine = catalyst_create_engine(":memory:")
    try:
        table.create(engine)
        with engine.begin() as connection:
            connection.execute(insert(table).values(id=3, created=instant))
        with engine.connect() as connection:
            assert connection.execute(select(-table.c.id, table.c.created)).all() == [(-3, instant)]
    finally:
        engine.dispose()


def test_mismatched_comparison_literals_match_sqlalchemy() -> None:
    catalyst_table = Table(
        "mismatched_literals", MetaData(), Column("enabled", Boolean), Column("created", DateTime)
    )
    sa_table = SATable(
        "mismatched_literals",
        SAMetaData(),
        SAColumn("enabled", SABoolean),
        SAColumn("created", SADateTime),
    )
    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        catalyst_table.create(catalyst)
        sa_table.metadata.create_all(sqlalchemy)
        with catalyst.begin() as connection:
            connection.execute(
                insert(catalyst_table).values(enabled=True, created=datetime(2020, 1, 1))
            )
        with sqlalchemy.begin() as connection:
            connection.execute(
                sa_insert(sa_table).values(enabled=True, created=datetime(2020, 1, 1))
            )
        with catalyst.connect() as connection:
            bool_rows = connection.execute(
                select(catalyst_table.c.enabled).where(catalyst_table.c.enabled == 2)
            ).all()
            datetime_rows = connection.execute(
                select(catalyst_table.c.created).where(catalyst_table.c.created == 5)
            ).all()
        with sqlalchemy.connect() as connection:
            sa_bool_rows = connection.execute(
                sa_select(sa_table.c.enabled).where(sa_table.c.enabled == 2)
            ).all()
            sa_datetime_rows = connection.execute(
                sa_select(sa_table.c.created).where(sa_table.c.created == 5)
            ).all()
        assert bool_rows == sa_bool_rows == []
        assert datetime_rows == sa_datetime_rows == []
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()


def test_decimal_numeric_is_not_silently_coerced() -> None:
    from decimal import Decimal

    catalyst = catalyst_create_engine(":memory:")
    sqlalchemy = sa_create_engine("sqlite:///:memory:")
    try:
        with catalyst.connect() as connection:
            with pytest.raises(ProgrammingError, match="type 'Decimal' is not supported"):
                connection.execute("SELECT ?", [Decimal("1.25")])
        with sqlalchemy.connect() as connection:
            with pytest.raises(Exception) as sqlalchemy_error:
                connection.exec_driver_sql("SELECT ?", (Decimal("1.25"),))
            assert type(sqlalchemy_error.value).__name__ == "ProgrammingError"
    finally:
        catalyst.dispose()
        sqlalchemy.dispose()
