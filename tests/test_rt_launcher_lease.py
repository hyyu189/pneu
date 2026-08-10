from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlauncher
import _rtruntime


class ExecCalled(Exception):
    pass


def lease(project: Path, agent_id: str, *, revision: int = 1):
    return SimpleNamespace(
        project_root=project.resolve(),
        agent_id=agent_id,
        session_id=f"session-{revision}",
        revision=revision,
    )


def write_project(project: Path, *, agent_id: str, harness: str) -> Path:
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project.resolve()}\n"
        "agents:\n"
        f"  {agent_id}:\n"
        f"    harness: {harness}\n"
        "    instances:\n"
        f"      - id: {agent_id}\n"
    )
    return project.resolve()


def clear_lease_environment(monkeypatch) -> None:
    for name in (
        *_rtlauncher.LEASE_ENV_NAMES,
        "RT_RUNTIME_DIR",
        "RT_CODEX_RUNTIME_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_launcher_ctrl_c_is_a_clean_cancellation(monkeypatch, capsys):
    def interrupting_launch(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(_rtlauncher, "launch", interrupting_launch)

    result = _rtlauncher.main("claude")

    captured = capsys.readouterr()
    assert result == 130
    assert "rt-claude: cancelled by user (Ctrl-C)" in captured.err
    assert "Traceback" not in captured.err


def test_anchored_launcher_claims_seat_and_exports_lease_environment(
    tmp_path, monkeypatch
):
    project = write_project(
        tmp_path / "project", agent_id="claude", harness="claude-code"
    )
    fake_binary = tmp_path / "claude"
    observed = {}
    calls = []

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_PROJECT_ROOT", "/stale/project")
    monkeypatch.setenv("RT_FROM", "claude")
    monkeypatch.setenv("RT_SESSION_ID", "stale-session")
    monkeypatch.setenv("RT_LEASE_REVISION", "6")
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )

    def fake_claim(root, agent_id, harness):
        calls.append((root, agent_id, harness))
        return lease(project, agent_id, revision=7)

    def fake_execv(program, command):
        observed["program"] = program
        observed["command"] = command
        observed["environment"] = {
            name: os.environ.get(name) for name in _rtlauncher.LEASE_ENV_NAMES
        }
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher, "claim", fake_claim)
    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("claude", ["--resume"])

    assert calls == [(project, "claude", "claude")]
    assert observed == {
        "program": str(fake_binary),
        "command": [str(fake_binary), "--resume"],
        "environment": {
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": "claude",
            "RT_SESSION_ID": "session-7",
            "RT_LEASE_REVISION": "7",
        },
    }


def test_resume_shaped_launch_reuses_same_process_lease_and_exports_record(
    tmp_path, monkeypatch
):
    project = write_project(
        tmp_path / "project", agent_id="claude", harness="claude-code"
    )
    runtime = tmp_path / "runtime"
    fake_binary = tmp_path / "claude"
    observed = {}

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    token = _rtruntime.claim(project, "claude", "claude")
    # Simulate a resume launcher inheriting a prior fence. The launcher must
    # reconcile it against the live record before exec instead of claiming a
    # second revision.
    monkeypatch.setenv("RT_PROJECT_ROOT", str(project))
    monkeypatch.setenv("RT_FROM", "claude")
    monkeypatch.setenv("RT_SESSION_ID", "stale-export")
    monkeypatch.setenv("RT_LEASE_REVISION", "stale-revision")
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda *_args: pytest.fail("resume path claimed the seat twice"),
    )

    def fake_execv(program, command):
        observed.update(
            program=program,
            command=command,
            environment={
                name: os.environ.get(name) for name in _rtlauncher.LEASE_ENV_NAMES
            },
        )
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("claude", ["--resume", "native-thread"])

    record_path = _rtruntime.seat_paths(project, "claude").lease
    record = json.loads(record_path.read_text())
    assert observed["command"] == [str(fake_binary), "--resume", "native-thread"]
    assert observed["environment"]["RT_SESSION_ID"] == record["sessionId"]
    assert observed["environment"]["RT_LEASE_REVISION"] == record["revision"]
    assert observed["environment"]["RT_SESSION_ID"] == token.session_id


