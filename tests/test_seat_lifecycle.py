import argparse
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
ISOLATED_BIN = ROOT / "tests" / "fixtures" / "bin"
sys.path.insert(0, str(BIN))

import _rtlauncher
import _rtcodex
import _rtlib
import _rtruntime


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


wake = load_script("rt-codex-wake", "seat_lifecycle_codex_wake")
doctor = load_script("rt-doctor", "seat_lifecycle_doctor")
advisory = load_script("rt-startup-advisory", "seat_lifecycle_startup_advisory")


def run_tool(name: str, *args: str, cwd: Path | None = None, env=None):
    merged = os.environ.copy()
    merged.update(
        {
            "PATH": f"{ISOLATED_BIN}:{merged.get('PATH', '')}",
            "CMUX_SURFACE_ID": "",
            "CODEX_THREAD_ID": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ROUNDTABLE_PROJECT_DIR": "",
            "RT_FALLBACK_PROJECT": "",
            "RT_FROM": "",
            "RT_PROJECTS_FILE": "/dev/null",
        }
    )
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, str(BIN / name), *args],
        cwd=cwd or ROOT,
        env=merged,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_project(path: Path, *, codex: bool = True) -> Path:
    root = path.resolve()
    state = root / ".roundtable"
    state.mkdir(parents=True)
    harness = "codex" if codex else "claude-code"
    agent = "codex" if codex else "claude"
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {root}\n"
        "agents:\n"
        f"  {agent}:\n"
        f"    harness: {harness}\n"
    )
    return root


def write_registry(
    path: Path,
    entries: list[dict],
    *,
    schema: str = _rtlib.PROJECTS_SCHEMA,
) -> Path:
    path.write_text(
        json.dumps(
            {"schema": schema, "projects": entries}, indent=2
        )
        + "\n"
    )
    return path


def add_mail(project: Path, msg_id: str = "20260717T050000Z-claude-to-codex-1"):
    _rtlib.register_project(project)
    inbox = (
        _rtlib.resolve_project_mailbox(project).inbox_dir
        / "codex"
        / "new"
    )
    inbox.mkdir(parents=True, exist_ok=True)
    mail = inbox / f"{msg_id}.md"
    mail.write_text(f"[CLAUDE→CODEX directive id={msg_id}] test\n")
    return mail


def codex_thread(project: Path, thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "sessionId": "session-1",
        "cwd": str(project),
        "source": "cli",
        "threadSource": None,
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": "idle"},
    }


