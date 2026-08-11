"""Condition-level mutation guards for the D11 mobile-fleet safety gates.

Each mutation runs against a private source copy. The checkout remains
untouched, and the focused contract must turn red when one load-bearing
condition is bypassed.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


CONTRACT = r'''
from __future__ import annotations

import importlib.machinery
import importlib.util
from contextlib import contextmanager
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

import _rtrchost


BIN = Path(__file__).resolve().parent / "bin"


def load_script(filename: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / filename))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


rc_cli = load_script("rt-rc-host", "mobile_mutation_rc_cli")
wait = load_script("rt-wait-inbox", "mobile_mutation_wait")
pneu = load_script("pneu", "mobile_mutation_pneu")


def test_workspace_trust_gate_stops_before_git_preflight(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        _rtrchost,
        "_canonical_project",
        lambda _project: (
            project,
            "00000000-0000-0000-0000-000000000001",
            {"name": "project", "group": "group"},
        ),
    )
    monkeypatch.setattr(
        _rtrchost, "workspace_trusted", lambda _project, home=None: False
    )

    with pytest.raises(_rtrchost.RCHostError, match="workspace trust"):
        _rtrchost.enable(project, home=tmp_path / "home")


def test_adoption_does_not_reuse_a_different_live_session(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    env_file = tmp_path / "claude-env"
    env_file.write_text("")
    token = SimpleNamespace(
        harness="claude",
        session_id="live-other-session",
        owner_pid=777,
        project_root=project,
        agent_id="claude",
        revision=1,
    )
    monkeypatch.setattr(wait, "_project_at_or_above", lambda _cwd: project)

    @contextmanager
    def locked_project(_project, timeout):
        yield SimpleNamespace(project_root=project)

    monkeypatch.setattr(wait, "locked_project_mailbox_checked", locked_project)
    monkeypatch.setattr(wait, "claude_seat", lambda _project: "claude")
    monkeypatch.setattr(
        wait,
        "inspect_seat",
        lambda _project, _agent: SimpleNamespace(
            status="active_healthy", token=token
        ),
    )
    monkeypatch.setattr(wait, "record_registration", lambda *_args, **_kwargs: None)
    environment = {"CLAUDE_ENV_FILE": str(env_file)}

    adopted = wait._adopt_unleased_claude_session(
        {
            "hook_event_name": "SessionStart",
            "source": "resume",
            "session_id": "incoming-session",
            "cwd": str(project),
        },
        environment=environment,
        owner_pid=777,
    )

    assert adopted is None
    assert "RT_SESSION_ID" not in environment


def test_worktree_create_never_succeeds_without_one_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(rc_cli, "_payload_project", lambda *_args: project)
    monkeypatch.setattr(
        rc_cli, "require_enabled", lambda _project: {"projectRoot": str(project)}
    )
    monkeypatch.setattr(
        rc_cli,
        "_run_worktree",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    result = rc_cli.hook_create(
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "WorktreeCreate",
                    "cwd": str(project),
                    "name": "phone-tree",
                }
            )
        ),
        stdout,
        stderr,
    )

    assert result == 2
    assert stdout.getvalue() == ""
    assert "no unique worktree path" in stderr.getvalue()


def test_card_requires_both_terminal_streams():
    class TTYInput(io.StringIO):
        def isatty(self):
            return True

    assert pneu._rich_card_available(TTYInput(), io.StringIO()) is False
'''


MUTATIONS = (
    (
        "trust-bypass",
        "_rtrchost.py",
        "if not workspace_trusted(root, home=selected_home):",
        "if False:",
    ),
    (
        "adoption-session-bypass",
        "rt-wait-inbox",
        "or token.session_id != session_id",
        "or False",
    ),
    (
        "empty-hook-output-bypass",
        "rt-rc-host",
        "if len(lines) != 1:",
        "if False:",
    ),
    (
        "card-fallback-bypass",
        "pneu",
        "return bool(stdin.isatty() and stderr.isatty())",
        "return True",
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


def test_mobile_fleet_guard_mutations_turn_the_private_contract_red(tmp_path):
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
