"""Mutation checks for the Grok lease and identity fence."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import _kit as kit


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "integrations" / "grok" / "roundtable" / "__init__.py"
RUNTIME = ROOT / "bin" / "_rtruntime.py"

CONTRACT = r'''
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT))
import _rtruntime
from integrations.grok.roundtable import GrokAdapter, create_isolation

project = ROOT / "project"
project.mkdir()
runtime = ROOT / "runtime"
os.environ["RT_RUNTIME_DIR"] = str(runtime)
os.environ["RT_CODEX_RUNTIME_DIR"] = str(runtime)
lease = _rtruntime.claim(project, "grok", "grok", owner_pid=os.getpid())
try:
    executable = ROOT / "grok"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    wrong = os.environ.get("MUTATE_WRONG_IDENTITY") == "1"
    instance = GrokAdapter(
        project,
        agent_id="wrong" if wrong else "grok",
        session_id=lease.session_id,
        revision=str(lease.revision),
        isolation=create_isolation(project, runtime_root=ROOT / "grok-runtime"),
        executable=executable,
        api_key_resolver=lambda: "xai-test-key",
    )
    instance._start_client = lambda: None
    class Ready:
        class Process:
            def poll(self):
                return None
        process = Process()
    instance.client = Ready()
    instance.grok_session_id = "session"
    instance._mail_generation = lambda: ()
    instance._fence_update = lambda **_kwargs: None
    instance._clear_fence = lambda: None
    instance.stop = lambda **_kwargs: None
    if wrong:
        try:
            instance.run(once=True)
        except _rtruntime.FenceRejected:
            raise
        raise AssertionError("wrong identity was accepted")
    instance.run(once=True)
finally:
    _rtruntime.release(lease)
'''


def _private_copy(tmp_path: Path) -> Path:
    root = tmp_path / "private-copy"
    package = root / "integrations" / "grok" / "roundtable"
    package.mkdir(parents=True)
    (root / "bin").mkdir()
    shutil.copy2(SOURCE, package / "__init__.py")
    shutil.copy2(RUNTIME, root / "bin" / "_rtruntime.py")
    (root / "contract.py").write_text(CONTRACT, encoding="utf-8")
    return root


def _run_contract(root: Path, *, wrong_identity: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "bin")))
    if wrong_identity:
        environment["MUTATE_WRONG_IDENTITY"] = "1"
    else:
        environment.pop("MUTATE_WRONG_IDENTITY", None)
    return subprocess.run(
        [sys.executable, str(root / "contract.py")],
        cwd=root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def test_lease_and_identity_mutations_turn_the_private_contract_red(tmp_path):
    baseline = _private_copy(tmp_path)
    result = _run_contract(baseline)
    assert result.returncode == 0, result.stdout

    source_path = baseline / "integrations" / "grok" / "roundtable" / "__init__.py"
    source = source_path.read_text(encoding="utf-8")
    # Derived from the file under mutation rather than copied into this test:
    # a hand-written multi-line needle pins the adapter's indentation, so
    # reindenting it would fail this safety test with a confusing count error
    # instead of a real finding.
    needle = kit.call_source(source_path, "load_validated_lease")
    assert source.count(needle) == 1

    lease_bypass = _private_copy(tmp_path / "lease-bypass")
    lease_path = lease_bypass / "integrations" / "grok" / "roundtable" / "__init__.py"
    mutated = lease_path.read_text(encoding="utf-8").replace(
        needle,
        needle.replace("load_validated_lease", "print"),
        1,
    )
    lease_path.write_text(mutated, encoding="utf-8")
    result = _run_contract(lease_bypass, wrong_identity=True)
    assert result.returncode != 0, result.stdout

    identity_bypass = _private_copy(tmp_path / "identity-bypass")
    identity_path = identity_bypass / "integrations" / "grok" / "roundtable" / "__init__.py"
    mutated = identity_path.read_text(encoding="utf-8").replace(
        needle,
        needle.replace("self.agent_id,", '"grok",'),
        1,
    )
    identity_path.write_text(mutated, encoding="utf-8")
    result = _run_contract(identity_bypass, wrong_identity=True)
    assert result.returncode != 0, result.stdout