class FakeClient:
    def __init__(self, thread: dict):
        self.thread = thread
        self.calls = []
        self.turn_count = 0

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": [self.thread["id"]]}
        if method in {"thread/read", "thread/resume"}:
            return {"thread": dict(self.thread)}
        if method == "thread/name/set":
            return {}
        if method == "hooks/list":
            return {
                "data": [
                    {
                        "cwd": self.thread["cwd"],
                        "hooks": [],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        if method == "turn/start":
            self.turn_count += 1
            return {"turn": {"id": f"wake-{self.turn_count}"}}
        raise AssertionError(method)


class TTYInput(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def isolate_wake_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "RUNTIME_DIR", tmp_path / "wake-runtime")


def test_rt_projects_add_list_rm_and_canonical_idempotence(tmp_path):
    project = write_project(tmp_path / "project")
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)
    registry = tmp_path / "projects.yaml"

    added = run_tool(
        "rt-projects", "--registry", str(registry), "add", str(project)
    )
    rejected_alias = run_tool(
        "rt-projects", "--registry", str(registry), "add", str(alias)
    )
    duplicate = run_tool(
        "rt-projects", "--registry", str(registry), "add", str(project)
    )
    listed = run_tool("rt-projects", "--registry", str(registry), "list")

    assert added.returncode == 0, added.stderr
    assert f"added {project}" in added.stdout
    assert rejected_alias.returncode != 0
    assert "symbolic-link project root" in rejected_alias.stderr
    assert duplicate.returncode == 0, duplicate.stderr
    assert f"already registered {project}" in duplicate.stdout
    assert listed.returncode == 0, listed.stderr
    assert [line.split("\t", 1)[0] for line in listed.stdout.splitlines()] == [
        str(project)
    ]

    rejected_remove_alias = run_tool(
        "rt-projects", "--registry", str(registry), "rm", str(alias)
    )
    removed = run_tool(
        "rt-projects", "--registry", str(registry), "rm", str(project)
    )
    absent = run_tool(
        "rt-projects", "--registry", str(registry), "rm", str(project)
    )
    assert rejected_remove_alias.returncode != 0
    assert "symbolic-link project root" in rejected_remove_alias.stderr
    assert removed.returncode == 0, removed.stderr
    assert f"tombstoned {project}" in removed.stdout
    assert absent.returncode == 1
    assert f"not registered {project}" in absent.stdout


def test_rt_projects_list_warns_and_preserves_invalid_entries(tmp_path):
    project = write_project(tmp_path / "valid")
    stale = tmp_path / "deleted-project"
    registry = write_registry(
        tmp_path / "projects.yaml",
        [
            {"root": str(project), "registered_at": "2026-07-17T00:00:00Z"},
            {"root": str(stale), "registered_at": "2026-07-17T00:01:00Z"},
            {"root": "relative/project", "registered_at": "2026-07-17T00:02:00Z"},
        ],
        schema=_rtlib.LEGACY_PROJECTS_SCHEMA,
    )
    before = registry.read_text()

    listed = run_tool("rt-projects", "--registry", str(registry), "list")

    assert listed.returncode == 0
    assert listed.stdout.split("\t", 1)[0] == str(project)
    assert "registry warning" in listed.stderr
    assert str(stale) in listed.stderr
    assert "path is not absolute" in listed.stderr
    assert registry.read_text() == before


def test_roundtable_init_registers_via_rt_projects_file(tmp_path):
    parent = tmp_path / "projects"
    home = tmp_path / "home"
    parent.mkdir()
    home.mkdir()
    registry = tmp_path / "isolated-projects.yaml"

    proc = run_tool(
        "roundtable-init",
        "--no-git",
        "--parent",
        str(parent),
        "created",
        env={"HOME": str(home), "RT_PROJECTS_FILE": str(registry)},
    )

    project = (parent / "created").resolve()
    entries, warnings = _rtlib.load_project_registry(registry)
    assert proc.returncode == 0, proc.stderr
    assert f"registered {project}" in proc.stdout
    assert warnings == []
    assert [entry["root"] for entry in entries] == [project]
    assert entries[0]["registered_at"].endswith("Z")


def test_launcher_project_ancestor_normalizes_to_root_even_without_tty(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    registry = tmp_path / "projects.yaml"
    _rtlib.register_project(project, registry)
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    nested = project / "nested" / "deeper"
    nested.mkdir(parents=True)

    selected = _rtlauncher.choose_launch_cwd(
        "codex", cwd=nested, stdin=io.StringIO(), stderr=io.StringIO()
    )

    assert selected == project


def test_launcher_anchored_project_fails_before_launch_when_unregistered(
    tmp_path,
    monkeypatch,
):
    project = write_project(tmp_path / "project")
    registry = write_registry(tmp_path / "projects.yaml", [])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))

    with pytest.raises(
        _rtlauncher.SelectionError,
        match="project registration preflight failed",
    ):
        _rtlauncher.choose_launch_cwd(
            "codex",
            cwd=project,
            stdin=io.StringIO(),
            stderr=io.StringIO(),
        )


def test_launcher_outside_project_non_tty_fails_without_prompt(tmp_path):
    stderr = io.StringIO()

    with pytest.raises(
        _rtlauncher.SelectionError, match="stdin is not a TTY"
    ):
        _rtlauncher.choose_launch_cwd(
            "claude", cwd=tmp_path, stdin=io.StringIO(), stderr=stderr
        )

    assert stderr.getvalue() == ""


def test_launcher_menu_selects_registered_project(tmp_path, monkeypatch):
    project = write_project(tmp_path / "registered")
    registry = tmp_path / "projects.yaml"
    _rtlib.register_project(
        project, registry, registered_at="2026-07-17T00:00:00Z"
    )
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    stderr = io.StringIO()

    selected = _rtlauncher.choose_launch_cwd(
        "hermes", cwd=tmp_path, stdin=TTYInput("1\n"), stderr=stderr
    )

    assert selected == project
    assert f"1) {project}" in stderr.getvalue()


def test_launcher_menu_reprompts_after_invalid_numeric_input(tmp_path, monkeypatch):
    project = write_project(tmp_path / "registered", codex=False)
    registry = tmp_path / "projects.yaml"
    _rtlib.register_project(project, registry)
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    stderr = io.StringIO()

    selected = _rtlauncher.choose_launch_cwd(
        "claude",
        cwd=tmp_path / "outside",
        stdin=TTYInput("0\n1\n"),
        stderr=stderr,
    )

    assert selected == project
    assert "please try again" in stderr.getvalue()


def test_launcher_menu_creates_then_selects_project(tmp_path, monkeypatch):
    registry = write_registry(tmp_path / "projects.yaml", [])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    calls = []

    def fake_init(command, check):
        calls.append((command, check))
        project = write_project(tmp_path / "new-project")
        _rtlib.register_project(project, registry)
        return SimpleNamespace(returncode=0)

    selected = _rtlauncher.choose_launch_cwd(
        "claude",
        cwd=tmp_path,
        stdin=TTYInput("2\nnew-project\n\n"),
        stderr=(stderr := io.StringIO()),
        init_runner=fake_init,
    )

    assert selected == (tmp_path / "new-project").resolve()
    assert "Initialize Git too? [y/N]" in stderr.getvalue()
    assert calls == [
        (
            [
                str(BIN / "roundtable-init"),
                "new-project",
                "--parent",
                str(tmp_path.resolve()),
            ],
            False,
        )
    ]


def test_launcher_menu_only_opts_into_git_after_yes(tmp_path, monkeypatch):
    registry = write_registry(tmp_path / "projects.yaml", [])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    calls = []

    def fake_init(command, check):
        calls.append((command, check))
        project = write_project(tmp_path / "new-project")
        _rtlib.register_project(project, registry)
        return SimpleNamespace(returncode=0)

    selected = _rtlauncher.choose_launch_cwd(
        "hermes",
        cwd=tmp_path,
        stdin=TTYInput("2\nnew-project\nyes\n"),
        stderr=io.StringIO(),
        init_runner=fake_init,
    )

    assert selected == (tmp_path / "new-project").resolve()
    assert calls[0][0][-1] == "--git"


def test_launcher_menu_allows_explicit_unanchored_start(tmp_path, monkeypatch):
    registry = write_registry(tmp_path / "projects.yaml", [])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    stderr = io.StringIO()

    selected = _rtlauncher.choose_launch_cwd(
        "hermes", cwd=tmp_path, stdin=TTYInput("3\n"), stderr=stderr
    )

    assert selected is None
    assert "advisory: starting without a Roundtable project anchor" in stderr.getvalue()


def test_launcher_menu_hides_unanchored_start_for_codex(tmp_path, monkeypatch):
    registry = write_registry(tmp_path / "projects.yaml", [])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    stderr = io.StringIO()

    with pytest.raises(_rtlauncher.SelectionError, match="selection out of range"):
        _rtlauncher.choose_launch_cwd(
            "codex", cwd=tmp_path, stdin=TTYInput("3\n"), stderr=stderr
        )

    output = stderr.getvalue()
    assert "Start without a project anchor" not in output
    assert "requires a project anchor" in output


def test_launcher_menu_can_safely_set_up_the_current_folder(
    tmp_path, monkeypatch
):
    registry = write_registry(tmp_path / "projects.yaml", [])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    calls = []

    def fake_init(command, cwd, check):
        calls.append((command, cwd, check))
        write_project(cwd)
        _rtlib.register_project(cwd, registry)
        return SimpleNamespace(returncode=0)

    selected = _rtlauncher.choose_launch_cwd(
        "claude",
        cwd=tmp_path,
        stdin=TTYInput("1\n\n"),
        stderr=io.StringIO(),
        init_runner=fake_init,
    )

    assert selected == tmp_path.resolve()
    assert calls == [
        (
            [str(BIN / "roundtable-init"), "--here"],
            tmp_path.resolve(),
            False,
        )
    ]


def test_home_is_never_discovered_as_a_project_root(tmp_path, monkeypatch):
    home = tmp_path / "home"
    write_project(home)
    monkeypatch.setenv("HOME", str(home))

    assert _rtlib.is_project_root(home) is False
    assert _rtlauncher.project_at_or_above(home) is None


@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        ("claude", ["claude", "--resume"]),
        ("hermes", ["hermes", "--continue"]),
        ("codex", ["codex", "--remote", "unix://", "--model", "gpt-5"]),
    ],
)
def test_launcher_exec_preserves_harness_contract(
    tmp_path, monkeypatch, harness, expected
):
    project = write_project(tmp_path / "project")
    configured_harness = {
        "claude": "claude-code",
        "codex": "codex",
        "hermes": "hermes-agent",
    }[harness]
    (project / ".roundtable" / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        f"  {harness}:\n"
        f"    harness: {configured_harness}\n"
    )
    argv = expected[len(_rtlauncher.COMMANDS[harness]) :]
    observed = {}

    class ExecCalled(Exception):
        pass

    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setenv("RT_FROM", harness)
    if harness == "hermes":
        monkeypatch.setenv("RT_HERMES_SKIP_AUTH_CHECK", "1")
    for name in (
        "RT_PROJECT_ROOT",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
        "RT_RUNTIME_DIR",
        "RT_CODEX_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        _rtlauncher.os, "chdir", lambda path: observed.setdefault("cwd", path)
    )
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, selected_harness: SimpleNamespace(
            project_root=root,
            agent_id=agent_id,
            session_id=f"{selected_harness}-session",
            revision=7,
        ),
    )

    def fake_execv(program, command):
        observed.update(program=program, command=command)
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)
    fake_binary = tmp_path / harness
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: fake_binary)
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action=None: ready_action() if ready_action else None,
    )
    monkeypatch.setattr(
        _rtlauncher,
        "arm_codex_launch_intent",
        lambda _token: None,
    )

    with pytest.raises(ExecCalled):
        _rtlauncher.launch(harness, argv)

    expected = [str(fake_binary), *expected[1:]]
    if harness == "claude":
        expected.extend(["--remote-control", "claude@project"])
    elif harness == "codex":
        expected[3:3] = ["-C", str(project)]
        expected.extend(_rtlauncher.codex_seat_overrides())
    assert observed == {"cwd": project, "program": expected[0], "command": expected}