def test_bare_claude_launcher_forces_a_fresh_native_chat(tmp_path, monkeypatch):
    project = write_project(
        tmp_path / "project", agent_id="claude", harness="claude-code"
    )
    fake_binary = tmp_path / "claude"
    native_session_id = "7f7c1e26-7632-4f66-bc5e-14ec57c61001"
    observed = {}

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "claude")
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, _harness: lease(root, agent_id),
    )
    monkeypatch.setattr(
        _rtlauncher.uuid,
        "uuid4",
        lambda: native_session_id,
    )

    def fake_execv(program, command):
        observed.update(program=program, command=command)
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("claude", [])

    assert observed == {
        "program": str(fake_binary),
        "command": [str(fake_binary), "--session-id", native_session_id],
    }


def test_unanchored_bare_claude_preserves_native_startup_mode(
    tmp_path, monkeypatch
):
    fake_binary = tmp_path / "claude"
    observed = {}

    clear_lease_environment(monkeypatch)
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: None
    )
    monkeypatch.setattr(
        _rtlauncher, "project_at_or_above", lambda _cwd: None
    )
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )

    def fake_execv(program, command):
        observed.update(program=program, command=command)
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("claude", [])

    assert observed == {
        "program": str(fake_binary),
        "command": [str(fake_binary)],
    }


@pytest.mark.parametrize(
    ("argv", "expected_args"),
    [
        ([], ["--tui"]),
        (["--continue"], ["--continue"]),
        (["--oneshot", "hello"], ["--oneshot", "hello"]),
    ],
)
def test_unanchored_hermes_defaults_to_tui_without_changing_explicit_modes(
    tmp_path, monkeypatch, argv, expected_args
):
    fake_binary = tmp_path / "hermes"
    observed = {}

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_PROJECT_ROOT", "/inherited/project")
    monkeypatch.setenv("RT_FROM", "manual-identity")
    monkeypatch.setenv("RT_SESSION_ID", "inherited-session")
    monkeypatch.setenv("RT_LEASE_REVISION", "99")
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: None
    )
    monkeypatch.setattr(
        _rtlauncher, "project_at_or_above", lambda _cwd: None
    )
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )

    def unexpected_claim(*_args, **_kwargs):
        raise AssertionError("unanchored launch must not claim a seat")

    def fake_execv(program, command):
        observed.update(
            program=program,
            command=command,
            environment={
                name: os.environ.get(name)
                for name in _rtlauncher.LEASE_ENV_NAMES
            },
        )
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher, "claim", unexpected_claim)
    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("hermes", argv)

    # The full inherited lease context marks this shell as another seat's
    # session, so the launcher scrubs every inherited seat variable —
    # including RT_FROM, which here is the caller seat's identity rather
    # than an explicit launch selection.
    assert observed == {
        "program": str(fake_binary),
        "command": [str(fake_binary), *expected_args],
        "environment": {
            "RT_PROJECT_ROOT": None,
            "RT_FROM": None,
            "RT_SESSION_ID": None,
            "RT_LEASE_REVISION": None,
        },
    }


