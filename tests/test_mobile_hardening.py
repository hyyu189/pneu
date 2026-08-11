from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtcodex
import _rtlauncher


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


wake = load_script("rt-codex-wake", "mobile_hardening_codex_wake")


class ExecCalled(Exception):
    pass


def write_project(path: Path, *, agent: str, harness: str) -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        f"  {agent}:\n"
        f"    harness: {harness}\n"
        "    instances:\n"
        f"      - id: {agent}\n"
    )
    return project


def codex_thread(cwd: Path, thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "sessionId": "native-session",
        "cwd": str(cwd),
        "source": "cli",
        "threadSource": None,
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": "idle"},
    }


def test_thread_cwd_accepts_exact_path_and_symlink_alias(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "project-alias"
    alias.symlink_to(project, target_is_directory=True)

    assert _rtcodex.require_thread_project_cwd(
        project, codex_thread(project), expected_thread_id="thread-1"
    ) == project.resolve()
    assert _rtcodex.require_thread_project_cwd(
        project, codex_thread(alias), expected_thread_id="thread-1"
    ) == project.resolve()


def test_thread_cwd_mismatch_names_both_paths_and_operator_remedy(tmp_path):
    project = tmp_path / "selected"
    recorded = tmp_path / "recorded"
    project.mkdir()
    recorded.mkdir()

    with pytest.raises(_rtcodex.CodexRuntimeError) as captured:
        _rtcodex.require_thread_project_cwd(
            project,
            codex_thread(recorded),
            expected_thread_id="thread-1",
        )

    message = str(captured.value)
    assert str(recorded) in message
    assert str(project) in message
    assert "resume" in message.lower()
    assert "rt-codex-wake reanchor" in message


def test_thread_cwd_refuses_moved_worktree_path(tmp_path):
    project = tmp_path / "selected"
    project.mkdir()
    missing = tmp_path / "renamed-away"

    with pytest.raises(_rtcodex.CodexRuntimeError, match="no longer exists"):
        _rtcodex.require_thread_project_cwd(
            project,
            codex_thread(missing),
            expected_thread_id="thread-1",
        )


@pytest.mark.parametrize("argv", [["resume"], ["resume", "--last"]])
def test_roundtable_resume_requires_explicit_thread_id(argv):
    with pytest.raises(_rtlauncher.SelectionError, match="explicit thread ID"):
        _rtlauncher.codex_resume_thread_id(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["resume", "thread-1"],
        ["--strict-config", "resume", "thread-1"],
        ["--model", "gpt-5.6", "resume", "thread-1"],
        ["resume", "--all", "--model", "gpt-5.6", "thread-1"],
        ["resume", "--", "thread-1"],
    ],
)
def test_roundtable_resume_finds_explicit_thread_after_supported_options(argv):
    assert _rtlauncher.codex_resume_thread_id(argv) == "thread-1"


def test_codex_resume_validation_runs_before_claim(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project", agent="codex", harness="codex")
    calls = []
    for name in _rtlauncher.LEASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: tmp_path / "codex"
    )
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action: ready_action(),
    )

    def refuse_resume(_project, _argv):
        calls.append("resume-cwd")
        raise _rtlauncher.SelectionError("wrong project")

    monkeypatch.setattr(_rtlauncher, "preflight_codex_resume", refuse_resume)
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda *_args: calls.append("claim") or pytest.fail("must not claim"),
    )

    with pytest.raises(_rtlauncher.SelectionError, match="wrong project"):
        _rtlauncher.launch("codex", ["resume", "thread-1"])

    assert calls == ["resume-cwd"]


def test_codex_resume_preflight_reads_recorded_cwd(tmp_path, monkeypatch):
    project = tmp_path / "selected"
    recorded = tmp_path / "recorded"
    project.mkdir()
    recorded.mkdir()
    calls = []

    class Client:
        def __init__(self, socket):
            calls.append(("connect", socket))

        def request(self, method, params):
            calls.append((method, params))
            return {"thread": codex_thread(recorded)}

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(_rtcodex, "AppServerClient", Client)

    with pytest.raises(_rtlauncher.SelectionError, match="resume refused"):
        _rtlauncher.preflight_codex_resume(project, ["resume", "thread-1"])

    assert (
        "thread/read",
        {"threadId": "thread-1", "includeTurns": False},
    ) in calls
    assert calls[-1] == ("close", None)


def test_reanchor_uses_explicit_resume_cwd_override(tmp_path, monkeypatch, capsys):
    project = write_project(tmp_path / "selected", agent="codex", harness="codex")
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    before = codex_thread(recorded)
    calls = []

    class Client:
        def request(self, method, params):
            calls.append((method, params))
            if method == "thread/read":
                return {"thread": dict(before)}
            if method == "thread/resume":
                updated = dict(before)
                updated["cwd"] = str(project)
                return {"thread": updated}
            raise AssertionError(method)

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(wake, "require_supported_version", lambda: None)
    monkeypatch.setattr(wake, "require_supported_daemon", lambda _socket: None)
    monkeypatch.setattr(wake, "AppServerClient", lambda _socket: Client())

    result = wake.reanchor_command(
        SimpleNamespace(
            project=str(project),
            thread_id="thread-1",
            socket=tmp_path / "app.sock",
        )
    )

    assert result == 0
    assert (
        "thread/resume",
        {
            "threadId": "thread-1",
            "cwd": str(project),
            "excludeTurns": True,
        },
    ) in calls
    assert "reanchored thread=thread-1" in capsys.readouterr().out


