"""Syntax gate for every Python file under bin/, extensionless CLIs included.

CI's ``compileall`` step matches only ``*.py``, so the 18 extensionless CLI
entry points have no committed syntax gate, and two of them (``rt-claude``,
``rt-hermes``) are never executed by any test either — a syntax error there
passes every existing check and surfaces only when a user runs the command.
Recorded as F5.2 in ``handoff/archive/acceptance-m4-2026-07-29.md``.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


def _python_bin_files() -> list[Path]:
    found = []
    for path in sorted(BIN_DIR.iterdir()):
        if not path.is_file():
            continue
        if path.suffix == ".py":
            found.append(path)
            continue
        with path.open("rb") as handle:
            first_line = handle.readline()
        if first_line.startswith(b"#!") and b"python" in first_line:
            found.append(path)
    return found


def test_bin_discovery_finds_the_full_toolset() -> None:
    # If bin/ shrinks below the current 23 Python files, discovery itself
    # broke (e.g. shebang rewrite) and the per-file gate below is vacuous.
    assert len(_python_bin_files()) >= 23


@pytest.mark.parametrize(
    "path", [pytest.param(p, id=p.name) for p in _python_bin_files()]
)
def test_bin_file_compiles(path: Path, tmp_path: Path) -> None:
    py_compile.compile(
        str(path), cfile=str(tmp_path / "compiled.pyc"), doraise=True
    )
