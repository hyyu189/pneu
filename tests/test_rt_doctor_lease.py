from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


doctor = load_script("rt-doctor", "rt_doctor_lease")
import _rtruntime
import _rtlib


@pytest.fixture(autouse=True)
def isolated_project_registry(tmp_path, monkeypatch) -> Path:
    registry = tmp_path / "projects.json"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    return registry


def write_project(path: Path, agents: list[tuple[str, str]]) -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    lines = [
        "schema: roundtable.agents.v1",
        f"project: {project}",
        "agents:",
    ]
    for agent_id, harness in agents:
        lines.extend(
            [
                f"  {agent_id}:",
                f"    harness: {harness}",
                "    instances:",
                f"      - id: {agent_id}",
            ]
        )
    (state / "agents.yaml").write_text("\n".join(lines) + "\n")
    return project


def write_registry(path: Path, projects: list[Path]) -> Path:
    assert path.resolve() == Path(os.environ["RT_PROJECTS_FILE"]).resolve()
    for project in projects:
        _rtlib.register_project(
            project,
            path=path,
            registered_at="2026-07-19T00:00:00Z",
        )
    return path


def write_wake_state(path: Path, bindings: dict | None = None) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema": "roundtable.codex-wake.v1",
                "bindings": bindings or {},
                "projects": {},
            }
        )
        + "\n"
    )
    return path


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/python /managed/rt-wait-inbox claude 45",
        "/usr/bin/python /managed/rt-wait-inbox --claude-hook",
        "/usr/bin/python /managed/rt-wait-inbox --claude-stop-hook",
    ],
)
def test_doctor_accepts_supported_tripwire_command_shapes(
    monkeypatch,
    command,
):
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=command + "\n",
        ),
    )

    assert doctor.tripwire_process(123, "claude") == (True, command)


@pytest.mark.parametrize(
    "command",
    [
        "/managed/rt-wait-inbox",
        "/managed/rt-wait-inbox hermes",
        "/managed/not-rt-wait-inbox claude",
        "/bin/sh -c 'echo rt-wait-inbox claude'",
    ],
)
def test_doctor_rejects_unrelated_tripwire_command_shapes(
    monkeypatch,
    command,
):
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=command + "\n",
        ),
    )

    assert doctor.tripwire_process(123, "claude") == (False, command)


def write_bridge_heartbeat(
    runtime: Path,
    socket_path: Path,
    *,
    last_error=None,
    last_rpc_ok_at: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    (runtime / "rt-codex-wake.pid").write_text("123\n")
    (runtime / "rt-codex-wake-heartbeat.json").write_text(
        json.dumps(
            {
                "schema": "roundtable.codex-wake-heartbeat.v1",
                "pid": 123,
                "bridgeBuildFingerprint": doctor.wake_bridge_build_fingerprint(),
                "heartbeatAt": now,
                "lastRpcOkAt": (
                    now if last_rpc_ok_at is None else last_rpc_ok_at
                ),
                "lastError": last_error,
                "socketPath": str(socket_path),
                "projects": [],
            }
        )
        + "\n"
    )


def inspection(status: str, *, record=None, detail: str | None = None):
    return SimpleNamespace(
        status=status,
        record=record,
        detail=detail or status,
        heartbeat_age=0.0 if status == "active_healthy" else None,
        wake_healthy=status == "active_healthy",
        adapter_healthy=status == "active_healthy",
    )


def test_doctor_reports_every_configured_seat_and_legacy_markers(
    tmp_path, monkeypatch, capsys
):
    configured = [
        ("healthy", "claude-code"),
        ("unhealthy", "claude-code"),
        ("stale", "hermes-agent"),
        ("ambiguous", "codex"),
    ]
    project = write_project(tmp_path / "project", configured)
    registry = write_registry(tmp_path / "projects.json", [project])
    state_file = write_wake_state(tmp_path / "wake-state.json")
    mailbox = _rtlib.resolve_project_mailbox(project, registry)
    assert mailbox.layout == "local"
    marker = mailbox.inbox_dir / "healthy" / ".armed-123"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    statuses = {
        "healthy": inspection("active_healthy", record={"agentId": "healthy"}),
        "unhealthy": inspection(
            "active_unhealthy", record={"agentId": "unhealthy"}
        ),
        "stale": inspection("stale", record={"agentId": "stale"}),
        "ambiguous": inspection(
            "ambiguous", record={"agentId": "ambiguous"}
        ),
    }
    calls = []

    def fake_inspect(root, agent_id):
        calls.append((root, agent_id))
        return statuses[agent_id]

    monkeypatch.setattr(doctor, "inspect_seat", fake_inspect)
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        registry,
        state_file,
        tmp_path / "app.sock",
        rpc_ok=False,
    )

    output = capsys.readouterr().out
    assert calls == [(project, agent_id) for agent_id, _harness in configured]
    assert "OK seat:" in output and "agent=healthy" in output
    assert "WARN seat:" in output and "agent=unhealthy" in output
    assert "agent=stale" in output and f"RT_FROM=stale rt-hermes" in output
    assert "FAIL seat:" in output and "agent=ambiguous" in output
    assert f"WARN legacy-tripwire-marker: {marker}" in output
    assert "tripwire-anchor" not in output
    assert report.failed


