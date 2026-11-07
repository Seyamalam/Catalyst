"""Engine URL, option, and pool parity against SQLAlchemy 2.1."""

import importlib.util
import logging
import tempfile
import threading
import time
from pathlib import Path

import pytest
import sqlalchemy
from catalyst import ArgumentError, NoSuchModuleError, TimeoutError
from catalyst import create_engine as catalyst_create_engine
from sqlalchemy import create_engine as sa_create_engine


def _class_name(callable_: object, *args: object, **kwargs: object) -> str:
    try:
        callable_(*args, **kwargs)  # type: ignore[operator]
    except Exception as error:
        return type(error).__name__
    return "success"


@pytest.mark.parametrize(
    ("url", "expected_database"),
    [
        ("sqlite://", ":memory:"),
        ("sqlite:///:memory:", ":memory:"),
        ("sqlite+pysqlite:///:memory:", ":memory:"),
        ("sqlite:///:memory:?pool_size=3", ":memory:"),
        ("sqlite:///relative/path", "relative/path"),
        ("sqlite+pysqlite:///relative/path", "relative/path"),
        ("sqlite:////absolute/path", "/absolute/path"),
        ("sqlite+pysqlite:////absolute/path", "/absolute/path"),
    ],
)
def test_sqlite_url_paths_match_sqlalchemy(url: str, expected_database: str) -> None:
    catalyst = catalyst_create_engine(url)
    sqlalchemy_engine = sa_create_engine(url)
    assert catalyst._native is not None
    if sqlalchemy_engine.url.database is None:
        with catalyst.connect() as connection:
            assert connection.execute("SELECT 1").fetchone() == (1,)
    else:
        assert sqlalchemy_engine.url.database == expected_database
    catalyst.dispose()
    sqlalchemy_engine.dispose()


def test_relative_url_path_reaches_the_filesystem(tmp_path: Path) -> None:
    """Parsed relative paths actually open the target file (guards lazy opens)."""
    import os

    target = tmp_path / "relative.sqlite"
    previous = Path.cwd()
    os.chdir(tmp_path)
    try:
        engine = catalyst_create_engine("sqlite:///relative.sqlite")
        with engine.connect() as connection:
            connection.execute("CREATE TABLE marker (value INTEGER)")
        engine.dispose()
    finally:
        os.chdir(previous)
    assert target.is_file()


def test_url_query_options_are_ignored_with_warning_like_sqlalchemy() -> None:
    """SQLAlchemy's pysqlite dialect warns and ignores URL query arguments."""
    with pytest.warns(RuntimeWarning, match="Ignoring URL query"):
        engine = catalyst_create_engine("sqlite:///:memory:?pool_size=2&timeout=4")
    engine.dispose()
    with tempfile.TemporaryDirectory() as directory:
        url = f"sqlite:///{Path(directory) / 'query.sqlite'}?pool_size=2&timeout=4"
        with pytest.warns(RuntimeWarning):
            engine = catalyst_create_engine(url)
        engine.dispose()


def test_nonfinite_timeouts_are_rejected() -> None:
    """Non-finite timeouts cannot map to a native duration; SA accepts inf."""
    with pytest.raises(ArgumentError):
        catalyst_create_engine(":memory:", pool_timeout=float("inf"))
    with pytest.raises(ArgumentError):
        catalyst_create_engine(":memory:", timeout=float("inf"))


def test_string_boolean_options_coerce_like_sqlalchemy() -> None:
    assert catalyst_create_engine(":memory:", echo="false")._echo is False
    assert catalyst_create_engine(":memory:", echo="true")._echo is True


def test_future_rejects_only_falsy_values_like_sqlalchemy() -> None:
    assert _class_name(catalyst_create_engine, ":memory:", future=False) == "ArgumentError"
    assert _class_name(sa_create_engine, "sqlite://", future=False) == "ArgumentError"
    assert _class_name(catalyst_create_engine, ":memory:", future="true") == "success"
    assert _class_name(sa_create_engine, "sqlite://", future="true") == "success"


def test_unparseable_url_matches_argument_error_name() -> None:
    actual = _class_name(catalyst_create_engine, "sqlite://bad host/db")
    expected = _class_name(sa_create_engine, "sqlite://bad host/db")
    assert actual == expected == ArgumentError.__name__