def test_launch_scrubs_full_inherited_seat_environment(
    tmp_path, monkeypatch, capsys
):
    # Field finding #8 (2026-07-21): a launcher run from inside another
    # seat's shell inherited that seat's RT_* environment, so the wrong
    # RT_FROM reached identity selection. A complete foreign lease context
    # must be discarded before choosing or claiming the new seat.
    project = write_project(
        tmp_path / "project", agent_id="hermes", harness="hermes-agent"
    )
    fake_binary = tmp_path / "hermes"
    observed = {}
    calls = []

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RT_PROJECT_ROOT", "/foreign/project")
    monkeypatch.setenv("RT_FROM", "claude")
    monkeypatch.setenv("RT_SESSION_ID", "foreign-session")
    monkeypatch.setenv("RT_LEASE_REVISION", "41")
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )

    def fake_claim(root, agent_id, harness):
        calls.append((root, agent_id, harness))
        return lease(project, agent_id, revision=5)

    def fake_execv(program, command):
        observed["program"] = program
        observed["command"] = command
        observed["environment"] = {
            name: os.environ.get(name) for name in _rtlauncher.LEASE_ENV_NAMES
        }
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher, "claim", fake_claim)
    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("hermes", ["--continue"])

    assert calls == [(project, "hermes", "hermes")]
    assert observed == {
        "program": str(fake_binary),
        "command": [str(fake_binary), "--continue"],
        "environment": {
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": "hermes",
            "RT_SESSION_ID": "session-5",
            "RT_LEASE_REVISION": "5",
        },
    }
    assert (
        "rt-hermes: this shell inherited RT_PROJECT_ROOT, RT_SESSION_ID, RT_LEASE_REVISION"
        in capsys.readouterr().err
    )


def test_launch_preserves_explicit_rt_from_without_lease_context(
    tmp_path, monkeypatch, capsys
):
    project = (tmp_path / "project").resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  hermes:\n"
        "    harness: hermes-agent\n"
        "    instances:\n"
        "      - id: hermes-build\n"
        "      - id: hermes-review\n"
    )
    fake_binary = tmp_path / "hermes"
    observed = {}

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RT_FROM", "hermes-review")
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, _harness: lease(project, agent_id, revision=6),
    )

    def fake_execv(program, command):
        observed["environment"] = {
            name: os.environ.get(name) for name in _rtlauncher.LEASE_ENV_NAMES
        }
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("hermes", [])

    # RT_FROM alone carries no lease context: it stays the documented
    # explicit multi-instance selection and no advisory is printed.
    assert observed["environment"] == {
        "RT_PROJECT_ROOT": str(project),
        "RT_FROM": "hermes-review",
        "RT_SESSION_ID": "session-6",
        "RT_LEASE_REVISION": "6",
    }
    assert "ignoring Roundtable seat environment" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("active_healthy", "active"),
        ("active_unhealthy", "unhealthy"),
    ],
)
def test_occupied_seat_is_a_clear_selection_error(
    tmp_path, monkeypatch, status, expected
):
    project = (tmp_path / "project").resolve()

    class Occupied(RuntimeError):
        def __init__(self):
            self.inspection = SimpleNamespace(
                status=status,
                detail=f"seat is {status}",
                token=SimpleNamespace(agent_id="claude-build"),
            )

    def occupied(*_args, **_kwargs):
        raise Occupied

    monkeypatch.setattr(_rtlauncher, "SeatOccupied", Occupied)
    monkeypatch.setattr(_rtlauncher, "claim", occupied)

    with pytest.raises(_rtlauncher.SelectionError, match=expected) as captured:
        _rtlauncher.claim_launch_seat(project, "claude", "claude")
    assert "seat 'claude-build'" in str(captured.value)
    assert "requested seat 'claude'" in str(captured.value)


def test_explicit_identity_must_belong_to_selected_project_and_harness(
    tmp_path, monkeypatch
):
    project = write_project(
        tmp_path / "project", agent_id="claude", harness="claude-code"
    )
    monkeypatch.setenv("RT_FROM", "claude-review")

    with pytest.raises(
        _rtlauncher.SelectionError,
        match="RT_FROM='claude-review' is not configured",
    ):
        _rtlauncher.set_launch_identity(project, "claude")


def test_configured_instance_id_must_be_mailbox_safe(tmp_path):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    (project / ".roundtable" / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: ../outside\n"
    )

    with pytest.raises(
        _rtlauncher.SelectionError,
        match="configured instance id",
    ):
        _rtlauncher.configured_sender_ids(project, "codex")


