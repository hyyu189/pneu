from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlib  # noqa: E402


def load_doctor():
    name = "rt_doctor_diagnostics"
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / "rt-doctor"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


doctor = load_doctor()


def test_doctor_reports_each_rc_host_on_one_report_only_line(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor,
        "iter_states",
        lambda: [{"projectName": "alpha"}, {"projectName": "beta"}],
    )
    values = iter(
        [
            SimpleNamespace(
                healthy=True,
                loaded=True,
                process_alive=True,
                pid=101,
                last_registration={
                    "at": "2026-08-10T12:00:00.000Z",
                    "projectRoot": "/tmp/alpha-worktree/phone",
                },
            ),
            SimpleNamespace(
                healthy=False,
                loaded=False,
                process_alive=False,
                pid=None,
                last_registration=None,
            ),
        ]
    )
    monkeypatch.setattr(doctor, "status_from_state", lambda _state: next(values))

    doctor.report_rc_hosts()

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("OK rc-host[alpha]: launchd=loaded process=alive pid=101")
    assert "last-registration=2026-08-10T12:00:00.000Z" in lines[0]
    assert lines[1].startswith("WARN rc-host[beta]: launchd=not-loaded process=not-running")
    assert "last-registration=never" in lines[1]


def _registered_project(path: Path, registry: Path) -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        """schema: roundtable.agents.v1
project: "."
agents:
  codex:
    harness: codex
    instances:
      - id: codex
"""
    )
    (state / ".gitignore").write_text(
        "project.json\ninbox/\nmessages/\nlocks/\n"
    )
    (state / "inbox").mkdir()
    (state / "messages").mkdir()
    (state / "locks").mkdir()
    _rtlib.register_project(project, path=registry)
    return project


def _registered_grok_project(path: Path, registry: Path) -> Path:
    project = _registered_project(path, registry)
    (project / ".roundtable" / "agents.yaml").write_text(
        """schema: roundtable.agents.v1
project: "."
agents:
  grok:
    harness: grok-build
    instances:
      - id: grok
"""
    )
    return project


@pytest.mark.parametrize(
    ("fixture", "expected_level", "expected_state"),
    [
        ("present", "OK", "present"),
        ("absent", "WARN", "absent"),
        ("unreadable", "WARN", "unreadable"),
    ],
)
def test_grok_monitor_advisory_uses_bounded_session_fixtures(
    tmp_path,
    capsys,
    fixture,
    expected_level,
    expected_state,
):
    registry = tmp_path / "registry" / "projects.yaml"
    project = _registered_grok_project(tmp_path / "grok-project", registry)
    mailbox = _rtlib.resolve_project_mailbox(project, registry)
    expected_maildir = mailbox.inbox_dir / "grok" / "new"
    sessions = tmp_path / "fixture-home" / ".grok" / "sessions"
    session = sessions / "session-1"
    session.mkdir(parents=True)
    evidence = session / "updates.jsonl"
    if fixture == "present":
        evidence.write_text(
            json.dumps(
                {
                    "hookEventName": "Stop",
                    "backgroundTasks": [
                        {
                            "tool": "monitor",
                            "persistent": True,
                            "watch": str(expected_maildir),
                        }
                    ],
                }
            )
            + "\n"
        )
    elif fixture == "absent":
        evidence.write_text(
            json.dumps({"hookEventName": "Stop", "backgroundTasks": []}) + "\n"
        )
    else:
        evidence.write_bytes(b"\xffnot-utf8")
    report = doctor.Report()
    inspections = {
        (str(project), "grok"): {
            "status": "active_healthy",
            "record": {"agentId": "grok", "harness": "grok"},
        }
    }

    doctor.report_grok_monitor_liveness(
        report,
        {str(project): project},
        inspections,
        registry,
        sessions,
    )

    output = capsys.readouterr().out
    assert f"{expected_level} grok-monitor:" in output
    assert f"evidence={expected_state}" in output
    assert str(project) in output
    if fixture == "present":
        assert "report-only; session evidence is not a lease" in output
    else:
        assert "re-arm its persistent pneu mailbox monitor" in output
        assert "resume always requires one re-arm turn" in output
    assert not report.failed


