from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtrchost
import _rtlib
import _rtruntime


def load_script(path: Path, name: str):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


rc_cli = load_script(BIN / "rt-rc-host", "rt_rc_host_tests")
wait = load_script(BIN / "rt-wait-inbox", "rt_wait_inbox_rc_adoption_tests")


def git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def write_project(path: Path, *, register: bool = True) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", "pneu tests")
    git(path, "config", "user.email", "pneu@example.invalid")
    state = path / ".roundtable"
    state.mkdir()
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {path.resolve()}\n"
        "agents:\n"
        "  claude:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude\n",
        encoding="utf-8",
    )
    (path / "README.md").write_text("fixture\n", encoding="utf-8")
    git(path, "add", "README.md", ".roundtable/agents.yaml")
    git(path, "commit", "-qm", "fixture")
    if register:
        _rtlib.register_project(path)
    return path.resolve()


@pytest.fixture
def rc_lab(tmp_path, monkeypatch):
    registry = tmp_path / "projects.yaml"
    runtime = tmp_path / "runtime"
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    home.mkdir()
    claude = tmp_path / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    claude.chmod(0o755)
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_LAUNCH_AGENTS_DIR", str(launch_agents))
    monkeypatch.setenv("RT_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("ROUNDTABLE_INSTALL_PREFIX", raising=False)
    monkeypatch.delenv(_rtrchost.REGISTRATION_ENV, raising=False)
    project = write_project(tmp_path / "project")
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    str(project): {"hasTrustDialogAccepted": True},
                }
            }
        ),
        encoding="utf-8",
    )
    launch_state = {"loaded": False}

    def inspect(_label):
        if launch_state["loaded"]:
            return True, "pid = 123", 123
        return False, "not loaded", None

    def run_launchctl(arguments):
        if arguments[0] == "bootstrap":
            launch_state["loaded"] = True
        elif arguments[0] == "bootout":
            launch_state["loaded"] = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_rtrchost, "_launchctl_inspect", inspect)
    monkeypatch.setattr(_rtrchost, "_run_launchctl", run_launchctl)
    monkeypatch.setattr(_rtrchost, "_pid_alive", lambda pid: pid == 123)
    return project, home, launch_agents, launch_state


def test_enable_is_project_local_idempotent_and_disable_reverses_owned_state(rc_lab):
    project, home, launch_agents, launch_state = rc_lab

    first = _rtrchost.enable(project, home=home)
    second = _rtrchost.enable(project, home=home)

    assert first.healthy and second.healthy
    settings_path = project / ".claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    expected = _rtrchost.hook_groups(BIN / "rt-rc-host")
    assert settings["hooks"]["WorktreeCreate"] == [expected["WorktreeCreate"]]
    assert settings["hooks"]["WorktreeRemove"] == [expected["WorktreeRemove"]]
    assert not (home / ".claude" / "settings.json").exists()

    plist_path = launch_agents / f"{first.label}.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["WorkingDirectory"] == str(project)
    assert plist["ProgramArguments"][1:4] == ["remote-control", "--spawn", "worktree"]
    assert "claude@project" in plist["ProgramArguments"]
    assert plist["EnvironmentVariables"][_rtrchost.REGISTRATION_ENV] == first.project_uuid

    disabled = _rtrchost.disable(project)

    assert not disabled.enabled
    assert not launch_state["loaded"]
    assert not settings_path.exists()
    assert not plist_path.exists()
    assert _rtrchost.load_state_for_project(project) is None


def test_trust_gate_has_plain_remedy_and_makes_no_partial_changes(rc_lab):
    project, home, launch_agents, _launch_state = rc_lab
    (home / ".claude.json").write_text(json.dumps({"projects": {}}), encoding="utf-8")

    with pytest.raises(_rtrchost.RCHostError, match="accept the workspace trust dialog"):
        _rtrchost.enable(project, home=home)

    assert not (project / ".claude" / "settings.local.json").exists()
    assert not launch_agents.exists()
    assert _rtrchost.load_state_for_project(project) is None


def test_trust_accepted_on_ancestor_directory_satisfies_the_gate(rc_lab):
    project, home, _launch_agents, _launch_state = rc_lab
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    str(project.parent): {"hasTrustDialogAccepted": True},
                    str(project): {"hasTrustDialogAccepted": False},
                }
            }
        ),
        encoding="utf-8",
    )

    assert _rtrchost.workspace_trusted(project, home=home) is True

    enabled = _rtrchost.enable(project, home=home)
    assert enabled.healthy


def test_trust_records_without_any_true_ancestor_stay_untrusted(rc_lab):
    project, home, _launch_agents, _launch_state = rc_lab
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {
                    str(project.parent): {"hasTrustDialogAccepted": False},
                    str(project / "nested"): {"hasTrustDialogAccepted": True},
                }
            }
        ),
        encoding="utf-8",
    )

    assert _rtrchost.workspace_trusted(project, home=home) is False


def test_disable_refuses_hook_drift_before_unloading_or_mutating(rc_lab):
    project, home, launch_agents, launch_state = rc_lab
    enabled = _rtrchost.enable(project, home=home)
    settings_path = project / ".claude" / "settings.local.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["WorktreeCreate"] = []
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(_rtrchost.RCHostError, match="drifted"):
        _rtrchost.disable(project)

    assert launch_state["loaded"]
    assert (launch_agents / f"{enabled.label}.plist").is_file()
    assert _rtrchost.load_state_for_project(project) is not None