def test_codex_propagates_claimed_seat_to_remote_tool_environment(
    tmp_path, monkeypatch
):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    fake_binary = tmp_path / "codex"
    observed = {}
    launch_order = []
    user_override = 'shell_environment_policy.set={MY_EXISTING_VALUE="keep"}'
    user_argv = ["-c", user_override, "--model", "gpt-5.6"]
    custom_runtime = (tmp_path / "custom-runtime").resolve()

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.setenv("RT_RUNTIME_DIR", str(custom_runtime))
    monkeypatch.delenv("RT_CODEX_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action: (
            launch_order.append("preflight"),
            ready_action(),
        ),
    )

    def claim_after_preflight(root, agent_id, harness):
        launch_order.append("claim")
        return lease(root, agent_id, revision=11)

    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        claim_after_preflight,
    )
    monkeypatch.setattr(
        _rtlauncher,
        "arm_codex_launch_intent",
        lambda _token: launch_order.append("arm"),
    )

    def fake_execv(program, command):
        observed["program"] = program
        observed["command"] = command
        observed["environment"] = {
            name: os.environ.get(name)
            for name in _rtlauncher.CODEX_TOOL_ENV_NAMES
        }
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("codex", user_argv)

    injected = []
    for name, value in observed["environment"].items():
        injected.extend(
            [
                "-c",
                f"shell_environment_policy.set.{name}={_rtlauncher.json.dumps(value)}",
            ]
        )
    assert observed["program"] == str(fake_binary)
    assert observed["command"] == [
        str(fake_binary),
        "--remote",
        "unix://",
        "-C",
        str(project),
        *user_argv,
        *injected,
    ]
    assert observed["command"].count(user_override) == 1
    assert observed["environment"] == {
        "RT_PROJECT_ROOT": str(project),
        "RT_FROM": "codex",
        "RT_SESSION_ID": "session-11",
        "RT_LEASE_REVISION": "11",
        "RT_RUNTIME_DIR": str(custom_runtime),
        "RT_CODEX_RUNTIME_DIR": str(custom_runtime),
    }
    assert launch_order == ["preflight", "claim", "arm"]


def test_unanchored_codex_fails_before_preflight_or_exec(tmp_path, monkeypatch):
    clear_lease_environment(monkeypatch)
    calls = []
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: None)
    monkeypatch.setattr(_rtlauncher, "project_at_or_above", lambda _cwd: None)
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda **_kwargs: calls.append("preflight"),
    )
    monkeypatch.setattr(
        _rtlauncher.os,
        "execv",
        lambda *_args: calls.append("exec"),
    )

    with pytest.raises(_rtlauncher.SelectionError, match="requires a Roundtable project"):
        _rtlauncher.launch("codex", [])

    assert calls == []


def test_codex_does_not_exec_when_binding_intent_cannot_be_armed(
    tmp_path,
    monkeypatch,
):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    calls = []
    claimed = lease(project, "codex")
    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: tmp_path / "codex")
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda _root, _agent_id, _harness: claimed,
    )
    monkeypatch.setattr(
        _rtlauncher,
        "arm_codex_launch_intent",
        lambda _token: (_ for _ in ()).throw(
            _rtlauncher.RuntimeStateError("unsafe runtime")
        ),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action: ready_action(),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "release",
        lambda token: calls.append(("release", token)) or True,
    )
    monkeypatch.setattr(_rtlauncher.os, "execv", lambda *_args: calls.append("exec"))

    with pytest.raises(_rtlauncher.SelectionError, match="could not arm"):
        _rtlauncher.launch("codex", [])

    assert calls == [("release", claimed)]
    assert os.environ.get("RT_FROM") == "codex"
    assert os.environ.get("RT_PROJECT_ROOT") is None
    assert os.environ.get("RT_SESSION_ID") is None
    assert os.environ.get("RT_LEASE_REVISION") is None


