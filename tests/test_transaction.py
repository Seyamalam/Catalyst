"""SQLite transaction and savepoint parity against SQLAlchemy."""

import tempfile
from pathlib import Path

import pytest
from catalyst import InvalidRequestError
from catalyst import create_engine as catalyst_create_engine
from catalyst import text as catalyst_text
from sqlalchemy import create_engine, text
from sqlalchemy.exc import InvalidRequestError as SAInvalidRequestError


def _insert_rows(engine: object) -> list[tuple[int]]:
    with engine.connect() as connection:  # type: ignore[attr-defined]
        connection.execute(text("CREATE TABLE IF NOT EXISTS items (value INTEGER)"))
        connection.commit()
        connection.execute(text("INSERT INTO items VALUES (1)"))
        connection.commit()
        return connection.execute(text("SELECT value FROM items ORDER BY value")).all()


def test_root_commit_persists_and_rollback_discards() -> None:
    catalyst_engine = catalyst_create_engine(":memory:")
    sa_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sa_engine, text),
    ):
        with engine.connect() as connection:
            with connection.begin():
                connection.execute(statement("CREATE TABLE items (value INTEGER)"))
                connection.execute(statement("INSERT INTO items VALUES (1)"))
            with connection.begin():
                connection.execute(statement("INSERT INTO items VALUES (2)"))
                connection.rollback()
            assert connection.execute(statement("SELECT value FROM items")).all() == [(1,)]
    catalyst_engine.dispose()
    sa_engine.dispose()


def test_engine_begin_commits_and_rolls_back_on_exception() -> None:
    catalyst_engine = catalyst_create_engine(":memory:")
    sa_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sa_engine, text),
    ):
        with engine.begin() as connection:
            connection.execute(statement("CREATE TABLE items (value INTEGER)"))
        with pytest.raises(ValueError):
            with engine.begin() as connection:
                connection.execute(statement("INSERT INTO items VALUES (1)"))
                raise ValueError("rollback")
        with engine.connect() as connection:
            assert connection.execute(statement("SELECT COUNT(*) FROM items")).fetchone() == (0,)
    catalyst_engine.dispose()
    sa_engine.dispose()


def test_nested_rollback_preserves_outer_work_and_outer_rollback_discards_all() -> None:
    catalyst_engine = catalyst_create_engine(":memory:")
    sa_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sa_engine, text),
    ):
        with engine.begin() as connection:
            connection.execute(statement("CREATE TABLE items (value INTEGER)"))
            connection.execute(statement("INSERT INTO items VALUES (1)"))
            with connection.begin_nested():
                connection.execute(statement("INSERT INTO items VALUES (2)"))
            nested = connection.begin_nested()
            connection.execute(statement("INSERT INTO items VALUES (3)"))
            nested.rollback()
            actual = connection.execute(statement("SELECT value FROM items ORDER BY value")).all()
            assert actual == [(1,), (2,)]
        with engine.begin() as connection:
            connection.execute(statement("INSERT INTO items VALUES (4)"))
            connection.rollback()
        with engine.connect() as connection:
            actual = connection.execute(statement("SELECT value FROM items ORDER BY value")).all()
            assert actual == [(1,), (2,)]
    catalyst_engine.dispose()
    sa_engine.dispose()


def test_second_root_begin_matches_invalid_request_error_name() -> None:
    catalyst_engine = catalyst_create_engine(":memory:")
    sa_engine = create_engine("sqlite://")
    for engine, error in (
        (catalyst_engine, InvalidRequestError),
        (sa_engine, SAInvalidRequestError),
    ):
        with engine.connect() as connection:
            transaction = connection.begin()
            with pytest.raises(error) as raised:
                connection.begin()
            assert type(raised.value).__name__ == SAInvalidRequestError.__name__
            transaction.rollback()
    catalyst_engine.dispose()
    sa_engine.dispose()


def test_close_rolls_back_open_transaction_and_transactional_ddl() -> None:
    with tempfile.TemporaryDirectory() as directory:
        for index, (create, statement) in enumerate(
            ((catalyst_create_engine, catalyst_text), (create_engine, text))
        ):
            path = Path(directory) / f"engine_{index}.sqlite"
            engine = create_engine(f"sqlite:///{path}") if create is create_engine else create(path)
            connection = engine.connect()
            if create is catalyst_create_engine:
                connection.begin()
            else:
                connection.exec_driver_sql("BEGIN")
            connection.execute(statement("CREATE TABLE items (value INTEGER)"))
            connection.execute(statement("INSERT INTO items VALUES (1)"))
            connection.close()
            with engine.connect() as check:
                assert (
                    check.execute(
                        statement("SELECT name FROM sqlite_master WHERE name = 'items'")
                    ).fetchall()
                    == []
                )
            engine.dispose()


