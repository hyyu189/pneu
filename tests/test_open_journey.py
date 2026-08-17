from __future__ import annotations

import hashlib
import importlib.util
from importlib.machinery import SourceFileLoader
import io
import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
TOOL = BIN / "rt-worktree"
sys.path.insert(0, str(BIN))

from _rtsurface import launcher_shell_command  # noqa: E402
from _rtruntime import claim, release, seat_paths  # noqa: E402


def _load_worktree_module():
    loader = SourceFileLoader("rt_worktree_open_journey", str(TOOL))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


WORKTREE = _load_worktree_module()
ORIGINAL_SEAT_LAUNCHER = WORKTREE._seat_launcher


def test_openclaw_is_not_a_worktree_seat_launcher():
    with pytest.raises(
        WORKTREE.WorktreeError,
        match="no user-facing pneu seat launcher",
    ):
        WORKTREE._seat_launcher("openclaw")


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


@pytest.fixture
def journey_lab(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.name", "pneu journey tests")
    _git(repository, "config", "user.email", "pneu@example.invalid")
    (repository / "README.md").write_text("journey fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-qm", "fixture")

    registry = tmp_path / "state" / "projects.yaml"
    runtime = tmp_path / "runtime"
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("ROUNDTABLE_ONBOARDING_SUBPROCESS", "1")

    created = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "add",
            "demo",
            "--repo",
            str(repository),
            "--yes",
        ],
        cwd=repository,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    monkeypatch.setenv("ROUNDTABLE_INSTALL_PREFIX", str(state_root))
    target = tmp_path / "repo-worktree" / "demo"
    (target / ".roundtable" / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        "project: .\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n",
        encoding="utf-8",
    )
    return repository, target, runtime, state_root


def _fake_herdr(tmp_path: Path, target: Path, monkeypatch) -> Path:
    fake_bin = tmp_path / "fake-herdr-bin"
    fake_bin.mkdir()
    executable = fake_bin / "herdr"
    pid_file = tmp_path / "launcher.pid"
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
if args[:2] == ["pane", "layout"]:
    print(json.dumps({{"result": {{"layout": {{"width": 160, "height": 40}}}}}}))
elif args[:2] == ["pane", "split"]:
    print(json.dumps({{"result": {{"pane": {{"pane_id": "w1:p9"}}}}}}))
