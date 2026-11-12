"""End-to-end ORM benchmarks: Catalyst vs SQLAlchemy 2.1.8 on sqlite3.

Run from the repository root after ``maturin develop --release``::

    python benches/bench_orm.py [--reps N] [--warmup N] [--quick] [--case NAME]...

Both engines run against in-memory SQLite (``":memory:"`` for Catalyst,
``"sqlite:///:memory:"`` for SQLAlchemy — each resolves to a single shared
connection). Every case performs identical work on both sides: the same
model shape, the same row data, the same statements. Sessions are rebuilt
per repetition so neither side benefits from a warm identity map unless the
case is specifically about it (``session_get_identity``).

Each number is the median of ``--reps`` timed runs after ``--warmup``
untimed runs. Speedups are ``sqlalchemy / catalyst``: above 1.0 means
Catalyst is faster.
"""

from __future__ import annotations

import argparse
import gc
import platform
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import catalyst
import sqlalchemy as sa
import sqlalchemy.orm as sa_orm
from catalyst import (
    DeclarativeBase,
    ForeignKey,
    Mapped,
    Session,
    create_engine,
    insert,
    mapped_column,
    relationship,
    select,
    selectinload,
    text,
)


class CBase(DeclarativeBase):
    pass


class CUser(CBase):
    __tablename__ = "bench_user"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    score: Mapped[float]
    active: Mapped[bool]
    nick: Mapped[str | None]
    day: Mapped[date]
    payload: Mapped[bytes]


class CParent(CBase):
    __tablename__ = "bench_parent"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    children: Mapped[list[CChild]] = relationship(back_populates="parent")


class CChild(CBase):
    __tablename__ = "bench_child"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    parent_id: Mapped[int] = mapped_column(ForeignKey("bench_parent.id"))
    parent: Mapped[CParent] = relationship(back_populates="children")


class SABase(sa_orm.DeclarativeBase):
    pass


class SAUser(SABase):
    __tablename__ = "bench_user"
    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    name: sa_orm.Mapped[str]
    score: sa_orm.Mapped[float]
    active: sa_orm.Mapped[bool]
    nick: sa_orm.Mapped[str | None]
    day: sa_orm.Mapped[date]
    payload: sa_orm.Mapped[bytes]


class SAParent(SABase):
    __tablename__ = "bench_parent"
    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    name: sa_orm.Mapped[str]
    children: sa_orm.Mapped[list[SAChild]] = sa_orm.relationship(back_populates="parent")


class SAChild(SABase):
    __tablename__ = "bench_child"
    id: sa_orm.Mapped[int] = sa_orm.mapped_column(primary_key=True)
    name: sa_orm.Mapped[str]
    parent_id: sa_orm.Mapped[int] = sa_orm.mapped_column(sa.ForeignKey("bench_parent.id"))
    parent: sa_orm.Mapped[SAParent] = sa_orm.relationship(back_populates="children")


DAY = date(2024, 1, 1)


def _row(index: int) -> dict[str, Any]:
    return {
        "id": index,
        "name": f"user_{index}",
        "score": index * 0.5 + 1.0,
        "active": index % 2 == 0,
        "nick": None if index % 3 else f"nick_{index}",
        "day": DAY,
        "payload": b"\x00\x01\x02\x03" + index.to_bytes(4, "little"),
    }


@dataclass
class CaseResult:
    name: str
    size: str
    impl: str
    seconds: float
    runs: list[float]


def _time(runs: list[float], timed: Callable[[], object], reps: int, warmup: int) -> float:
    """Run ``warmup`` untimed reps, then ``reps`` timed ones.

    ``timed`` appends its own elapsed seconds to ``runs`` so setup work
    (object construction, cleanup, session close) can sit outside its
    internal ``perf_counter`` window. Warm-up timings are discarded.
    """
    for _ in range(warmup):
        timed()
    del runs[:]
    for _ in range(reps):
        gc.collect()
        timed()
    return statistics.median(runs)