def test_native_grok_owner_health_defers_only_monitor_health_to_advisory(
    tmp_path,
    monkeypatch,
    capsys,
):
    project = tmp_path / "grok-project"
    project.mkdir()
    monkeypatch.setattr(
        doctor,
        "configured_instances",
        lambda _project: [("grok", "grok-build")],
    )
    monkeypatch.setattr(
        doctor,
        "inspect_seat",
        lambda *_args: {
            "status": "active_unhealthy",
            "detail": "owner pid 123 is live; wake adapter has no heartbeat",
            "record": {"agentId": "grok", "harness": "grok"},
        },
    )
    report = doctor.Report()

    inspections, codex_instances = doctor.inspect_project_seats(
        report,
        {str(project): project},
    )

    output = capsys.readouterr().out
    assert "OK seat:" in output
    assert "status=active_owner" in output
    assert "monitor-health=reported-separately" in output
    assert "restart the wake adapter" not in output
    assert (str(project), "grok") in inspections
    assert codex_instances == {str(project): set()}
    assert not report.failed


def test_legacy_marker_scan_reports_busy_project_and_continues(
    tmp_path,
    capsys,
):
    registry = tmp_path / "registry" / "projects.yaml"
    busy = _registered_project(tmp_path / "busy", registry)
    visible = _registered_project(tmp_path / "visible", registry)
    marker = (
        visible
        / ".roundtable"
        / "inbox"
        / "codex"
        / ".armed-legacy"
    )
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    report = doctor.Report()

    with _rtlib.locked_project_mailbox_checked(
        busy,
        registry_path=registry,
        exclusive=True,
    ):
        doctor.report_legacy_markers(
            report,
            {"busy": busy, "visible": visible},
            registry,
        )

    output = capsys.readouterr().out
    assert "WARN mailbox-layout-busy" in output
    assert str(marker) in output


def bind_request(runtime: Path, created_at: datetime) -> Path:
    queue = runtime / doctor.BIND_REQUESTS_DIRNAME
    queue.mkdir(parents=True, mode=0o700)
    os.chmod(runtime, 0o700)
    os.chmod(queue, 0o700)
    payload = {
        "schema": doctor.BIND_REQUEST_SCHEMA,
        "hookEventName": "SessionStart",
        "source": "startup",
        "threadId": "thread-1",
        "projectRoot": "/tmp/example-project",
        "agentId": "codex",
        "roundtableSessionId": "roundtable-session-1",
        "leaseRevision": "lease-revision-1",
        "createdAt": created_at.isoformat().replace("+00:00", "Z"),
    }
    identity = "\0".join(
        payload[name]
        for name in (
            "projectRoot",
            "agentId",
            "roundtableSessionId",
            "leaseRevision",
        )
    )
    name = hashlib.sha256(identity.encode()).hexdigest() + ".json"
    path = queue / name
    path.write_text(json.dumps(payload) + "\n")
    os.chmod(path, 0o600)
    return path


def test_doctor_skips_codex_services_but_keeps_runtime_checks_without_codex(
    tmp_path,
    monkeypatch,
    capsys,
):
    observed: list[str] = []
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rt-doctor",
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--registry",
            str(tmp_path / "projects.yaml"),
            "--prefix",
            str(tmp_path / "prefix"),
            "--home",
            str(tmp_path / "home"),
        ],
    )
    monkeypatch.setattr(
        doctor,
        "_codex_setup_status",
        lambda *_a: (
            0,
            {
                "ok": True,
                "harnesses": {"codex": {"state": "not_configured"}},
            },
        ),
    )
    monkeypatch.setattr(
        doctor,
        "codex_version",
        lambda: (_ for _ in ()).throw(RuntimeError("no Codex executable")),
    )
    monkeypatch.setattr(
        doctor,
        "daemon_version",
        lambda *_a: pytest.fail("daemon resolver must be skipped"),
    )
    monkeypatch.setattr(
        doctor,
        "socket_check",
        lambda *_a: pytest.fail("Codex socket check must be skipped"),
    )
    monkeypatch.setattr(
        doctor,
        "probe_handshake",
        lambda *_a: pytest.fail("Codex RPC check must be skipped"),
    )
    monkeypatch.setattr(
        doctor,
        "bridge_check",
        lambda *_a: pytest.fail("Codex bridge check must be skipped"),
    )
    monkeypatch.setattr(
        doctor,
        "report_bind_request_queue",
        lambda *_a, **_k: observed.append("bind-queue"),
    )
    monkeypatch.setattr(
        doctor,
        "project_health_checks",
        lambda *_a, **_k: observed.append("project-health"),
    )
    monkeypatch.setattr(
        doctor,
        "report_hook_trust",
        lambda *_a, **_k: observed.append("hook-trust"),
    )

    code = doctor.main()

    output = capsys.readouterr().out
    assert code == 0
    assert "WARN codex-setup:" in output
    assert "WARN codex-cli: Codex resolver unavailable" in output
    assert "SKIP daemon:" in output
    assert "SKIP bridge:" in output
    assert observed == ["bind-queue", "project-health", "hook-trust"]


