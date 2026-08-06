"""Mutation guard for the OpenClaw seat fence.

The mutation run is deliberately performed against a temporary source copy.
It never changes the checkout and proves that removing either the lease
validation or the agent identity check makes a focused contract test fail.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations" / "openclaw" / "roundtable" / "__init__.py"
RUNTIME = ROOT / "bin" / "_rtruntime.py"


CONTRACT = r'''
from pathlib import Path
import os

import pytest

import _rtruntime
from integrations.openclaw.roundtable import OpenClawAdapter, create_isolation


def test_fenced_start_rejects_invalid_identity(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    lease = _rtruntime.claim(project, "openclaw", "openclaw", owner_pid=os.getpid())
    try:
        executable = tmp_path / "openclaw"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        identity = "wrong" if os.environ.get("MUTATE_WRONG_IDENTITY") else "openclaw"
        session = "stale-session" if not os.environ.get("MUTATE_WRONG_IDENTITY") else lease.session_id
        instance = OpenClawAdapter(
            project,
            agent_id=identity,
            session_id=session,
            revision=str(lease.revision),
            isolation=create_isolation(project, runtime_root=tmp_path / "openclaw-runtime"),
            executable=executable,
        )
        instance.start_gateway = lambda: None
        instance.wait_ready = lambda *_args, **_kwargs: None
        instance._fence_update = lambda **_kwargs: None
        instance._mail_generation = lambda: ()
        instance.stop_gateway = lambda: None
        with pytest.raises(_rtruntime.FenceRejected):
            instance.run(once=True)
    finally:
        _rtruntime.release(lease)
'''


def _run_contract(root: Path, *, wrong_identity: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "bin")))
    if wrong_identity:
        environment["MUTATE_WRONG_IDENTITY"] = "1"
    else:
        environment.pop("MUTATE_WRONG_IDENTITY", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(root / "contract.py")],
        cwd=root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _private_copy(tmp_path: Path) -> Path:
    root = tmp_path / "private-copy"
    package = root / "integrations" / "openclaw" / "roundtable"
    package.mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy2(SOURCE, package / "__init__.py")
    shutil.copy2(RUNTIME, root / "bin" / "_rtruntime.py")
    shutil.copy2(ROOT / "bin" / "rt-inbox", root / "bin" / "rt-inbox")
    (root / "contract.py").write_text(CONTRACT, encoding="utf-8")
    return root


def test_lease_and_identity_mutations_turn_the_private_contract_red(tmp_path):
    baseline = _private_copy(tmp_path)
    result = _run_contract(baseline)
    assert result.returncode == 0, result.stdout

    source_path = baseline / "integrations" / "openclaw" / "roundtable" / "__init__.py"
    source = source_path.read_text(encoding="utf-8")
    needle = """load_validated_lease(
                    self.project_root,
                    self.agent_id,
                    self.session_id,
                    self.revision,
                )"""
    assert source.count(needle) == 1

    lease_bypass = _private_copy(tmp_path / "lease-bypass")
    lease_path = lease_bypass / "integrations" / "openclaw" / "roundtable" / "__init__.py"
    mutated = lease_path.read_text(encoding="utf-8").replace(needle, needle.replace("load_validated_lease", "print"), 1)
    lease_path.write_text(mutated, encoding="utf-8")
    result = _run_contract(lease_bypass)
    assert result.returncode != 0, result.stdout

    identity_bypass = _private_copy(tmp_path / "identity-bypass")
    identity_path = identity_bypass / "integrations" / "openclaw" / "roundtable" / "__init__.py"
    mutated = identity_path.read_text(encoding="utf-8").replace(
        needle,
        needle.replace("self.agent_id,", '"openclaw",'),
        1,
    )
    identity_path.write_text(mutated, encoding="utf-8")
    result = _run_contract(identity_bypass, wrong_identity=True)
    assert result.returncode != 0, result.stdout