def test_bind_refuses_wrong_thread_cwd_before_persisting(tmp_path, monkeypatch):
    project = write_project(tmp_path / "selected", agent="codex", harness="codex")
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    state_file = tmp_path / "wake-state.json"

    class Client:
        def request(self, method, _params):
            assert method == "thread/read"
            return {"thread": codex_thread(recorded)}

        def close(self):
            pass

    monkeypatch.setattr(wake, "require_supported_version", lambda: None)
    monkeypatch.setattr(wake, "require_supported_daemon", lambda _socket: None)
    monkeypatch.setattr(wake, "AppServerClient", lambda _socket: Client())

    with pytest.raises(wake.IdentityError, match="recorded cwd"):
        wake.bind_command(
            SimpleNamespace(
                project=str(project),
                thread_id="thread-1",
                socket=tmp_path / "app.sock",
                state_file=state_file,
            )
        )

    assert wake.StateStore(state_file).bindings == {}


def test_adoption_refuses_wrong_thread_cwd_before_fence_change(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "selected", agent="codex", harness="codex")
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    state_file = tmp_path / "wake-state.json"
    store = wake.StateStore(state_file)
    store.bind(project, codex_thread(project))
    original = dict(store.bindings[str(project)])
    token = SimpleNamespace(
        project_root=project,
        agent_id="codex",
        harness="codex",
        session_id="fresh-seat",
        revision=2,
    )
    monkeypatch.setattr(
        wake,
        "inspect_seat",
        lambda *_args: SimpleNamespace(status="active_healthy", token=token),
    )

    class Client:
        def request(self, method, _params):
            assert method == "thread/read"
            return {"thread": codex_thread(recorded)}

    with pytest.raises(wake.IdentityError, match="recorded cwd"):
        wake._adopt_stale_binding(
            Client(), store, project, store.bindings[str(project)]
        )

    assert wake.StateStore(state_file).bindings[str(project)] == original


def test_handoff_refuses_wrong_thread_cwd_before_unbinding(tmp_path, monkeypatch):
    project = write_project(tmp_path / "selected", agent="codex", harness="codex")
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    state_file = tmp_path / "wake-state.json"
    store = wake.StateStore(state_file)
    store.bind(project, codex_thread(project))

    class Client:
        def request(self, method, _params):
            assert method == "thread/read"
            return {"thread": codex_thread(recorded)}

        def close(self):
            pass

    monkeypatch.setattr(wake, "require_supported_version", lambda: None)
    monkeypatch.setattr(wake, "require_supported_daemon", lambda _socket: None)
    monkeypatch.setattr(wake, "AppServerClient", lambda _socket: Client())

    with pytest.raises(wake.IdentityError, match="recorded cwd"):
        wake.handoff_command(
            SimpleNamespace(
                project=str(project),
                thread_id="thread-1",
                socket=tmp_path / "app.sock",
                state_file=state_file,
            )
        )

    assert str(project) in wake.StateStore(state_file).bindings


def test_hermes_missing_credentials_refuses_before_claim(tmp_path, monkeypatch):
    project = write_project(
        tmp_path / "project", agent="hermes", harness="hermes-agent"
    )
    calls = []
    for name in _rtlauncher.LEASE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RT_FROM", "hermes")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("RT_HERMES_SKIP_AUTH_CHECK", raising=False)
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: tmp_path / "hermes"
    )
    monkeypatch.setattr(
        _rtlauncher, "claim", lambda *_args: calls.append("claim")
    )
    monkeypatch.setattr(_rtlauncher.os, "execv", lambda *_args: calls.append("exec"))

    with pytest.raises(_rtlauncher.SelectionError) as captured:
        _rtlauncher.launch("hermes", [])

    message = str(captured.value)
    assert "run `hermes` once outside pneu" in message.lower()
    assert "present-but-stale" in message
    assert calls == []


def test_hermes_present_or_explicit_bypass_passes_preflight(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("RT_HERMES_SKIP_AUTH_CHECK", raising=False)
    shared = hermes_home / "shared" / "nous_auth.json"
    shared.parent.mkdir(parents=True)
    shared.write_text("present-only; Hermes validates contents")

    _rtlauncher.preflight_hermes_credentials([])

    shared.unlink()
    monkeypatch.setenv("RT_HERMES_SKIP_AUTH_CHECK", "1")
    _rtlauncher.preflight_hermes_credentials([])