@pytest.mark.parametrize(
    ("reported_socket", "expected_detail"),
    [
        (None, "not owned by the Roundtable LaunchAgent"),
        ([], "reported socket []"),
    ],
)
def test_doctor_fails_daemon_when_roundtable_owner_is_unproven_or_malformed(
    tmp_path,
    monkeypatch,
    capsys,
    reported_socket,
    expected_detail,
):
    socket_path = tmp_path / "app.sock"
    daemon = {
        "status": "running",
        "socketPath": str(socket_path) if reported_socket is None else reported_socket,
        "managedCodexPath": "/tmp/old-codex",
        "managedCodexVersion": None,
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rt-doctor",
            "--socket",
            str(socket_path),
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--registry",
            str(tmp_path / "projects.yaml"),
        ],
    )
    monkeypatch.setattr(doctor, "report_codex_setup", lambda *_a: None)
    monkeypatch.setattr(
        doctor,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(doctor, "daemon_version", lambda *_a: (daemon, ""))
    monkeypatch.setattr(
        doctor,
        "require_daemon_identity",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError(
                "Unix socket peer is not owned by the Roundtable LaunchAgent process tree"
            )
        ),
    )
    monkeypatch.setattr(doctor, "socket_check", lambda *_a: (True, "safe"))
    monkeypatch.setattr(doctor, "probe_handshake", lambda *_a: (True, "ready"))
    monkeypatch.setattr(doctor, "bridge_check", lambda *_a: (True, "healthy"))
    monkeypatch.setattr(doctor, "report_bind_request_queue", lambda *_a, **_k: None)
    monkeypatch.setattr(doctor, "project_health_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(doctor, "report_hook_trust", lambda *_a, **_k: None)

    code = doctor.main()

    output = capsys.readouterr().out
    assert code == 1
    assert "FAIL daemon:" in output
    assert expected_detail in output


@pytest.mark.parametrize(
    ("result", "expected", "failed"),
    [
        (
            {
                "ok": True,
                "harnesses": {"codex": {"state": "configured"}},
            },
            "match the recorded ownership",
            False,
        ),
        (
            {
                "ok": True,
                "harnesses": {"codex": {"state": "not_configured"}},
            },
            "not configured by this Roundtable installation",
            False,
        ),
        (
            {
                "ok": True,
                "harnesses": {
                    "codex": {
                        "state": "upgrade_required",
                        "actions": ["merge the managed Codex hooks file"],
                    }
                },
            },
            "Codex setup upgrade is required",
            True,
        ),
        (
            {
                "ok": False,
                "error": "managed Codex SessionStart hook drift",
            },
            "managed Codex SessionStart hook drift",
            True,
        ),
    ],
)
def test_setup_diagnostic_translates_authoritative_read_only_status(
    tmp_path, monkeypatch, capsys, result, expected, failed
):
    code = 0 if result["ok"] else 2
    monkeypatch.setattr(doctor, "_codex_setup_status", lambda *_args: (code, result))
    report = doctor.Report()

    doctor.report_codex_setup(report, tmp_path / "prefix", tmp_path / "home")

    assert expected in capsys.readouterr().out
    assert report.failed is failed


def test_setup_diagnostic_skips_developer_invocation_without_touching_home(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        doctor,
        "_codex_setup_status",
        lambda *_args: pytest.fail("setup status must not run without a prefix"),
    )
    report = doctor.Report()

    doctor.report_codex_setup(report, None, tmp_path / "home")

    assert "ownership was not checked" in capsys.readouterr().out
    assert not report.failed
    assert not (tmp_path / "home").exists()


def test_auto_bind_queue_reports_fresh_request_without_mutating_it(
    tmp_path, capsys
):
    now = datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc)
    request = bind_request(tmp_path / "runtime", now - timedelta(seconds=4))
    before = request.read_bytes()
    report = doctor.Report()

    doctor.report_bind_request_queue(report, tmp_path / "runtime", 30, now=now)

    output = capsys.readouterr().out
    assert "WARN auto-bind-queue:" in output
    assert "fresh request" in output
    assert "age=4.0s" in output
    assert not report.failed
    assert request.read_bytes() == before