def test_supported_pool_options_and_future_true() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "pool.sqlite"
        engine = catalyst_create_engine(
            path, pool_size=2, max_overflow=1, pool_timeout=0, future=True
        )
        assert engine.connect()
        engine.dispose()
        sqlalchemy_engine = sa_create_engine(
            f"sqlite:///{path}", pool_size=2, max_overflow=1, pool_timeout=0, future=True
        )
        assert sqlalchemy_engine.pool.size() == 2
        sqlalchemy_engine.dispose()


@pytest.mark.parametrize("options", [{"future": False}])
def test_invalid_options_match_exception_class_name(options: dict[str, object]) -> None:
    actual = _class_name(catalyst_create_engine, ":memory:", **options)
    expected = _class_name(sa_create_engine, "sqlite://", **options)
    assert actual == expected == ArgumentError.__name__


def test_unknown_option_matches_sqlalchemy_type_error() -> None:
    actual = _class_name(catalyst_create_engine, ":memory:", foo=1)
    expected = _class_name(sa_create_engine, "sqlite://", foo=1)
    assert actual == expected == TypeError.__name__


def test_pool_overflow_then_timeout_matches_sqlalchemy() -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "overflow.sqlite"
        catalyst = catalyst_create_engine(path, pool_size=1, max_overflow=1, pool_timeout=0)
        sa = sa_create_engine(f"sqlite:///{path}", pool_size=1, max_overflow=1, pool_timeout=0)
        catalyst_connections = [catalyst.connect(), catalyst.connect()]
        sa_connections = [sa.connect(), sa.connect()]
        with pytest.raises(TimeoutError) as catalyst_error:
            catalyst.connect()
        with pytest.raises(Exception) as sa_error:
            sa.connect()
        assert type(catalyst_error.value).__name__ == type(sa_error.value).__name__
        assert type(catalyst_error.value).__name__ == sqlalchemy.exc.TimeoutError.__name__
        for connection in catalyst_connections:
            connection.close()
        for connection in sa_connections:
            connection.close()
        catalyst.dispose()
        sa.dispose()


def test_pool_checkout_waits_until_timeout_and_releases_waiters() -> None:
    with tempfile.TemporaryDirectory() as directory:
        engine = catalyst_create_engine(
            Path(directory) / "wait.sqlite", pool_size=1, max_overflow=0, pool_timeout=0.08
        )
        held = engine.connect()
        outcome: list[object] = []

        def checkout() -> None:
            started = time.monotonic()
            try:
                outcome.append(engine.connect())
            except Exception as error:
                outcome.extend([error, time.monotonic() - started])

        thread = threading.Thread(target=checkout)
        thread.start()
        thread.join(timeout=1)
        assert not thread.is_alive()
        error, elapsed = outcome
        assert type(error).__name__ == sqlalchemy.exc.TimeoutError.__name__
        assert isinstance(error, TimeoutError)
        assert float(elapsed) >= 0.05
        held.close()
        released = engine.connect()
        released.close()
        engine.dispose()


def test_echo_logs_sql_at_info(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="catalyst.engine"):
        with catalyst_create_engine(":memory:", echo=True) as engine:
            with engine.connect() as connection:
                connection.execute("SELECT 1")
    assert any(
        record.name == "catalyst.engine" and "SELECT 1" in record.message
        for record in caplog.records
    )


_DIALECT_DRIVER_MODULES = {
    "postgresql+psycopg": "psycopg",
    "mysql": "MySQLdb",
    "oracle": "oracledb",
}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql+psycopg://user@localhost/db", "ModuleNotFoundError"),
        ("postgresql+unknown://localhost/db", "NoSuchModuleError"),
        ("mysql://localhost/db", "ModuleNotFoundError"),
        ("oracle://localhost/db", "ModuleNotFoundError"),
    ],
)
def test_dialect_errors_match_sqlalchemy_class_names(url: str, expected: str) -> None:
    module = _DIALECT_DRIVER_MODULES.get(url.split("://")[0])
    if module is not None and importlib.util.find_spec(module) is not None:
        pytest.skip(f"driver module {module!r} is installed; SA would import it successfully")
    actual = _class_name(catalyst_create_engine, url)
    sqlalchemy_class = _class_name(sa_create_engine, url)
    assert actual == expected
    assert sqlalchemy_class == expected
    if expected == NoSuchModuleError.__name__:
        assert actual == NoSuchModuleError.__name__
