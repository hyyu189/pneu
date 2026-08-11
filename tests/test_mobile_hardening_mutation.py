"""Condition-level mutation guards for the D12 cwd and credential gates."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


CONTRACT = r'''
from pathlib import Path

import pytest

import _rtcodex
import _rtlauncher


def test_wrong_thread_cwd_is_rejected(tmp_path):
    project = tmp_path / "project"
    recorded = tmp_path / "recorded"
    project.mkdir()
    recorded.mkdir()
    with pytest.raises(_rtcodex.CodexRuntimeError):
        _rtcodex.require_thread_project_cwd(
            project,
            {"id": "thread-1", "cwd": str(recorded)},
            expected_thread_id="thread-1",
        )


def test_missing_hermes_credentials_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty-hermes-home"))
    monkeypatch.delenv("RT_HERMES_SKIP_AUTH_CHECK", raising=False)
    with pytest.raises(_rtlauncher.SelectionError):
        _rtlauncher.preflight_hermes_credentials([])
'''


MUTATIONS = (
    (
        "cwd-comparison-bypass",
        "_rtcodex.py",
        "if thread_cwd != seat_project:",
        "if False:",
    ),
    (
        "hermes-presence-bypass",
        "_rtlauncher.py",
        "if any(path.is_file() for path in candidates):",
        "if True:",
    ),
)


def _private_copy(root: Path) -> Path:
    shutil.copytree(
        ROOT / "bin",
        root / "bin",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (root / "contract.py").write_text(CONTRACT, encoding="utf-8")
    return root


def _run_contract(root: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root / "bin")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(root / "contract.py")],
        cwd=root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_mobile_hardening_mutations_turn_the_private_contract_red(tmp_path):
    baseline = _private_copy(tmp_path / "baseline")
    result = _run_contract(baseline)
    assert result.returncode == 0, result.stdout

    for slug, filename, needle, replacement in MUTATIONS:
        root = _private_copy(tmp_path / slug)
        path = root / "bin" / filename
        source = path.read_text(encoding="utf-8")
        assert source.count(needle) == 1
        path.write_text(source.replace(needle, replacement, 1), encoding="utf-8")
        result = _run_contract(root)
        assert result.returncode != 0, f"mutation {slug} survived:\n{result.stdout}"