def test_auto_bind_queue_reports_stale_request_without_expiring_it(
    tmp_path, capsys
):
    now = datetime(2026, 7, 20, 20, 0, tzinfo=timezone.utc)
    request = bind_request(tmp_path / "runtime", now - timedelta(seconds=31))
    before = request.read_bytes()
    report = doctor.Report()

    doctor.report_bind_request_queue(report, tmp_path / "runtime", 30, now=now)

    output = capsys.readouterr().out
    assert "FAIL auto-bind-queue:" in output
    assert "older than 30.0s" in output
    assert "bridge will safely accept or reject" in output
    assert report.failed
    assert request.read_bytes() == before


def test_auto_bind_queue_rejects_unsafe_directory_without_following_it(
    tmp_path, capsys
):
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    runtime.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (runtime / doctor.BIND_REQUESTS_DIRNAME).symlink_to(
        outside, target_is_directory=True
    )
    report = doctor.Report()

    doctor.report_bind_request_queue(report, runtime, 30)

    assert "unsafe request directory" in capsys.readouterr().out
    assert report.failed
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("reported", [None, "sha256:stale"])
def test_bridge_check_rejects_missing_or_stale_build_fingerprint(
    tmp_path, monkeypatch, reported
):
    now = datetime.now(timezone.utc).isoformat()
    socket_path = tmp_path / "app.sock"
    (tmp_path / "rt-codex-wake.pid").write_text("123\n")
    heartbeat = {
        "schema": "roundtable.codex-wake-heartbeat.v1",
        "pid": 123,
        "heartbeatAt": now,
        "lastRpcOkAt": now,
        "lastError": None,
        "socketPath": str(socket_path),
    }
    if reported is not None:
        heartbeat["bridgeBuildFingerprint"] = reported
    (tmp_path / "rt-codex-wake-heartbeat.json").write_text(json.dumps(heartbeat))
    monkeypatch.setattr(doctor, "pid_is_running", lambda *_args: (True, "pid 123"))
    monkeypatch.setattr(
        doctor,
        "wake_bridge_build_fingerprint",
        lambda: "sha256:current",
    )

    ok, detail = doctor.bridge_check(tmp_path, 15, socket_path)

    assert not ok
    assert "build fingerprint is stale or invalid" in detail
    assert "expected=sha256:current" in detail


class HookClient:
    response = None
    calls = []
    closed = False

    def __init__(self, _socket):
        type(self).calls = []
        type(self).closed = False

    def request(self, method, params):
        type(self).calls.append((method, params))
        return type(self).response

    def close(self):
        type(self).closed = True


@pytest.mark.parametrize(
    ("trust_status", "level", "text"),
    [
        ("managed", "OK", "all managed or trusted"),
        ("trusted", "OK", "all managed or trusted"),
        ("untrusted", "FAIL", "wake is blocked pending hook review"),
        ("modified", "FAIL", "wake is blocked pending hook review"),
        ("future-status", "FAIL", "unknown enabled hook trust state"),
    ],
)
def test_hook_trust_diagnostic_matches_bridge_gate(
    tmp_path, monkeypatch, capsys, trust_status, level, text
):
    project = (tmp_path / "project").resolve()
    project.mkdir()
    HookClient.response = {
        "data": [
            {
                "cwd": str(project),
                "hooks": [
                    {
                        "key": "user:session_start:0:0",
                        "enabled": True,
                        "trustStatus": trust_status,
                    }
                ],
                "warnings": [],
                "errors": [],
            }
        ]
    }
    monkeypatch.setattr(doctor, "_configured_codex_projects", lambda _path: [project])
    monkeypatch.setattr(doctor, "AppServerClient", HookClient)
    report = doctor.Report()

    doctor.report_hook_trust(
        report, tmp_path / "projects.json", tmp_path / "app.sock", True
    )

    output = capsys.readouterr().out
    assert f"{level} hook-trust:" in output
    assert text in output
    assert report.failed is (level == "FAIL")
    assert HookClient.calls == [
        ("hooks/list", {"cwds": [str(project)]})
    ]
    assert HookClient.closed