def test_codex_reports_arm_and_release_failures_together(
    tmp_path,
    monkeypatch,
):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: tmp_path / "codex")
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, _harness: lease(root, agent_id),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "arm_codex_launch_intent",
        lambda _token: (_ for _ in ()).throw(
            _rtlauncher.RuntimeStateError("arm unsafe")
        ),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "release",
        lambda _token: (_ for _ in ()).throw(OSError("release failed")),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action: ready_action(),
    )

    with pytest.raises(_rtlauncher.SelectionError) as captured:
        _rtlauncher.launch("codex", [])

    assert "arm unsafe" in str(captured.value)
    assert "release failed" in str(captured.value)
    assert os.environ.get("RT_PROJECT_ROOT") is None
    assert os.environ.get("RT_SESSION_ID") is None
    assert os.environ.get("RT_LEASE_REVISION") is None


def test_codex_reports_fenced_release_miss_after_arm_failure(
    tmp_path,
    monkeypatch,
):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: tmp_path / "codex")
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, _harness: lease(root, agent_id),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "arm_codex_launch_intent",
        lambda _token: (_ for _ in ()).throw(
            _rtlauncher.RuntimeStateError("arm unsafe")
        ),
    )
    monkeypatch.setattr(_rtlauncher, "release", lambda _token: False)
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action: ready_action(),
    )

    with pytest.raises(
        _rtlauncher.SelectionError,
        match="no longer releasable",
    ) as captured:
        _rtlauncher.launch("codex", [])

    assert "arm unsafe" in str(captured.value)


def test_codex_injects_reserved_overrides_before_double_dash(monkeypatch):
    environment = {
        "RT_PROJECT_ROOT": "/project",
        "RT_FROM": "codex",
        "RT_SESSION_ID": "session",
        "RT_LEASE_REVISION": "3",
        "RT_RUNTIME_DIR": "/custom/runtime",
        "RT_CODEX_RUNTIME_DIR": "/custom/runtime",
    }
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    result = _rtlauncher.append_codex_seat_overrides(
        ["--model", "gpt-5.6", "--", "literal prompt"]
    )

    separator = result.index("--")
    assert result[separator + 1 :] == ["literal prompt"]
    for name, value in environment.items():
        assert (
            f"shell_environment_policy.set.{name}="
            f"{_rtlauncher.json.dumps(value)}"
        ) in result[:separator]


def test_codex_anchor_is_explicit_and_precedes_double_dash(tmp_path):
    project = (tmp_path / "project").resolve()

    result = _rtlauncher.anchor_codex_project(
        project,
        ["--model", "gpt-5.6", "--", "--cd", "/literal/prompt"],
    )

    assert result == [
        "-C",
        str(project),
        "--model",
        "gpt-5.6",
        "--",
        "--cd",
        "/literal/prompt",
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["-C", "/other"],
        ["-C/other"],
        ["--cd", "/other"],
        ["--cd=/other"],
    ],
)
def test_codex_anchor_rejects_user_working_root_override(tmp_path, argv):
    with pytest.raises(
        _rtlauncher.SelectionError,
        match="-C/--cd is managed by Roundtable",
    ):
        _rtlauncher.anchor_codex_project(tmp_path, argv)


def test_codex_cd_conflict_fails_before_preflight_or_claim(tmp_path, monkeypatch):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    calls = []

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: tmp_path / "codex"
    )
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda **_kwargs: calls.append("preflight"),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda *_args: calls.append("claim"),
    )
    monkeypatch.setattr(_rtlauncher.os, "execv", lambda *_args: calls.append("exec"))

    with pytest.raises(
        _rtlauncher.SelectionError,
        match="-C/--cd is managed by Roundtable",
    ):
        _rtlauncher.launch("codex", ["--cd", "/other"])

    assert calls == []