def test_harness_bin_skips_cmux_path_shim(tmp_path, monkeypatch):
    home = tmp_path / "home"
    shim_dir = tmp_path / "cmux-cli-shims"
    real_dir = tmp_path / "real-bin"
    shim_dir.mkdir()
    real_dir.mkdir()
    shim = shim_dir / "claude"
    real = real_dir / "claude"
    for executable in (shim, real):
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
    monkeypatch.setattr(_rtlauncher.Path, "home", classmethod(lambda _cls: home))
    monkeypatch.setenv("PATH", f"{shim_dir}:{real_dir}")
    monkeypatch.delenv("RT_CLAUDE_BIN", raising=False)

    assert _rtlauncher.harness_bin("claude") == real.absolute()


def test_harness_bin_explicit_override_rejects_cmux_wrapper(tmp_path, monkeypatch):
    wrapper = tmp_path / "cmux-cli-shims" / "claude"
    wrapper.parent.mkdir()
    wrapper.write_text("#!/bin/sh\n")
    wrapper.chmod(0o755)
    monkeypatch.setenv("RT_CLAUDE_BIN", str(wrapper))

    with pytest.raises(_rtlauncher.SelectionError, match="cmux wrapper"):
        _rtlauncher.harness_bin("claude")


def test_launcher_sets_unique_instance_identity(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project", codex=False)
    config = project / ".roundtable" / "agents.yaml"
    config.write_text(
        "schema: roundtable.agents.v1\n"
        "agents:\n"
        "  reviewer:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude-review\n"
    )
    observed = {}

    class ExecCalled(Exception):
        pass

    monkeypatch.delenv("RT_FROM", raising=False)
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: tmp_path / "claude")
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, harness: SimpleNamespace(
            project_root=root,
            agent_id=agent_id,
            session_id=f"{harness}-session",
            revision=1,
        ),
    )
    for name in (
        "RT_PROJECT_ROOT",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
        "RT_RUNTIME_DIR",
        "RT_CODEX_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)

    def capture_identity(_program, _command):
        observed["sender"] = os.environ.get("RT_FROM")
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", capture_identity)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("claude", [])

    assert observed["sender"] == "claude-review"


def test_launcher_requires_explicit_identity_for_multiple_instances(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project", codex=False)
    config = project / ".roundtable" / "agents.yaml"
    config.write_text(
        "schema: roundtable.agents.v1\n"
        "agents:\n"
        "  claude:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude#1\n"
        "      - id: claude#2\n"
    )
    monkeypatch.delenv("RT_FROM", raising=False)
    monkeypatch.delenv("RT_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("RT_CODEX_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)

    with pytest.raises(_rtlauncher.SelectionError, match="set RT_FROM"):
        _rtlauncher.launch("claude", [])


def test_bridge_default_unbound_keeps_mail_without_rpc_or_turn(tmp_path):
    project = write_project(tmp_path / "project")
    mail = add_mail(project)
    client = FakeClient(codex_thread(project))
    store = wake.StateStore(tmp_path / "wake-state.json")

    result = wake.WakeBridge(client, [project], store).step()[0]

    assert not result.ok
    assert "no explicit Codex binding" in result.detail
    assert client.calls == []
    assert store.bindings == {}
    assert mail.is_file()


def test_bridge_isolates_bad_project_identity_and_continues(
    tmp_path, monkeypatch
):
    registry = tmp_path / "projects.yaml"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    bad = write_project(tmp_path / "bad")
    good = write_project(tmp_path / "good")
    _rtlib.register_project(bad, registry)
    _rtlib.register_project(good, registry)
    _rtlib.project_identity_path(bad).write_text("{not-json\n")
    store = wake.StateStore(tmp_path / "wake-state.json")
    client = FakeClient(codex_thread(good))

    results = wake.WakeBridge(client, [bad, good], store).step()

    assert len(results) == 2
    assert not results[0].ok
    assert "mailbox resolution failed" in results[0].detail
    assert results[1].ok and results[1].detail == "empty"
    assert store.project_state(bad)["phase"] == "IDENTITY_ERROR"


def test_bridge_auto_discover_is_explicit_opt_in(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project)
    client = FakeClient(codex_thread(project))
    state_path = tmp_path / "wake-state.json"

    result = wake.WakeBridge(
        client,
        [project],
        wake.StateStore(state_path),
        auto_discover=True,
    ).step()[0]

    assert result.ok and result.detail == "wake started"
    assert client.turn_count == 1
    assert wake.StateStore(state_path).bindings[str(project)]["threadId"] == "thread-1"


def test_rt_codex_wake_unbind_removes_only_target_binding(tmp_path):
    first = write_project(tmp_path / "first")
    second = write_project(tmp_path / "second")
    state_path = tmp_path / "wake-state.json"
    store = wake.StateStore(state_path)
    store.bind(first, codex_thread(first, "thread-first"))
    store.bind(second, codex_thread(second, "thread-second"))

    removed = run_tool(
        "rt-codex-wake",
        "--state-file",
        str(state_path),
        "unbind",
        str(first),
    )
    absent = run_tool(
        "rt-codex-wake",
        "--state-file",
        str(state_path),
        "unbind",
        str(first),
    )

    persisted = wake.StateStore(state_path)
    assert removed.returncode == 0, removed.stderr
    assert f"unbound project={first}" in removed.stdout
    assert absent.returncode == 1
    assert str(first) not in persisted.bindings
    assert str(first) not in persisted.data["projects"]
    assert persisted.bindings[str(second)]["threadId"] == "thread-second"


def test_rt_codex_wake_handoff_clears_only_stale_claim_and_prints_resume(
    tmp_path, monkeypatch, capsys
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    state_path = tmp_path / "wake-state.json"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    old = _rtruntime.claim(project, "codex", "codex")
    store = wake.StateStore(state_path)
    store.bind(project, codex_thread(project), lease=old)
    _rtruntime.arm_codex_launch_intent(old)

    # A dead owner proves that the claim can be cleared; an active owner is
    # refused by the same command before any state mutation.
    monkeypatch.setattr(_rtruntime, "_pid_state", lambda _pid: "dead")
    client = FakeClient(codex_thread(project))
    client.close = lambda: None
    monkeypatch.setattr(wake, "AppServerClient", lambda _socket: client)
    monkeypatch.setattr(wake, "require_supported_version", lambda: None)
    monkeypatch.setattr(wake, "require_supported_daemon", lambda _socket: None)

    result = wake.handoff_command(
        SimpleNamespace(
            project=str(project),
            thread_id="thread-1",
            socket=tmp_path / "socket",
            state_file=state_path,
        )
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "handoff prepared" in output
    assert "rt-codex resume thread-1" in output
    assert "rt-codex --resume" not in output
    assert not _rtruntime.seat_paths(project, "codex").project_dir.joinpath(
        "codex-launch-intent.json"
    ).exists()
    persisted = wake.StateStore(state_path)
    assert str(project) not in persisted.bindings


@pytest.mark.parametrize("version", [(0, 144, 6), (0, 147, 0)])
def test_handoff_resume_invocation_matches_pinned_cli_surfaces(version):
    """Parse the constructed launch path with both captured CLI grammars."""

    parser = argparse.ArgumentParser(prog=f"codex-{version}", add_help=False)
    parser.add_argument("--remote")
    parser.add_argument("-C", "--cd")
    subcommands = parser.add_subparsers(dest="command", required=True)
    resume = subcommands.add_parser("resume", add_help=False)
    resume.add_argument("session_id")
    command = [
        "--remote",
        "unix://",
        "-C",
        "/project",
        *wake.codex_resume_launcher_args("thread-1"),
    ]

    parsed = parser.parse_args(command)

    assert parsed.command == "resume"
    assert parsed.session_id == "thread-1"


def test_rt_codex_wake_handoff_refuses_a_live_seat_without_mutation(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    state_path = tmp_path / "wake-state.json"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    token = _rtruntime.claim(project, "codex", "codex")
    store = wake.StateStore(state_path)
    store.bind(project, codex_thread(project), lease=token)
    _rtruntime.arm_codex_launch_intent(token)
    client = FakeClient(codex_thread(project))
    client.close = lambda: None
    monkeypatch.setattr(wake, "AppServerClient", lambda _socket: client)
    monkeypatch.setattr(wake, "require_supported_version", lambda: None)
    monkeypatch.setattr(wake, "require_supported_daemon", lambda _socket: None)

    with pytest.raises(wake.IdentityError, match="active"):
        wake.handoff_command(
            SimpleNamespace(
                project=str(project),
                thread_id="thread-1",
                socket=tmp_path / "socket",
                state_file=state_path,
            )
        )

    assert str(project) in wake.StateStore(state_path).bindings
    assert _rtruntime.seat_paths(project, "codex").project_dir.joinpath(
        "codex-launch-intent.json"
    ).exists()


def test_bridge_discovers_watch_roots_from_registry_only(
    tmp_path, monkeypatch, capsys
):
    codex_project = write_project(tmp_path / "codex")
    non_codex_project = write_project(tmp_path / "claude", codex=False)
    stale = write_project(tmp_path / "stale")
    registry = tmp_path / "projects.yaml"
    for project in (codex_project, non_codex_project, stale):
        _rtlib.register_project(project, registry)
    (stale / ".roundtable" / "agents.yaml").unlink()
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))

    projects = wake.discover_projects([])

    assert projects == [codex_project]
    assert str(stale) in capsys.readouterr().err


def test_bridge_discovery_isolates_unrelated_malformed_registry_row(
    tmp_path, monkeypatch, capsys
):
    healthy = write_project(tmp_path / "healthy")
    registry = tmp_path / "projects.yaml"
    _rtlib.register_project(healthy, registry)
    document = _rtlib.yaml.safe_load(registry.read_text())
    document["projects"].append(
        {
            "uuid": "00000000-0000-4000-8000-000000000001",
            "path": "relative/broken",
            "name": "broken",
            "group": "broken",
            "layout": "local",
            "status": "active",
            "registered_at": "2026-07-29T00:00:00Z",
            "tombstoned_at": None,
        }
    )
    write_registry(registry, document["projects"])
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))

    assert wake.discover_projects([]) == [healthy]
    assert "path is not absolute" in capsys.readouterr().err


def test_doctor_reports_tripwire_and_codex_wrong_anchor(tmp_path, monkeypatch, capsys):
    project = write_project(tmp_path / "registered")
    wrong = write_project(tmp_path / "wrong")
    registry = tmp_path / "projects.yaml"
    _rtlib.register_project(project, registry)
    marker = (
        _rtlib.resolve_project_mailbox(
            project, registry_path=registry
        ).inbox_dir
        / "claude"
        / ".armed-123"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    state_file = tmp_path / "wake-state.json"
    state_file.write_text(
        json.dumps(
            {
                "schema": wake.STATE_SCHEMA,
                "bindings": {
                    str(project): {
                        "agent": "codex",
                        "project": str(project),
                        "threadId": "thread-1",
                    }
                },
                "projects": {},
            }
        )
    )

    class DoctorClient:
        def __init__(self, _socket):
            pass

        def request(self, method, params):
            assert method == "thread/read"
            assert params["threadId"] == "thread-1"
            return {"thread": {"cwd": str(wrong)}}

        def close(self):
            pass

    monkeypatch.setattr(doctor, "pid_alive", lambda pid: pid == 123)
    monkeypatch.setattr(
        doctor, "tripwire_process", lambda pid, agent: (True, f"rt-wait-inbox {agent}")
    )
    monkeypatch.setattr(doctor, "process_cwd", lambda _pid: wrong)
    monkeypatch.setattr(doctor, "AppServerClient", DoctorClient)
    report = doctor.Report()

    doctor.project_health_checks(
        report, registry, state_file, tmp_path / "app.sock", rpc_ok=True
    )

    output = capsys.readouterr().out
    assert report.failed
    assert f"path={project}" in output
    assert f"WARN legacy-tripwire-marker: {marker}" in output
    assert "tripwire-anchor" not in output
    assert f"FAIL codex-anchor: thread=thread-1 cwd={wrong} expected={project}" in output


def test_startup_advisory_ignores_unregistered_peer_projects(tmp_path):
    registered = write_project(tmp_path / "registered")
    unregistered = write_project(tmp_path / "unregistered")
    caller = {
        "workspace_id": "UUID-A",
        "workspace_ref": "workspace:1",
        "surface_ref": "surface:self",
    }
    listing = {
        "workspace_id": "UUID-A",
        "workspace_ref": "workspace:1",
        "surfaces": [
            {"ref": "surface:self", "type": "terminal"},
            {
                "ref": "surface:registered",
                "type": "terminal",
                "requested_working_directory": str(registered / "nested"),
            },
            {
                "ref": "surface:unregistered",
                "type": "terminal",
                "requested_working_directory": str(unregistered / "nested"),
            },
        ],
    }

    assert (
        advisory.peer_project(
            caller, listing, "surface:self", {registered}
        )
        == registered
    )
    assert advisory.peer_project(caller, listing, "surface:self", set()) is None


def test_startup_advisory_silences_malformed_registry(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CMUX_SURFACE_ID", "surface:self")
    monkeypatch.setattr(advisory, "project_at_or_above", lambda _cwd: None)

    monkeypatch.setattr(
        advisory,
        "load_project_registry",
        lambda: ([], ["malformed fixture"]),
    )
    monkeypatch.setattr(
        advisory,
        "command_json",
        lambda *_args, **_kwargs: pytest.fail(
            "malformed registry must suppress advisory before cmux"
        ),
    )

    assert advisory.main() == 0