def test_hook_trust_diagnostic_never_connects_when_rpc_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    project = (tmp_path / "project").resolve()
    monkeypatch.setattr(doctor, "_configured_codex_projects", lambda _path: [project])
    monkeypatch.setattr(
        doctor,
        "AppServerClient",
        lambda _socket: pytest.fail("client must not connect when RPC is down"),
    )
    report = doctor.Report()

    doctor.report_hook_trust(
        report, tmp_path / "projects.json", tmp_path / "app.sock", False
    )

    assert "unchecked for 1 Codex project" in capsys.readouterr().out
    assert not report.failed


def test_queue_files_remain_private_in_fixture(tmp_path):
    request = bind_request(
        tmp_path / "runtime", datetime.now(timezone.utc)
    )

    assert stat.S_IMODE(request.stat().st_mode) == 0o600
    assert stat.S_IMODE(request.parent.stat().st_mode) == 0o700


def _stub_codex_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str = "0.145.0",
) -> Path:
    """Stub one identity-proven daemon on `version` for the main() report."""

    socket_path = tmp_path / "app.sock"
    parsed = tuple(int(part) for part in version.split("."))
    daemon = {
        "status": "running",
        "socketPath": str(socket_path),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": version,
        "cliVersion": version,
        "appServerVersion": version,
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rt-doctor",
            "--socket",
            str(socket_path),
            "--runtime-dir",
            str(tmp_path / "runtime"),
            "--registry",
            str(tmp_path / "projects.yaml"),
        ],
    )
    monkeypatch.setattr(doctor, "report_codex_setup", lambda *_a: None)
    monkeypatch.setattr(
        doctor,
        "codex_version",
        lambda: (parsed, f"codex-cli {version}"),
    )
    monkeypatch.setattr(doctor, "daemon_version", lambda *_a: (daemon, ""))
    monkeypatch.setattr(
        doctor,
        "require_daemon_identity",
        lambda *_args: SimpleNamespace(
            distribution="standalone",
            selected_codex=Path("/tmp/standalone/current/codex"),
            launchd_pid=101,
            peer_pid=102,
        ),
    )
    monkeypatch.setattr(doctor, "socket_check", lambda *_a: (True, "safe"))
    monkeypatch.setattr(doctor, "probe_handshake", lambda *_a: (True, "ready"))
    monkeypatch.setattr(doctor, "launchd_loaded", lambda _label: True)
    monkeypatch.setattr(doctor, "bridge_check", lambda *_a: (True, "healthy"))
    monkeypatch.setattr(doctor, "report_bind_request_queue", lambda *_a, **_k: None)
    monkeypatch.setattr(doctor, "project_health_checks", lambda *_a, **_k: None)
    monkeypatch.setattr(doctor, "report_hook_trust", lambda *_a, **_k: None)
    return socket_path


def test_doctor_reports_probe_accepted_release(
    tmp_path, monkeypatch, capsys
):
    _stub_codex_services(tmp_path, monkeypatch)
    monkeypatch.setattr(
        doctor,
        "codex_protocol_probe",
        lambda _socket: (True, "read-only protocol probe passed"),
    )
    # A failing bridge must not misdirect a probe-accepted release toward a
    # floor reinstall; the launch fix applies instead.
    monkeypatch.setattr(
        doctor, "bridge_check", lambda *_a: (False, "heartbeat stale")
    )

    code = doctor.main()

    output = capsys.readouterr().out
    assert code == 1
    assert "OK version:" in output
    assert "passed the live read-only protocol probe" in output
    assert "install a Codex release at or above the floor" not in output
    assert "run `roundtable codex` from a normal terminal" in output


@pytest.mark.parametrize(
    ("version", "stub_probe", "expected"),
    [
        ("0.145.0", (False, "hooks/list probe failed: closed"),
         "failed the app-server protocol probe"),
        ("0.143.0", None, "below the minimum supported app-server release"),
    ],
)
def test_doctor_fails_release_on_failed_probe_or_below_floor(
    tmp_path, monkeypatch, capsys, version, stub_probe, expected
):
    _stub_codex_services(tmp_path, monkeypatch, version=version)
    if stub_probe is None:
        # A below-floor release is rejected before any probe runs.
        monkeypatch.setattr(
            doctor,
            "codex_protocol_probe",
            lambda _socket: pytest.fail(
                "a below-floor release must fail before any probe"
            ),
        )
    else:
        monkeypatch.setattr(
            doctor, "codex_protocol_probe", lambda _socket: stub_probe
        )

    code = doctor.main()

    output = capsys.readouterr().out
    assert code == 1
    assert "FAIL version:" in output
    assert expected in output