def _codex_launch_fixture(tmp_path, monkeypatch, user_argv, extra_env=None):
    project = write_project(
        tmp_path / "project", agent_id="codex", harness="codex"
    )
    fake_binary = tmp_path / "codex"
    observed = {}

    clear_lease_environment(monkeypatch)
    monkeypatch.setenv("RT_FROM", "codex")
    monkeypatch.delenv("RT_CODEX_NO_PRIMER", raising=False)
    for name, value in (extra_env or {}).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        _rtlauncher, "choose_launch_cwd", lambda _harness: project
    )
    monkeypatch.setattr(_rtlauncher.os, "chdir", lambda _path: None)
    monkeypatch.setattr(
        _rtlauncher, "harness_bin", lambda _harness: fake_binary
    )
    monkeypatch.setattr(
        _rtlauncher,
        "preflight_codex_services",
        lambda *, ready_action: ready_action(),
    )
    monkeypatch.setattr(
        _rtlauncher,
        "claim",
        lambda root, agent_id, harness: lease(root, agent_id, revision=3),
    )
    monkeypatch.setattr(
        _rtlauncher, "arm_codex_launch_intent", lambda _token: None
    )

    def fake_execv(program, command):
        observed["command"] = command
        raise ExecCalled

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_execv)

    with pytest.raises(ExecCalled):
        _rtlauncher.launch("codex", user_argv)
    return observed["command"]


def test_bare_codex_launch_appends_the_seat_primer(
    tmp_path, monkeypatch, capsys
):
    command = _codex_launch_fixture(tmp_path, monkeypatch, [])

    # The primer is the exact no-action activation text, one argv element,
    # behind the -- separator so Codex reads it as a prompt literal.
    assert command[-2:] == ["--", _rtlauncher.CODEX_SEAT_PRIMER]
    assert command.count(_rtlauncher.CODEX_SEAT_PRIMER) == 1
    assert _rtlauncher.CODEX_SEAT_PRIMER == (
        "[roundtable] Seat activation turn. Do not call tools, inspect "
        "files, or modify the workspace. Reply exactly: ready."
    )
    assert "activation primer skipped" not in capsys.readouterr().err


def test_explicit_codex_arguments_disable_the_primer(
    tmp_path, monkeypatch, capsys
):
    command = _codex_launch_fixture(
        tmp_path, monkeypatch, ["--model", "gpt-5.6"]
    )

    assert _rtlauncher.CODEX_SEAT_PRIMER not in command
    assert command[-2:] != ["--", _rtlauncher.CODEX_SEAT_PRIMER]
    assert "--model" in command and "gpt-5.6" in command
    advisory = capsys.readouterr().err
    assert "IMPORTANT: Codex activation primer skipped" in advisory
    assert "native arguments were supplied" in advisory
    assert "will not arm or bind until its first turn" in advisory
    assert "interact with it once (or resume it)" in advisory


def test_user_prompt_literals_stay_untouched_without_a_primer(
    tmp_path, monkeypatch
):
    command = _codex_launch_fixture(
        tmp_path, monkeypatch, ["--", "explain this repo"]
    )

    assert command.count("--") == 1
    assert command[-1] == "explain this repo"
    assert _rtlauncher.CODEX_SEAT_PRIMER not in command


def test_rt_codex_no_primer_disables_the_bare_primer(
    tmp_path, monkeypatch, capsys
):
    command = _codex_launch_fixture(
        tmp_path, monkeypatch, [], extra_env={"RT_CODEX_NO_PRIMER": "1"}
    )

    assert _rtlauncher.CODEX_SEAT_PRIMER not in command
    assert "--" not in command
    advisory = capsys.readouterr().err
    assert "IMPORTANT: Codex activation primer skipped" in advisory
    assert "RT_CODEX_NO_PRIMER=1" in advisory
    assert "will not arm or bind until its first turn" in advisory