def test_rows_rowcount_and_keys_are_unchanged_inside_transaction() -> None:
    catalyst_engine = catalyst_create_engine(":memory:")
    sa_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sa_engine, text),
    ):
        with engine.begin() as connection:
            connection.execute(statement("CREATE TABLE items (value INTEGER)"))
            inserted = connection.execute(statement("INSERT INTO items VALUES (7)"))
            selected = connection.execute(statement("SELECT value AS item FROM items"))
            assert inserted.rowcount == 1
            keys = selected.keys
            if callable(keys):
                keys = keys()
            assert tuple(keys) == ("item",)
            assert selected.fetchone() == (7,)
    catalyst_engine.dispose()
    sa_engine.dispose()


def test_dropped_connection_resets_open_transaction_on_pool_return() -> None:
    """A checkout dropped without close() is rolled back, like SQLAlchemy's pool."""
    import gc

    with tempfile.TemporaryDirectory() as directory:
        engine = catalyst_create_engine(Path(directory) / "reset.sqlite")
        with engine.connect() as setup:
            setup.execute("CREATE TABLE items (value INTEGER)")
            setup.commit()
        dirty = engine.connect()
        dirty.execute("INSERT INTO items VALUES (1)")
        # Simulate a holder that never calls close(): the pooled connection is
        # returned mid-transaction and must be reset on return, not poisoned.
        del dirty
        gc.collect()

        fresh = engine.connect()
        try:
            assert fresh.execute("SELECT COUNT(*) FROM items").fetchone() == (0,)
            fresh.execute("INSERT INTO items VALUES (2)")
        finally:
            fresh.close()
        engine.dispose()


def test_transaction_handle_commit_after_connection_commit_raises_like_sqlalchemy() -> None:
    with catalyst_create_engine(":memory:") as engine:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.commit()
            with pytest.raises(InvalidRequestError) as catalyst_error:
                transaction.commit()
    sqlalchemy_engine = create_engine("sqlite://")
    with sqlalchemy_engine.connect() as connection:
        transaction = connection.begin()
        connection.commit()
        with pytest.raises(SAInvalidRequestError):
            transaction.commit()
    sqlalchemy_engine.dispose()
    assert type(catalyst_error.value).__name__ == type(SAInvalidRequestError("x")).__name__


def test_ddl_rowcount_is_minus_one_like_sqlalchemy() -> None:
    """DDL reports rowcount -1 on both; zero-change DML reports 0 on both."""
    with catalyst_create_engine(":memory:").connect() as connection:
        created = connection.execute("CREATE TABLE items (value INTEGER)")
        deleted = connection.execute("DELETE FROM items WHERE value = 99")
        actual = (created.rowcount, deleted.rowcount)
    with create_engine("sqlite://").connect() as connection:
        created = connection.execute(text("CREATE TABLE items (value INTEGER)"))
        deleted = connection.execute(text("DELETE FROM items WHERE value = 99"))
        expected = (created.rowcount, deleted.rowcount)
    assert actual == expected == (-1, 0)


def test_native_rejects_foreign_savepoint_names() -> None:
    """Savepoint statements cannot be parameterized; names are validated natively."""
    engine = catalyst_create_engine(":memory:")
    with engine.connect() as connection:
        with pytest.raises(InvalidRequestError):
            connection._native.release_savepoint("not_a_real_savepoint")
    engine.dispose()