def test_doctor_distinguishes_registry_tombstone_and_orphan(
    tmp_path,
    monkeypatch,
    capsys,
):
    tombstoned = write_project(
        tmp_path / "tombstoned", [("claude", "claude-code")]
    )
    orphan = write_project(
        tmp_path / "orphan", [("hermes", "hermes-agent")]
    )
    registry = write_registry(
        tmp_path / "projects.json",
        [tombstoned, orphan],
    )
    tombstoned_uuid = _rtlib._read_project_identity(tombstoned)
    orphan_uuid = _rtlib._read_project_identity(orphan)
    assert _rtlib.unregister_project(tombstoned, path=registry)
    orphan.rename(tmp_path / "orphan-archived")
    state_file = write_wake_state(tmp_path / "wake-state.json")
    monkeypatch.setattr(
        doctor,
        "inspect_project_seats",
        lambda _report, _registered: ({}, []),
    )
    monkeypatch.setattr(
        doctor,
        "report_legacy_markers",
        lambda *_args, **_kwargs: None,
    )
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        registry,
        state_file,
        tmp_path / "app.sock",
        rpc_ok=False,
    )

    output = capsys.readouterr().out
    assert "WARN registry-tombstone:" in output
    assert f"uuid={tombstoned_uuid}" in output
    assert f"last_path={tombstoned}" in output
    assert "WARN registry-orphan:" in output
    assert f"uuid={orphan_uuid}" in output
    assert f"path={orphan}" in output


def test_doctor_checks_owner_and_watcher_process_anchors(
    tmp_path, monkeypatch, capsys
):
    project = write_project(
        tmp_path / "project", [("claude", "claude-code")]
    )
    wrong = write_project(tmp_path / "wrong", [("other", "claude-code")])
    registry = write_registry(tmp_path / "projects.json", [project])
    state_file = write_wake_state(tmp_path / "wake-state.json")
    record = {
        "projectRoot": str(project),
        "agentId": "claude",
        "ownerPid": 101,
        "wake": {"watcherPid": 202},
    }
    monkeypatch.setattr(
        doctor,
        "inspect_seat",
        lambda _root, _agent: inspection("active_healthy", record=record),
    )
    monkeypatch.setattr(doctor, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        doctor,
        "process_cwd",
        lambda pid: project if pid == 101 else wrong,
    )
    monkeypatch.setattr(
        doctor,
        "tripwire_process",
        lambda _pid, agent: (True, f"rt-wait-inbox {agent}"),
    )
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        registry,
        state_file,
        tmp_path / "app.sock",
        rpc_ok=False,
    )

    output = capsys.readouterr().out
    assert f"OK seat-owner-anchor: project={project} agent=claude" in output
    assert f"FAIL seat-watcher-anchor: project={project} agent=claude" in output
    assert f"cwd={wrong}" in output
    assert report.failed


@pytest.mark.parametrize(
    ("binding_revision", "expected_level"),
    [("revision-4", "OK"), ("revision-5", "FAIL")],
)
def test_codex_binding_must_match_current_roundtable_lease(
    tmp_path, monkeypatch, capsys, binding_revision, expected_level
):
    project = write_project(tmp_path / "project", [("codex", "codex")])
    registry = write_registry(tmp_path / "projects.json", [project])
    binding = {
        "agent": "codex",
        "project": str(project),
        "threadId": "thread-1",
        "roundtableSessionId": "rt-session",
        "leaseRevision": binding_revision,
    }
    state_file = write_wake_state(
        tmp_path / "wake-state.json", {str(project): binding}
    )
    record = {
        "projectRoot": str(project),
        "agentId": "codex",
        "sessionId": "rt-session",
        "revision": "revision-4",
    }
    monkeypatch.setattr(
        doctor,
        "inspect_seat",
        lambda _root, _agent: inspection("active_healthy", record=record),
    )
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        registry,
        state_file,
        tmp_path / "app.sock",
        rpc_ok=False,
    )

    output = capsys.readouterr().out
    assert (
        f"{expected_level} codex-anchor: project={project} binding lease"
        in output
    )
    assert report.failed is (expected_level == "FAIL")