def _clear_catalyst(engine: catalyst.Engine) -> None:
    with engine.begin() as connection:
        for table in ("bench_user", "bench_child", "bench_parent"):
            connection.execute(text(f"DELETE FROM {table}"))


def _clear_sa(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        for table in ("bench_user", "bench_child", "bench_parent"):
            connection.execute(sa.text(f"DELETE FROM {table}"))


# -- case: session.add_all + flush -----------------------------------------


def bench_insert(n: int, reps: int, warmup: int) -> list[CaseResult]:
    out: list[CaseResult] = []
    cengine = create_engine(":memory:")
    sa_engine = sa.create_engine("sqlite:///:memory:")
    try:
        CBase.metadata.create_all(cengine)
        SABase.metadata.create_all(sa_engine)

        def catalyst_run() -> None:
            objects = [CUser(**_row(index)) for index in range(n)]
            _clear_catalyst(cengine)
            start = time.perf_counter()
            session = Session(cengine)
            session.add_all(objects)
            session.flush()
            elapsed = time.perf_counter() - start
            session.rollback()
            session.close()
            runs.append(elapsed)

        def sa_run() -> None:
            objects = [SAUser(**_row(index)) for index in range(n)]
            _clear_sa(sa_engine)
            start = time.perf_counter()
            session = sa_orm.Session(sa_engine)
            session.add_all(objects)
            session.flush()
            elapsed = time.perf_counter() - start
            session.rollback()
            session.close()
            runs.append(elapsed)

        runs: list[float] = []
        median = _time(runs, catalyst_run, reps, warmup)
        out.append(CaseResult("insert_add_all_flush", str(n), "catalyst", median, runs))
        runs = []
        median = _time(runs, sa_run, reps, warmup)
        out.append(CaseResult("insert_add_all_flush", str(n), "sqlalchemy", median, runs))
    finally:
        cengine.dispose()
        sa_engine.dispose()
    return out


# -- case: select + hydrate -------------------------------------------------


def bench_select_hydrate(n: int, reps: int, warmup: int) -> list[CaseResult]:
    out: list[CaseResult] = []
    cengine = create_engine(":memory:")
    sa_engine = sa.create_engine("sqlite:///:memory:")
    try:
        CBase.metadata.create_all(cengine)
        SABase.metadata.create_all(sa_engine)
        rows = [_row(index) for index in range(n)]
        with cengine.begin() as connection:
            connection.execute(insert(CUser.__table__), rows)
        with sa_engine.begin() as connection:
            connection.execute(sa.insert(SAUser.__table__), rows)

        def catalyst_run() -> None:
            start = time.perf_counter()
            session = Session(cengine)
            objects = session.scalars(select(CUser)).all()
            elapsed = time.perf_counter() - start
            assert len(objects) == n
            session.close()
            runs.append(elapsed)

        def sa_run() -> None:
            start = time.perf_counter()
            session = sa_orm.Session(sa_engine)
            objects = session.scalars(sa.select(SAUser)).all()
            elapsed = time.perf_counter() - start
            assert len(objects) == n
            session.close()
            runs.append(elapsed)

        runs: list[float] = []
        median = _time(runs, catalyst_run, reps, warmup)
        out.append(CaseResult("select_hydrate", str(n), "catalyst", median, runs))
        runs = []
        median = _time(runs, sa_run, reps, warmup)
        out.append(CaseResult("select_hydrate", str(n), "sqlalchemy", median, runs))
    finally:
        cengine.dispose()
        sa_engine.dispose()
    return out


# -- case: executemany -------------------------------------------------------


def bench_executemany(n: int, reps: int, warmup: int) -> list[CaseResult]:
    out: list[CaseResult] = []
    cengine = create_engine(":memory:")
    sa_engine = sa.create_engine("sqlite:///:memory:")
    try:
        CBase.metadata.create_all(cengine)
        SABase.metadata.create_all(sa_engine)
        rows = [_row(index) for index in range(n)]

        def catalyst_run() -> None:
            _clear_catalyst(cengine)
            start = time.perf_counter()
            with cengine.begin() as connection:
                connection.execute(insert(CUser.__table__), rows)
            runs.append(time.perf_counter() - start)

        def sa_run() -> None:
            _clear_sa(sa_engine)
            start = time.perf_counter()
            with sa_engine.begin() as connection:
                connection.execute(sa.insert(SAUser.__table__), rows)
            runs.append(time.perf_counter() - start)

        runs: list[float] = []
        median = _time(runs, catalyst_run, reps, warmup)
        out.append(CaseResult("executemany_insert", str(n), "catalyst", median, runs))
        runs = []
        median = _time(runs, sa_run, reps, warmup)
        out.append(CaseResult("executemany_insert", str(n), "sqlalchemy", median, runs))
    finally:
        cengine.dispose()
        sa_engine.dispose()
    return out


# -- case: selectinload N parents x M children ------------------------------


def bench_selectinload(parents: int, children: int, reps: int, warmup: int) -> list[CaseResult]:
    out: list[CaseResult] = []
    cengine = create_engine(":memory:")
    sa_engine = sa.create_engine("sqlite:///:memory:")
    try:
        CBase.metadata.create_all(cengine)
        SABase.metadata.create_all(sa_engine)
        parent_rows = [{"id": i, "name": f"parent_{i}"} for i in range(1, parents + 1)]
        child_rows = [
            {"id": p * children + c, "name": f"child_{p}_{c}", "parent_id": p}
            for p in range(1, parents + 1)
            for c in range(children)
        ]
        with cengine.begin() as connection:
            connection.execute(insert(CParent.__table__), parent_rows)
            connection.execute(insert(CChild.__table__), child_rows)
        with sa_engine.begin() as connection:
            connection.execute(sa.insert(SAParent.__table__), parent_rows)
            connection.execute(sa.insert(SAChild.__table__), child_rows)
        expected = parents * children

        def catalyst_run() -> None:
            start = time.perf_counter()
            session = Session(cengine)
            loaded = session.scalars(select(CParent).options(selectinload(CParent.children))).all()
            total = sum(len(parent.children) for parent in loaded)
            elapsed = time.perf_counter() - start
            assert total == expected
            session.close()
            runs.append(elapsed)

        def sa_run() -> None:
            start = time.perf_counter()
            session = sa_orm.Session(sa_engine)
            loaded = session.scalars(
                sa.select(SAParent).options(sa_orm.selectinload(SAParent.children))
            ).all()
            total = sum(len(parent.children) for parent in loaded)
            elapsed = time.perf_counter() - start
            assert total == expected
            session.close()
            runs.append(elapsed)

        size = f"{parents}x{children}"
        runs: list[float] = []
        median = _time(runs, catalyst_run, reps, warmup)
        out.append(CaseResult("selectinload", size, "catalyst", median, runs))
        runs = []
        median = _time(runs, sa_run, reps, warmup)
        out.append(CaseResult("selectinload", size, "sqlalchemy", median, runs))
    finally:
        cengine.dispose()
        sa_engine.dispose()
    return out


# -- case: session.get identity-map hits -------------------------------------


def bench_session_get(calls: int, ids: int, reps: int, warmup: int) -> list[CaseResult]:
    out: list[CaseResult] = []
    cengine = create_engine(":memory:")
    sa_engine = sa.create_engine("sqlite:///:memory:")
    try:
        CBase.metadata.create_all(cengine)
        SABase.metadata.create_all(sa_engine)
        rows = [_row(index) for index in range(1, ids + 1)]
        with cengine.begin() as connection:
            connection.execute(insert(CUser.__table__), rows)
        with sa_engine.begin() as connection:
            connection.execute(sa.insert(SAUser.__table__), rows)
        sequence = [index % ids + 1 for index in range(calls)]

        def catalyst_run() -> None:
            session = Session(cengine)
            for ident in range(1, ids + 1):
                session.get(CUser, ident)
            start = time.perf_counter()
            hits = 0
            for ident in sequence:
                if session.get(CUser, ident) is not None:
                    hits += 1
            elapsed = time.perf_counter() - start
            assert hits == calls
            session.close()
            runs.append(elapsed)

        def sa_run() -> None:
            session = sa_orm.Session(sa_engine)
            for ident in range(1, ids + 1):
                session.get(SAUser, ident)
            start = time.perf_counter()
            hits = 0
            for ident in sequence:
                if session.get(SAUser, ident) is not None:
                    hits += 1
            elapsed = time.perf_counter() - start
            assert hits == calls
            session.close()
            runs.append(elapsed)

        size = f"{calls} calls / {ids} ids"
        runs: list[float] = []
        median = _time(runs, catalyst_run, reps, warmup)
        out.append(CaseResult("session_get_identity", size, "catalyst", median, runs))
        runs = []
        median = _time(runs, sa_run, reps, warmup)
        out.append(CaseResult("session_get_identity", size, "sqlalchemy", median, runs))
    finally:
        cengine.dispose()
        sa_engine.dispose()
    return out


def _fmt(seconds: float) -> str:
    if seconds < 1e-3:
        return f"{seconds * 1e6:9.1f} us"
    if seconds < 1:
        return f"{seconds * 1e3:9.2f} ms"
    return f"{seconds:9.3f} s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reps", type=int, default=7, help="timed repetitions per case")
    parser.add_argument("--warmup", type=int, default=2, help="untimed warm-up runs")
    parser.add_argument("--quick", action="store_true", help="reps=3, warmup=1")
    parser.add_argument("--case", action="append", default=[], help="run only named case(s)")
    args = parser.parse_args()
    reps, warmup = (3, 1) if args.quick else (args.reps, args.warmup)

    cases: dict[str, Callable[[], list[CaseResult]]] = {
        "insert_1k": lambda: bench_insert(1_000, reps, warmup),
        "insert_10k": lambda: bench_insert(10_000, reps, warmup),
        "select_hydrate_10k": lambda: bench_select_hydrate(10_000, reps, warmup),
        "executemany_10k": lambda: bench_executemany(10_000, reps, warmup),
        "selectinload_100x10": lambda: bench_selectinload(100, 10, reps, warmup),
        "session_get_2k": lambda: bench_session_get(2_000, 100, reps, warmup),
    }
    selected = args.case or list(cases)
    results: list[CaseResult] = []
    for name in selected:
        if name not in cases:
            print(f"unknown case {name!r}; choices: {', '.join(cases)}", file=sys.stderr)
            return 2
        results.extend(cases[name]())

    version = getattr(catalyst, "__version__", "0.1.0")
    print(
        f"# {platform.system()} {platform.release()} ({platform.machine()}), "
        f"Python {platform.python_version()}, SQLAlchemy {sa.__version__}, "
        f"Catalyst {version}"
    )
    print(f"# median of {reps} runs after {warmup} warm-up(s); speedup = sqlalchemy/catalyst")
    print(f"{'benchmark':<24}{'size':<18}{'catalyst':>14}{'sqlalchemy':>14}{'speedup':>9}")
    by_case: dict[tuple[str, str], dict[str, CaseResult]] = {}
    for result in results:
        by_case.setdefault((result.name, result.size), {})[result.impl] = result
    for (name, size), pair in by_case.items():
        cat, sal = pair["catalyst"], pair["sqlalchemy"]
        ratio = sal.seconds / cat.seconds if cat.seconds else 0.0
        print(f"{name:<24}{size:<18}{_fmt(cat.seconds):>14}{_fmt(sal.seconds):>14}{ratio:>8.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