def test_memory_shared_handle_transaction_interleave_matches_sqlalchemy() -> None:
    """:memory: checkouts serialize on one wire and share transaction boundaries."""
    catalyst_engine = catalyst_create_engine(":memory:")
    sqlalchemy_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sqlalchemy_engine, text),
    ):
        with engine.connect() as setup:
            setup.execute(statement("CREATE TABLE items (value INTEGER)"))
            setup.commit()

        # A sibling execute joins an open transaction; sibling commit commits
        # work done through both logical handles.
        with engine.connect() as outer:
            outer.begin()
            outer.execute(statement("INSERT INTO items VALUES (1)"))
            with engine.connect() as sibling:
                sibling.execute(statement("INSERT INTO items VALUES (2)"))
                sibling.commit()
            outer.execute(statement("INSERT INTO items VALUES (3)"))
            outer.commit()
        with engine.connect() as check:
            assert check.execute(statement("SELECT value FROM items ORDER BY value")).all() == [
                (1,),
                (2,),
                (3,),
            ]
            check.rollback()

        # A sibling rollback also ends the shared root transaction, including
        # writes made by the outer handle.
        with engine.connect() as outer:
            outer.begin()
            outer.execute(statement("INSERT INTO items VALUES (4)"))
            with engine.connect() as sibling:
                sibling.execute(statement("INSERT INTO items VALUES (5)"))
                sibling.rollback()
            outer.rollback()
        with engine.connect() as check:
            assert check.execute(statement("SELECT value FROM items ORDER BY value")).all() == [
                (1,),
                (2,),
                (3,),
            ]
            check.rollback()

        # Savepoint bookkeeping is physical-slot state too: a savepoint opened
        # through one wrapper can be rolled back through its nested handle.
        with engine.connect() as outer:
            outer.begin()
            outer.execute(statement("INSERT INTO items VALUES (6)"))
            with engine.connect() as sibling:
                nested = sibling.begin_nested()
                sibling.execute(statement("INSERT INTO items VALUES (7)"))
                nested.rollback()
                outer.commit()
        with engine.connect() as check:
            assert check.execute(statement("SELECT value FROM items ORDER BY value")).all() == [
                (1,),
                (2,),
                (3,),
                (6,),
            ]
            check.rollback()

    catalyst_engine.dispose()
    sqlalchemy_engine.dispose()


def test_file_engine_checkouts_keep_transactions_independent(tmp_path: Path) -> None:
    path = tmp_path / "independent.sqlite"
    catalyst_engine = catalyst_create_engine(path, pool_size=2, max_overflow=0)
    sqlalchemy_engine = create_engine(f"sqlite:///{tmp_path / 'sa-independent.sqlite'}")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sqlalchemy_engine, text),
    ):
        with engine.connect() as setup:
            setup.execute(statement("CREATE TABLE items (value INTEGER)"))
            setup.commit()
        with engine.connect() as outer:
            outer.execute(statement("INSERT INTO items VALUES (1)"))
            with engine.connect() as sibling:
                assert sibling.execute(statement("SELECT COUNT(*) FROM items")).fetchone() == (0,)
                sibling.rollback()
            outer.rollback()
    catalyst_engine.dispose()
    sqlalchemy_engine.dispose()


def test_fresh_holder_operations_do_not_end_the_shared_transaction() -> None:
    """A holder with no own transaction is a no-op, like SQLAlchemy.

    On the shared ``:memory:`` wire, SQLAlchemy only lets a holder that has its
    own transaction (explicit or autobegin) touch commit/rollback/close.
    """
    catalyst_engine = catalyst_create_engine(":memory:")
    sqlalchemy_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sqlalchemy_engine, text),
    ):
        with engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(statement("CREATE TABLE items (value INTEGER)"))
            holder.execute(statement("INSERT INTO items VALUES (1)"))
            # Fresh holder operations must not end the shared transaction.
            fresh = engine.connect()
            fresh.commit()
            fresh.rollback()
            fresh.close()
            assert holder.execute(statement("SELECT COUNT(*) FROM items")).fetchone() == (1,)
            holder.commit()
            assert holder.execute(statement("SELECT COUNT(*) FROM items")).fetchone() == (1,)
            transaction = None  # noqa: F841 - transaction already ended by holder commit
    catalyst_engine.dispose()
    sqlalchemy_engine.dispose()


def test_holder_begin_on_busy_shared_wire_joins_and_close_rolls_back() -> None:
    """begin() on a busy shared wire joins; its close rolls the shared wire back."""
    catalyst_engine = catalyst_create_engine(":memory:")
    sqlalchemy_engine = create_engine("sqlite://")
    for engine, statement in (
        (catalyst_engine, catalyst_text),
        (sqlalchemy_engine, text),
    ):
        with engine.connect() as setup:
            setup.execute(statement("CREATE TABLE items (value INTEGER)"))
            setup.commit()
        with engine.connect() as holder:
            transaction = holder.begin()
            holder.execute(statement("INSERT INTO items VALUES (1)"))
            joiner = engine.connect()
            joiner.begin()  # joins the busy shared wire, like SQLAlchemy
            joiner.close()  # holder with a transaction: close rolls the wire back
            assert holder.execute(statement("SELECT COUNT(*) FROM items")).fetchone() == (0,)
            transaction = None  # noqa: F841 - transaction ended by the joiner's close
    catalyst_engine.dispose()
    sqlalchemy_engine.dispose()
