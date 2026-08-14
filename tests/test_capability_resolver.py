"""Capability binding: native thread identity resolves one exact fenced seat.

These tests cover stage 1 and stage 3 of the capability-binding acceptance:
a tool process that carries only ``CODEX_THREAD_ID`` -- the shape every
daemon-executed Codex tool has -- must reach the exact active seat, and must
reach nothing at all when the lease, binding, project, or thread disagrees.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtcapability
import _rtruntime
from _rtlib import register_project, resolve_project_mailbox


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


wake = load_script("rt-codex-wake", "capability_resolver_wake")


@pytest.fixture
def host(tmp_path, monkeypatch):
    """One isolated host runtime plus project registry."""

    registry = tmp_path / "projects.yaml"
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    for name in (
        "RT_PROJECT_ROOT",
        "RT_FROM",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
        "CODEX_THREAD_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    return {"registry": registry, "runtime": runtime}


def write_project(path: Path) -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n"
        "  claude:\n"
        "    harness: claude\n"
        "    instances:\n"
        "      - id: claude\n"
    )
    register_project(project)
    return project


def thread_payload(project: Path, thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "sessionId": "native-session-1",
        "cwd": str(project),
        "source": "cli",
        "threadSource": None,
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": "idle"},
    }


def bind_seat(
    host: dict,
    project: Path,
    *,
    thread_id: str = "thread-1",
    surface: dict | None = None,
):
    """Claim a Codex seat and complete both capability-binding stages."""

    token = _rtruntime.claim(project, "codex", "codex")
    _rtruntime.record_seat_capability(
        project,
        "codex",
        "codex",
        session_id=token.session_id,
        revision=token.revision,
        surface=surface,
    )
    store = wake.StateStore(host["runtime"] / "rt-codex-wake-state.json")
    store.bind(project, thread_payload(project, thread_id), lease=token)
    binding = store.bindings[str(project)]
    _rtruntime.record_seat_capability(
        project,
        "codex",
        "codex",
        session_id=token.session_id,
        revision=token.revision,
        thread_id=binding["threadId"],
        binding_revision=binding["bindingRevision"],
    )
    return token, store, binding


def tool_environment(host: dict, *, thread_id: str) -> dict[str, str]:
    """The environment a daemon-executed Codex tool actually has.

    No ``RT_*`` fence variables: the app-server owns these processes, so the
    launcher's environment never reaches them.
    """

    return {
        "HOME": os.environ["HOME"],
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RT_PROJECTS_FILE": str(host["registry"]),
        "RT_RUNTIME_DIR": str(host["runtime"]),
        "RT_CODEX_RUNTIME_DIR": str(host["runtime"]),
        "CODEX_THREAD_ID": thread_id,
    }


def run_tool(
    tool: str,
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BIN / tool), *arguments],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def inbox_files(project: Path, agent: str) -> list[Path]:
    mailbox = resolve_project_mailbox(project)
    inbox = mailbox.inbox_dir / agent / "new"
    if not inbox.is_dir():
        return []
    return sorted(path for path in inbox.iterdir() if path.suffix == ".md")


def test_native_thread_resolves_the_exact_active_seat(host, tmp_path):
    project = write_project(tmp_path / "project")
    token, _store, binding = bind_seat(host, project)

    capability = _rtcapability.resolve_codex_capability(project, "thread-1")

    assert capability.agent_id == "codex"
    assert capability.token.session_id == token.session_id
    assert str(capability.token.revision) == str(token.revision)
    assert capability.binding_revision == binding["bindingRevision"]
    assert capability.environment() == {
        "RT_PROJECT_ROOT": str(project),
        "RT_FROM": "codex",
        "RT_SESSION_ID": token.session_id,
        "RT_LEASE_REVISION": str(token.revision),
    }


def test_fenced_send_and_ack_succeed_from_a_daemon_tool_process(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project)
    environment = tool_environment(host, thread_id="thread-1")

    sent = run_tool(
        "rt-say",
        [
            "--fenced",
            "--no-nudge",
            "--expect-reply",
            "30m",
            "claude",
            "question",
            "does capability resolution work",
        ],
        cwd=project,
        env=environment,
    )
    assert sent.returncode == 0, sent.stderr
    assert inbox_files(project, "claude")

    # A reply alarm is per-seat runtime state, so it proves the resolved fence
    # was accepted for a write, not merely for a read.
    expectations = _rtruntime.list_reply_expectations(project, "codex")
    assert [item.peer for item in expectations] == ["claude"]

    inbound = run_tool(
        "rt-say",
        ["--no-nudge", "codex", "fyi", "inbound for the codex seat"],
        cwd=project,
        env={**tool_environment(host, thread_id="thread-1"), "RT_FROM": "claude"},
    )
    assert inbound.returncode == 0, inbound.stderr
    waiting = inbox_files(project, "codex")
    assert len(waiting) == 1
    message_id = waiting[0].stem

    acked = run_tool(
        "rt-ack",
        ["--fenced", message_id],
        cwd=project,
        env=environment,
    )
    assert acked.returncode == 0, acked.stderr
    assert not inbox_files(project, "codex")


def test_superseded_lease_refuses(host, tmp_path):
    project = write_project(tmp_path / "project")
    token, _store, _binding = bind_seat(host, project)
    _rtruntime.release(token)
    _rtruntime.claim(project, "codex", "codex")

    with pytest.raises(_rtcapability.CapabilityError) as error:
        _rtcapability.resolve_codex_capability(project, "thread-1")
    assert "superseded or stale" in str(error.value)


def test_changed_binding_revision_refuses(host, tmp_path):
    project = write_project(tmp_path / "project")
    token, store, binding = bind_seat(host, project)
    # Rebinding the same thread under the same lease mints a new binding
    # revision; a capability record still naming the old one is a conflict.
    store.bind(project, thread_payload(project), lease=token)
    assert store.bindings[str(project)]["bindingRevision"] != binding[
        "bindingRevision"
    ]

    with pytest.raises(_rtcapability.CapabilityError) as error:
        _rtcapability.resolve_codex_capability(project, "thread-1")
    assert "does not match the current binding" in str(error.value)


def test_btw_child_thread_resolves_to_nothing(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project)

    with pytest.raises(_rtcapability.CapabilityError) as error:
        _rtcapability.resolve_codex_capability(project, "btw-child-thread")
    assert "is not the bound Codex seat thread" in str(error.value)


def test_fenced_tool_refuses_for_a_btw_child_thread(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project)

    refused = run_tool(
        "rt-say",
        ["--fenced", "--no-nudge", "claude", "fyi", "from a side conversation"],
        cwd=project,
        env=tool_environment(host, thread_id="btw-child-thread"),
    )
    assert refused.returncode != 0
    assert "not the bound Codex seat thread" in refused.stderr
    assert not inbox_files(project, "claude")


def test_project_mismatch_refuses(host, tmp_path):
    project = write_project(tmp_path / "project")
    other = write_project(tmp_path / "other")
    bind_seat(host, project)

    with pytest.raises(_rtcapability.CapabilityError) as error:
        _rtcapability.resolve_codex_capability(other, "thread-1")
    assert "no Codex thread binding is recorded" in str(error.value)


def test_two_seats_in_different_projects_never_cross(host, tmp_path):
    first = write_project(tmp_path / "first")
    second = write_project(tmp_path / "second")
    first_token, _store, _binding = bind_seat(host, first, thread_id="thread-a")
    second_token, _store, _binding = bind_seat(host, second, thread_id="thread-b")

    resolved_first = _rtcapability.resolve_codex_capability(first, "thread-a")
    resolved_second = _rtcapability.resolve_codex_capability(second, "thread-b")

    assert resolved_first.token.session_id == first_token.session_id
    assert resolved_second.token.session_id == second_token.session_id
    assert resolved_first.token.session_id != resolved_second.token.session_id
    for project, foreign in ((first, "thread-b"), (second, "thread-a")):
        with pytest.raises(_rtcapability.CapabilityError):
            _rtcapability.resolve_codex_capability(project, foreign)


def test_client_origin_does_not_change_the_resolved_seat(host, tmp_path):
    """Stage 3: any client of the exact bound thread is the same seat.

    Desktop joined through ``CODEX_APP_SERVER_USE_LOCAL_DAEMON`` drives the
    same thread over the same daemon, so its tool processes carry the same
    native identity and must resolve to the same fences.  The resolver has no
    notion of client origin by design.
    """

    project = write_project(tmp_path / "project")
    token, store, _binding = bind_seat(host, project)
    # The app-server labels an external client `vscode`; a Desktop join does
    # not change the thread id, its cwd, or the binding.
    desktop_view = thread_payload(project)
    desktop_view.update({"source": "vscode", "threadSource": "user"})
    store.bind(project, desktop_view, lease=token)
    binding = store.bindings[str(project)]
    _rtruntime.record_seat_capability(
        project,
        "codex",
        "codex",
        session_id=token.session_id,
        revision=token.revision,
        thread_id=binding["threadId"],
        binding_revision=binding["bindingRevision"],
    )

    capability = _rtcapability.resolve_codex_capability(project, "thread-1")
    assert capability.token.session_id == token.session_id
    assert str(capability.token.revision) == str(token.revision)


def test_surface_capability_round_trips_with_the_binding(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(
        host,
        project,
        surface={"kind": "herdr", "pane": "pane-7", "workspace": "ws-1"},
    )

    capability = _rtcapability.resolve_codex_capability(project, "thread-1")
    assert capability.surface == {
        "kind": "herdr",
        "pane": "pane-7",
        "workspace": "ws-1",
    }


def test_capability_record_rejects_environment_shaped_fields(host, tmp_path):
    project = write_project(tmp_path / "project")
    token = _rtruntime.claim(project, "codex", "codex")

    with pytest.raises(_rtruntime.RuntimeStateError):
        _rtruntime.record_seat_capability(
            project,
            "codex",
            "codex",
            session_id=token.session_id,
            revision=token.revision,
            surface={
                "kind": "herdr",
                "pane": "pane-7",
                "HOME": "/home/example",
                "PATH": "/usr/bin",
            },
        )


def test_released_lease_drops_its_capability_record(host, tmp_path):
    project = write_project(tmp_path / "project")
    token, _store, _binding = bind_seat(host, project)
    assert _rtruntime.read_seat_capability(project, "codex") is not None

    assert _rtruntime.release(token) is True
    assert _rtruntime.read_seat_capability(project, "codex") is None


def test_legacy_binding_without_lease_identity_refuses(host, tmp_path):
    project = write_project(tmp_path / "project")
    _rtruntime.claim(project, "codex", "codex")
    state_path = host["runtime"] / "rt-codex-wake-state.json"
    store = wake.StateStore(state_path)
    store.bind(project, thread_payload(project))

    with pytest.raises(_rtcapability.CapabilityError) as error:
        _rtcapability.resolve_codex_capability(project, "thread-1")
    assert "carries no lease identity" in str(error.value)


def test_binding_state_written_by_another_user_is_refused(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project)
    state_path = host["runtime"] / "rt-codex-wake-state.json"
    payload = json.loads(state_path.read_text())
    payload["schema"] = "roundtable.codex-wake.v0"
    state_path.write_text(json.dumps(payload))

    with pytest.raises(_rtcapability.CapabilityError) as error:
        _rtcapability.resolve_codex_capability(project, "thread-1")
    assert "invalid Codex binding state schema" in str(error.value)


def test_rt_surface_drives_the_recorded_pane_from_a_daemon_tool_process(
    host,
    tmp_path,
):
    project = write_project(tmp_path / "project")
    bind_seat(
        host,
        project,
        surface={"kind": "herdr", "pane": "pane-7", "endpoint": "/tmp/herdr.sock"},
    )
    broker = tmp_path / "herdr-broker"
    broker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$RT_TEST_BROKER_LOG"\n'
        "exit 0\n"
    )
    broker.chmod(0o755)
    log = tmp_path / "broker.log"
    environment = {
        **tool_environment(host, thread_id="thread-1"),
        "RT_HERDR_BROKER": str(broker),
        "RT_TEST_BROKER_LOG": str(log),
    }

    shown = run_tool("rt-surface", ["show"], cwd=project, env=environment)
    assert shown.returncode == 0, shown.stderr
    assert "kind=herdr address=--pane pane-7" in shown.stdout

    probed = run_tool("rt-surface", ["probe"], cwd=project, env=environment)
    assert probed.returncode == 0, probed.stderr
    assert log.read_text().splitlines() == ["pane layout --pane pane-7"]

    driven = run_tool(
        "rt-surface",
        ["run", "--", "pane", "run", "{surface}", "echo hello"],
        cwd=project,
        env=environment,
    )
    assert driven.returncode == 0, driven.stderr
    assert log.read_text().splitlines()[-1] == (
        "pane run --pane pane-7 echo hello"
    )


def test_rt_surface_refuses_ambient_addressing(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project, surface={"kind": "herdr", "pane": "pane-7"})

    refused = run_tool(
        "rt-surface",
        ["run", "--", "pane", "run", "--current", "{surface}", "echo hello"],
        cwd=project,
        env=tool_environment(host, thread_id="thread-1"),
    )
    assert refused.returncode != 0
    assert "ambient surface addressing is refused" in refused.stderr


def test_rt_surface_refuses_a_thread_that_is_not_the_seat(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project, surface={"kind": "herdr", "pane": "pane-7"})

    refused = run_tool(
        "rt-surface",
        ["show"],
        cwd=project,
        env=tool_environment(host, thread_id="btw-child-thread"),
    )
    assert refused.returncode != 0
    assert "not the bound Codex seat thread" in refused.stderr


def test_rt_surface_reports_a_seat_without_a_recorded_surface(host, tmp_path):
    project = write_project(tmp_path / "project")
    bind_seat(host, project)

    result = run_tool(
        "rt-surface",
        ["show"],
        cwd=project,
        env=tool_environment(host, thread_id="thread-1"),
    )
    assert result.returncode != 0
    assert "no recorded surface capability" in result.stderr
