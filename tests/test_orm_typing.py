"""PEP 681 typing parity: declarative classes type-check like dataclasses."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_VALID_USAGE = """\
from catalyst import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    nick: Mapped[str | None] = mapped_column(default=None)


u = User(id=1, name="ada", nick=None)
ident: int = u.id
label: str = u.name
alias: str | None = u.nick
u.name = "grace"
u.nick = "g"
User.id == 1
"""

_INVALID_USAGE = """\
from catalyst import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


User(id="wrong-type", name="ada")
"""


def _run_mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "probe.py"
    probe.write_text(source)
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--strict", str(probe)],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        check=False,
    )


def test_declarative_init_and_descriptors_typecheck_clean(tmp_path: Path) -> None:
    """A mapped class accepts keyword construction and typed attribute access."""
    result = _run_mypy(tmp_path, _VALID_USAGE)
    assert result.returncode == 0, result.stdout


def test_declarative_init_rejects_wrong_field_type(tmp_path: Path) -> None:
    """``dataclass_transform`` synthesizes a strictly typed ``__init__``."""
    result = _run_mypy(tmp_path, _INVALID_USAGE)
    assert result.returncode != 0
    assert "id" in result.stdout