@pytest.mark.parametrize(
    ("status", "runtime_record", "expected_level"),
    [
        ("vacant", None, "WARN"),
        ("ambiguous", None, "FAIL"),
        ("stale", {"sessionId": "old", "revision": "old-revision"}, "FAIL"),
    ],
)
def test_legacy_codex_binding_only_works_without_runtime_record(
    tmp_path, monkeypatch, capsys, status, runtime_record, expected_level
):
    project = write_project(tmp_path / "project", [("codex", "codex")])
    registry = write_registry(tmp_path / "projects.json", [project])
    state_file = write_wake_state(
        tmp_path / "wake-state.json",
        {
            str(project): {
                "agent": "codex",
                "project": str(project),
                "threadId": "thread-legacy",
            }
        },
    )
    monkeypatch.setattr(
        doctor,
        "inspect_seat",
        lambda _root, _agent: inspection(status, record=runtime_record),
    )
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        registry,
        state_file,
        tmp_path / "app.sock",
        rpc_ok=False,
    )

    output = capsys.readouterr().out
    assert (
        f"{expected_level} codex-anchor: project={project} binding "
        in output
    )
    assert report.failed is (expected_level == "FAIL")


def test_legacy_binding_fails_when_custom_codex_seat_has_runtime_record(
    tmp_path, monkeypatch, capsys
):
    project = write_project(
        tmp_path / "project",
        [("codex", "codex"), ("codex-review", "codex")],
    )
    registry = write_registry(tmp_path / "projects.json", [project])
    state_file = write_wake_state(
        tmp_path / "wake-state.json",
        {
            str(project): {
                "agent": "codex",
                "project": str(project),
                "threadId": "thread-legacy",
            }
        },
    )
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    _rtruntime.claim(project, "codex-review", "codex")
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        registry,
        state_file,
        tmp_path / "app.sock",
        rpc_ok=False,
    )

    output = capsys.readouterr().out
    assert (
        f"FAIL codex-anchor: project={project} binding lacks Roundtable "
        "lease identity while Codex runtime state exists"
    ) in output
    assert report.failed


def test_doctor_runtime_override_sets_and_restores_both_aliases(
    tmp_path, monkeypatch
):
    selected = (tmp_path / "selected").resolve()
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "generic"))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(tmp_path / "legacy"))
    observed = {}

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rt-doctor",
            "--runtime-dir",
            str(selected),
            "--registry",
            str(tmp_path / "missing-registry.json"),
        ],
    )
    monkeypatch.setattr(doctor, "codex_version", lambda: (None, "missing"))
    monkeypatch.setattr(doctor, "daemon_version", lambda _socket: (None, "missing"))
    monkeypatch.setattr(doctor, "launchd_loaded", lambda _label: False)
    monkeypatch.setattr(doctor, "bridge_check", lambda *_args: (False, "missing"))
    monkeypatch.setattr(
        doctor,
        "project_health_checks",
        lambda *_args: observed.update(
            generic=os.environ.get("RT_RUNTIME_DIR"),
            legacy=os.environ.get("RT_CODEX_RUNTIME_DIR"),
        ),
    )

    doctor.main()

    assert observed == {
        "generic": str(selected),
        "legacy": str(selected),
    }
    assert os.environ["RT_RUNTIME_DIR"] == str(tmp_path / "generic")
    assert os.environ["RT_CODEX_RUNTIME_DIR"] == str(tmp_path / "legacy")


def test_fresh_bridge_heartbeat_with_error_is_not_healthy(
    tmp_path, monkeypatch
):
    socket_path = tmp_path / "app.sock"
    write_bridge_heartbeat(
        tmp_path,
        socket_path,
        last_error="unsupported Codex protocol",
        last_rpc_ok_at="",
    )
    monkeypatch.setattr(
        doctor,
        "pid_is_running",
        lambda *_args: (True, "pid 123"),
    )

    ok, detail = doctor.bridge_check(tmp_path, 15, socket_path)

    assert not ok
    assert detail == "bridge reports error: unsupported Codex protocol"


def test_bridge_health_requires_a_fresh_successful_rpc(
    tmp_path, monkeypatch
):
    socket_path = tmp_path / "app.sock"
    write_bridge_heartbeat(
        tmp_path,
        socket_path,
        last_rpc_ok_at="",
    )
    monkeypatch.setattr(
        doctor,
        "pid_is_running",
        lambda *_args: (True, "pid 123"),
    )

    ok, detail = doctor.bridge_check(tmp_path, 15, socket_path)

    assert not ok
    assert "last successful bridge RPC timestamp" in detail


def test_bridge_health_requires_the_requested_socket(
    tmp_path, monkeypatch
):
    socket_path = tmp_path / "app.sock"
    write_bridge_heartbeat(tmp_path, tmp_path / "other.sock")
    monkeypatch.setattr(
        doctor,
        "pid_is_running",
        lambda *_args: (True, "pid 123"),
    )

    ok, detail = doctor.bridge_check(tmp_path, 15, socket_path)

    assert not ok
    assert "heartbeat socket" in detail