def test_enable_bootstrap_failure_restores_preexisting_settings(rc_lab, monkeypatch):
    project, home, launch_agents, _launch_state = rc_lab
    settings_path = project / ".claude" / "settings.local.json"
    settings_path.parent.mkdir()
    original = b'{"theme":"dark","hooks":{"Notification":[]}}\n'
    settings_path.write_bytes(original)
    settings_path.chmod(0o640)
    monkeypatch.setattr(
        _rtrchost,
        "_run_launchctl",
        lambda _arguments: SimpleNamespace(
            returncode=5,
            stdout="",
            stderr="bootstrap refused",
        ),
    )

    with pytest.raises(_rtrchost.RCHostError, match="bootstrap refused"):
        _rtrchost.enable(project, home=home)

    assert settings_path.read_bytes() == original
    assert settings_path.stat().st_mode & 0o777 == 0o640
    assert not list(launch_agents.glob("*.plist"))
    assert _rtrchost.load_state_for_project(project) is None


def test_worktree_create_never_returns_empty_success(monkeypatch, tmp_path):
    payload = {
        "hook_event_name": "WorktreeCreate",
        "cwd": str(tmp_path),
        "name": "phone-task",
    }
    monkeypatch.setattr(rc_cli, "_payload_project", lambda *_args: tmp_path)
    monkeypatch.setattr(
        rc_cli,
        "require_enabled",
        lambda _project: {"projectRoot": str(tmp_path)},
    )
    monkeypatch.setattr(
        rc_cli,
        "_run_worktree",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = rc_cli.hook_create(io.StringIO(json.dumps(payload)), stdout, stderr)

    assert code == 2
    assert stdout.getvalue() == ""
    assert "produced no unique worktree path" in stderr.getvalue()


def test_worktree_hooks_create_registered_container_tree_and_defer_live_removal(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "anchor")
    monkeypatch.setattr(
        rc_cli,
        "require_enabled",
        lambda _project: {"projectRoot": str(project)},
    )
    create_stdout = io.StringIO()
    create_stderr = io.StringIO()

    code = rc_cli.hook_create(
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "WorktreeCreate",
                    "cwd": str(project),
                    "name": "phone-task",
                }
            )
        ),
        create_stdout,
        create_stderr,
    )

    target = tmp_path / "anchor-worktree" / "phone-task"
    assert code == 0, create_stderr.getvalue()
    assert create_stdout.getvalue().strip() == str(target)
    assert target.is_dir()
    assert _rtlib.resolve_project_mailbox_checked(target).project_root == target

    lease = _rtruntime.claim(
        target,
        "claude",
        "claude",
        owner_pid=os.getpid(),
        session_id="live-phone-session",
    )
    try:
        remove_stderr = io.StringIO()
        code = rc_cli.hook_remove(
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "WorktreeRemove",
                        "cwd": str(project),
                        "worktree_path": str(target),
                    }
                )
            ),
            remove_stderr,
        )
        assert code == 0
        assert target.is_dir()
        assert "removal deferred" in remove_stderr.getvalue()
    finally:
        assert _rtruntime.release(lease)


def test_registered_unleased_session_adoption_is_idempotent(tmp_path, monkeypatch):
    project = write_project(tmp_path / "adopted")
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "claude-env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    environment = {"CLAUDE_ENV_FILE": str(env_file)}
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "phone-session-1",
        "cwd": str(project),
    }

    first = wait._adopt_unleased_claude_session(
        payload, environment=environment, owner_pid=os.getpid()
    )
    first_token = _rtruntime.inspect_seat(project, "claude").token
    second = wait._adopt_unleased_claude_session(
        {**payload, "source": "compact"},
        environment=environment,
        owner_pid=os.getpid(),
    )
    second_token = _rtruntime.inspect_seat(project, "claude").token

    assert first == second == "claude"
    assert first_token is not None and second_token is not None
    assert first_token.revision == second_token.revision
    exports = env_file.read_text(encoding="utf-8")
    assert "export RT_PROJECT_ROOT=" in exports
    assert "export RT_SESSION_ID=phone-session-1" in exports


def test_adoption_releases_new_lease_when_environment_persistence_fails(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "adoption-rollback")
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "claude-env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(
        wait,
        "_persist_adopted_environment",
        lambda *_args: (_ for _ in ()).throw(
            _rtruntime.RuntimeStateError("simulated persistence failure")
        ),
    )

    with pytest.raises(_rtruntime.RuntimeStateError, match="persistence failure"):
        wait._adopt_unleased_claude_session(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": "phone-session-rollback",
                "cwd": str(project),
            },
            environment={"CLAUDE_ENV_FILE": str(env_file)},
            owner_pid=os.getpid(),
        )

    assert _rtruntime.inspect_seat(project, "claude").status == "vacant"


def test_adoption_never_claims_unregistered_or_displaces_live_lease(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    env_file = tmp_path / "claude-env"
    env_file.write_text("", encoding="utf-8")
    environment = {"CLAUDE_ENV_FILE": str(env_file)}
    unregistered = write_project(tmp_path / "unregistered", register=False)
    payload = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": "new-session",
        "cwd": str(unregistered),
    }
    assert (
        wait._adopt_unleased_claude_session(
            payload, environment=environment, owner_pid=os.getpid()
        )
        is None
    )
    assert _rtruntime.inspect_seat(unregistered, "claude").status == "vacant"

    registered = write_project(tmp_path / "occupied")
    lease = _rtruntime.claim(
        registered,
        "claude",
        "claude",
        owner_pid=os.getpid(),
        session_id="existing-session",
    )
    try:
        assert (
            wait._adopt_unleased_claude_session(
                {**payload, "cwd": str(registered)},
                environment=environment,
                owner_pid=os.getppid(),
            )
            is None
        )
        current = _rtruntime.inspect_seat(registered, "claude").token
        assert current is not None and current.revision == lease.revision
    finally:
        assert _rtruntime.release(lease)