elif args[:2] == ["pane", "run"]:
    process = subprocess.Popen(
        args[3],
        cwd=os.environ["RT_TEST_TARGET"],
        env=os.environ.copy(),
        shell=True,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(os.environ["RT_TEST_LAUNCHER_PID"]).write_text(str(process.pid))
    print("{{}}")
else:
    raise SystemExit(8)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    monkeypatch.setenv(
        "PATH",
        str(fake_bin) + os.pathsep + os.environ.get("PATH", os.defpath),
    )
    monkeypatch.setenv("HERDR_ENV", "1")
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p1")
    monkeypatch.setenv("RT_TEST_TARGET", str(target))
    monkeypatch.setenv("RT_TEST_LAUNCHER_PID", str(pid_file))
    return pid_file


def _source_launcher_layout(tmp_path: Path, body: str) -> tuple[Path, Path]:
    source_bin = tmp_path / "source" / "bin"
    source_bin.mkdir(parents=True)
    tool = source_bin / "rt-worktree"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    launcher = source_bin / "rt-codex"
    launcher.write_text(body, encoding="utf-8")
    launcher.chmod(0o755)
    return tool, launcher


def _open_with_fake_launcher(monkeypatch, tool: Path, repository: Path):
    monkeypatch.setattr(
        WORKTREE,
        "_seat_launcher",
        lambda harness: ORIGINAL_SEAT_LAUNCHER(
            harness,
            tool_path=tool,
            environ=os.environ,
        ),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    code = WORKTREE.main(
        ["open", "demo", "--seat", "codex", "--surface", "herdr"],
        cwd=repository,
        stdout=stdout,
        stderr=stderr,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def _stop_launcher(pid_file: Path) -> None:
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text(encoding="utf-8"))
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)


def test_open_journey_waits_for_claim_then_records_surface(
    journey_lab,
    tmp_path,
    monkeypatch,
):
    repository, target, runtime, _state_root = journey_lab
    pid_file = _fake_herdr(tmp_path, target, monkeypatch)
    tool, _launcher = _source_launcher_layout(
        tmp_path,
        f"""#!{sys.executable}
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, {str(BIN)!r})
from _rtruntime import claim

claim(Path.cwd(), os.environ["RT_FROM"], "codex")
time.sleep(5)
""",
    )
    monkeypatch.setenv("RT_WORKTREE_OPEN_TIMEOUT", "2")

    try:
        code, stdout, stderr = _open_with_fake_launcher(
            monkeypatch,
            tool,
            repository,
        )
        assert code == 0, stderr
        assert "worktree opened:" in stdout
        assert "activation: active_unhealthy" in stdout
        surface_path = seat_paths(target, "codex", root=runtime).surface
        payload = json.loads(surface_path.read_text(encoding="utf-8"))
        assert payload["surface"] == {"kind": "herdr", "pane": "w1:p9"}
    finally:
        _stop_launcher(pid_file)


def test_open_journey_launcher_exit_times_out_without_surface(
    journey_lab,
    tmp_path,
    monkeypatch,
):
    repository, target, runtime, _state_root = journey_lab
    _fake_herdr(tmp_path, target, monkeypatch)
    tool, launcher = _source_launcher_layout(
        tmp_path,
        "#!/bin/sh\nexit 0\n",
    )
    monkeypatch.setenv("RT_WORKTREE_OPEN_TIMEOUT", "0.2")

    code, _stdout, stderr = _open_with_fake_launcher(
        monkeypatch,
        tool,
        repository,
    )

    assert code == 2
    assert "seat activation timed out after 0.2s" in stderr
    assert "surface: herdr:w1:p9" in stderr
    assert "command: " in stderr
    assert str(launcher) in stderr
    assert "last seat state: vacant" in stderr
    assert "advisory record: not written" in stderr
    assert not seat_paths(target, "codex", root=runtime).surface.exists()


def test_open_journey_refuses_an_already_active_seat_before_spawning(
    journey_lab,
    tmp_path,
    monkeypatch,
):
    repository, target, runtime, _state_root = journey_lab
    _fake_herdr(tmp_path, target, monkeypatch)
    tool, _launcher = _source_launcher_layout(
        tmp_path,
        "#!/bin/sh\nexit 0\n",
    )
    token = claim(target, "codex", "codex")

    try:
        code, _stdout, stderr = _open_with_fake_launcher(
            monkeypatch,
            tool,
            repository,
        )
        assert code == 2
        assert "seat 'codex' is already active" in stderr
        assert not (tmp_path / "launcher.pid").exists()
        assert not seat_paths(target, "codex", root=runtime).surface.exists()
    finally:
        assert release(token)


def test_source_launcher_command_injects_explicit_state_and_runtime(
    tmp_path,
    monkeypatch,
):
    state_root = tmp_path / "state root"
    runtime = tmp_path / "runtime root"
    state_root.mkdir()
    monkeypatch.setenv("ROUNDTABLE_INSTALL_PREFIX", str(state_root))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    tool, launcher = _source_launcher_layout(
        tmp_path,
        "#!/bin/sh\nexit 0\n",
    )

    resolved = ORIGINAL_SEAT_LAUNCHER(
        "codex",
        tool_path=tool,
        environ=os.environ,
    )
    command = launcher_shell_command(
        resolved.path,
        "codex",
        environment=dict(resolved.environment),
    )

    assert resolved.path == launcher.resolve()
    tokens = shlex.split(command)
    assert f"ROUNDTABLE_INSTALL_PREFIX={state_root}" in tokens
    assert f"RT_RUNTIME_DIR={runtime}" in tokens
    assert f"RT_CODEX_RUNTIME_DIR={runtime}" in tokens


def test_installed_layout_resolves_manifest_owned_hermetic_wrapper(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "installed"
    version = prefix / "versions" / "1.3.3"
    tool = version / "bin" / "rt-worktree"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    wrapper = prefix / "bin" / "rt-codex"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(
        "#!/bin/sh\n"
        f"export ROUNDTABLE_INSTALL_PREFIX={prefix}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    manifest = {
        "schema": "roundtable.install.v1",
        "prefix": str(prefix),
        "versions": [str(version)],
        "files": {
            str(wrapper): hashlib.sha256(wrapper.read_bytes()).hexdigest(),
        },
    }
    (prefix / "install-manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    monkeypatch.setenv("ROUNDTABLE_INSTALL_PREFIX", str(prefix))

    resolved = ORIGINAL_SEAT_LAUNCHER(
        "codex",
        tool_path=tool,
        environ=os.environ,
    )
    command = launcher_shell_command(
        resolved.path,
        "codex",
        environment=dict(resolved.environment),
    )

    assert resolved.path == wrapper
    assert resolved.environment == ()
    assert str(wrapper) in command
    assert "ROUNDTABLE_INSTALL_PREFIX=" not in command
