from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtcodex
from _rtruntime import RuntimeStateError


@contextmanager
def unlocked(*_args, **_kwargs):
    yield


def status(state: str, detail: str = "detail") -> _rtcodex.CodexServiceStatus:
    return _rtcodex.CodexServiceStatus(state, detail, (0, 144, 6))


def test_ready_probe_is_structured_and_requires_no_output(monkeypatch, capsys):
    socket_path = Path("/tmp/roundtable-test.sock")
    selected_codex = Path("/tmp/roundtable-current-codex")
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", None)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: None)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    daemon = {
        "status": "running",
        "socketPath": str(socket_path),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": None,
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(_rtcodex, "daemon_version", lambda _path: (daemon, ""))
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: (True, "probe passed"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "wake_bridge_health",
        lambda *_a, **_k: (True, "healthy"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_READY
    assert observed.daemon == daemon
    assert capsys.readouterr() == ("", "")


def test_installed_pre_hook_manifest_fails_closed_before_service_probe(
    tmp_path,
    monkeypatch,
):
    socket_path = Path("/tmp/roundtable-pre-hook-manifest.sock")
    probes: list[str] = []
    prefix = tmp_path / "installed"
    prefix.mkdir(mode=0o700)
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(
        _rtcodex,
        "_setup_manifest",
        lambda: {"harnesses": {"codex": {"plists": []}}},
    )
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: probes.append("probe") or (True, "ready", None),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_SETUP_REQUIRED
    assert "SessionStart hook ownership is missing or outdated" in observed.detail
    assert observed.cli_version == (0, 144, 6)
    assert observed.app_plist == "current"
    assert observed.wake_plist == "current"
    assert probes == []


@pytest.mark.parametrize("managed_version", [None, "0.143.0"])
def test_responsive_npm_daemon_uses_launchd_owner_not_managed_slot(
    monkeypatch,
    managed_version,
):
    socket_path = Path("/tmp/roundtable-managed-path.sock")
    selected_codex = Path("/tmp/npm/bin/codex")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", None)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: {})
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    daemon = {
        "status": "running",
        "socketPath": str(socket_path),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": managed_version,
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(_rtcodex, "daemon_version", lambda _path: (daemon, ""))
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: (True, "probe passed"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "wake_bridge_health",
        lambda *_a, **_k: (True, "healthy"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_READY
    assert observed.daemon == daemon


def test_responsive_codex_pid_backend_is_unsafe(monkeypatch):
    socket_path = Path("/tmp/roundtable-foreign-backend.sock")
    selected_codex = Path("/tmp/npm/bin/codex")
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", None)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: {})
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    daemon = {
        "status": "running",
        "backend": "pid",
        "socketPath": str(socket_path),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": "0.144.6",
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(_rtcodex, "daemon_version", lambda _path: (daemon, ""))

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_UNSAFE
    assert "Codex pid backend daemon" in observed.detail


def test_stale_foreign_daemon_is_unsafe_before_reload_classification(monkeypatch):
    socket_path = Path("/tmp/roundtable-foreign-stale.sock")
    selected_codex = Path("/tmp/npm/bin/codex")
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", None)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: {})
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    daemon = {
        "status": "running",
        "socketPath": str(socket_path),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": None,
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.5",
    }
    monkeypatch.setattr(_rtcodex, "daemon_version", lambda _path: (daemon, ""))
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (_ for _ in ()).throw(
            _rtcodex.CodexRuntimeError("foreign socket peer")
        ),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_UNSAFE
    assert observed.detail == "foreign socket peer"


@pytest.mark.parametrize("reported_build", [None, "sha256:stale"])
def test_wake_bridge_health_rejects_old_or_stale_build_fingerprint(
    tmp_path,
    monkeypatch,
    reported_build,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    pid_path = runtime / "rt-codex-wake.pid"
    heartbeat_path = runtime / "rt-codex-wake-heartbeat.json"
    pid_path.write_text("123\n")
    pid_path.chmod(0o600)
    now = datetime.now(timezone.utc).isoformat()
    heartbeat = {
        "schema": "roundtable.codex-wake-heartbeat.v1",
        "pid": 123,
        "heartbeatAt": now,
        "lastRpcOkAt": now,
        "lastError": None,
        "socketPath": str(tmp_path / "app.sock"),
    }
    if reported_build is not None:
        heartbeat["bridgeBuildFingerprint"] = reported_build
    heartbeat_path.write_text(json.dumps(heartbeat))
    heartbeat_path.chmod(0o600)
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(_rtcodex, "launchd_running", lambda _label: True)
    monkeypatch.setattr(
        _rtcodex,
        "pid_is_running",
        lambda *_args: (True, "pid 123"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "wake_bridge_build_fingerprint",
        lambda: "sha256:current",
    )

    ok, detail = _rtcodex.wake_bridge_health(tmp_path / "app.sock")

    assert not ok
    assert "build fingerprint is stale or invalid" in detail


@pytest.mark.parametrize(
    ("app_state", "wake_state"),
    [
        ("missing", "current"),
        ("current", "missing"),
        ("owned_drift", "current"),
        ("unowned_current", "current"),
    ],
)
def test_missing_or_outdated_managed_plist_requires_setup_before_repair(
    monkeypatch,
    app_state,
    wake_state,
):
    socket_path = Path("/tmp/roundtable-setup-required.sock")
    states = iter([app_state, wake_state])
    probes = []
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: {})
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: probes.append("probe") or (True, "ready", None),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_SETUP_REQUIRED
    assert "roundtable setup apply --harness codex" in observed.detail
    assert probes == []


def test_installed_exact_plist_still_requires_manifest_adoption(
    tmp_path,
    monkeypatch,
):
    label = _rtcodex.APP_SERVER_LABEL
    path = tmp_path / f"{label}.plist"
    expected = {"Label": label, "ProgramArguments": ["/tmp/codex"]}
    path.write_bytes(plistlib.dumps(expected, fmt=plistlib.FMT_XML, sort_keys=True))
    path.chmod(0o600)
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(tmp_path / "installed"))
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: path)

    observed = _rtcodex._plist_state(label, expected, None)

    assert observed == "unowned_current"


def _write_reload_marker(
    prefix: Path,
    app_payload: dict,
) -> Path:
    runtime = prefix / ".runtime"
    runtime.mkdir(parents=True, mode=0o700)
    runtime.chmod(0o700)
    path = _rtcodex.codex_reload_marker_path(prefix)
    path.write_text(
        json.dumps(
            _rtcodex.codex_reload_marker_payload(
                app_payload,
                prefix=prefix,
            )
        )
        + "\n"
    )
    path.chmod(0o600)
    return path


def test_same_version_responsive_daemon_honors_persistent_reload_marker(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "installed"
    prefix.mkdir(mode=0o700)
    socket_path = Path("/tmp/roundtable-marked-upgrade.sock")
    selected_codex = Path("/tmp/roundtable-current-codex")
    app_path = tmp_path / "app-server.plist"
    app_payload = {"Label": _rtcodex.APP_SERVER_LABEL, "ThrottleInterval": 17}
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: app_path)
    marker_path = _write_reload_marker(prefix, app_payload)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: {})
    monkeypatch.setattr(
        _rtcodex,
        "_manifest_owns_current_codex_hook",
        lambda _manifest: True,
    )
    monkeypatch.setattr(
        _rtcodex,
        "app_server_plist",
        lambda *_a, **_k: app_payload,
    )
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    daemon = {
        "status": "running",
        "socketPath": str(socket_path),
        "managedCodexPath": str(selected_codex),
        "managedCodexVersion": "0.144.6",
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(_rtcodex, "daemon_version", lambda _path: (daemon, ""))
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: (True, "probe passed"),
    )
    monkeypatch.setattr(_rtcodex, "inspect_host_harness_seats", lambda _h: [])
    monkeypatch.setattr(
        _rtcodex,
        "wake_bridge_health",
        lambda *_a, **_k: pytest.fail("marker must be classified before bridge READY"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_RELOAD_REQUIRED_IDLE
    assert "pending reload" in observed.detail
    assert marker_path.is_file()


@pytest.mark.parametrize("unsafe_kind", ["wrong-digest", "public-mode", "symlink"])
def test_foreign_or_unsafe_reload_marker_fails_closed(
    tmp_path,
    monkeypatch,
    unsafe_kind,
):
    prefix = tmp_path / "installed"
    prefix.mkdir(mode=0o700)
    app_path = tmp_path / "app-server.plist"
    app_payload = {"Label": _rtcodex.APP_SERVER_LABEL}
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: app_path)
    marker_path = _write_reload_marker(prefix, app_payload)
    if unsafe_kind == "wrong-digest":
        value = json.loads(marker_path.read_text())
        value["appPlistDigest"] = "0" * 64
        marker_path.write_text(json.dumps(value) + "\n")
        marker_path.chmod(0o600)
    elif unsafe_kind == "public-mode":
        marker_path.chmod(0o644)
    else:
        outside = tmp_path / "outside-marker.json"
        outside.write_text(marker_path.read_text())
        outside.chmod(0o600)
        marker_path.unlink()
        marker_path.symlink_to(outside)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="reload marker"):
        _rtcodex.codex_reload_required(app_payload)


def test_cold_marked_start_loads_exact_pair_then_clears_marker(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "installed"
    prefix.mkdir(mode=0o700)
    app_path = tmp_path / "app-server.plist"
    app_payload = {"Label": _rtcodex.APP_SERVER_LABEL}
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: app_path)
    marker_path = _write_reload_marker(prefix, app_payload)
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_COLD),
            status(_rtcodex.SERVICE_COLD),
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    calls: list[str] = []
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    monkeypatch.setattr(_rtcodex, "codex_setup_state_lock", unlocked)
    monkeypatch.setattr(
        _rtcodex,
        "app_server_plist",
        lambda *_a, **_k: app_payload,
    )
    monkeypatch.setattr(
        _rtcodex,
        "ensure_daemon",
        lambda *_a, **_k: calls.append("ensure-daemon"),
    )
    def fake_reload_pair(*_a, on_daemon_ready=None, **_k):
        calls.append("reload-pair")
        if on_daemon_ready is not None:
            on_daemon_ready()

    monkeypatch.setattr(_rtcodex, "_reload_service_pair", fake_reload_pair)

    observed = _rtcodex.codex_launch_preflight()

    assert observed.state == _rtcodex.SERVICE_READY
    assert calls == ["reload-pair"]
    assert not marker_path.exists()


def test_approved_marked_reload_clears_marker_after_success(
    tmp_path,
    monkeypatch,
):
    prefix = tmp_path / "installed"
    prefix.mkdir(mode=0o700)
    app_path = tmp_path / "app-server.plist"
    app_payload = {"Label": _rtcodex.APP_SERVER_LABEL}
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: app_path)
    marker_path = _write_reload_marker(prefix, app_payload)
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE),
            status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE),
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    calls: list[str] = []
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    monkeypatch.setattr(_rtcodex, "codex_setup_state_lock", unlocked)
    monkeypatch.setattr(
        _rtcodex,
        "app_server_plist",
        lambda *_a, **_k: app_payload,
    )
    def fake_reload_pair(*_a, on_daemon_ready=None, **_k):
        calls.append("reload-pair")
        if on_daemon_ready is not None:
            on_daemon_ready()

    monkeypatch.setattr(_rtcodex, "_reload_service_pair", fake_reload_pair)

    observed = _rtcodex.codex_launch_preflight(
        confirm_reload=lambda _status: calls.append("approved") or True
    )

    assert observed.state == _rtcodex.SERVICE_READY
    assert calls == ["approved", "reload-pair"]
    assert not marker_path.exists()


def test_responsive_daemon_preserves_structured_status_fields(
    monkeypatch,
):
    socket_path = Path("/tmp/roundtable-invalid-daemon-status.sock")
    daemon = {"status": "starting"}
    states = iter(["current", "current"])
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", None)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: None)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: next(states))
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    monkeypatch.setattr(
        _rtcodex,
        "daemon_version",
        lambda _path: (daemon, "status is starting"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_UNSAFE
    assert observed.cli_version == (0, 144, 6)
    assert observed.daemon == daemon
    assert observed.app_plist == "current"
    assert observed.wake_plist == "current"


def test_cold_preflight_rechecks_under_lock_and_uses_safe_ensure(monkeypatch):
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_COLD),
            status(_rtcodex.SERVICE_COLD),
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    calls: list[str] = []
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    monkeypatch.setattr(
        _rtcodex,
        "ensure_daemon",
        lambda *_a, **_k: calls.append("ensure-daemon"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "_restart_wake_bridge",
        lambda *_a, **_k: calls.append("restart-wake"),
    )

    observed = _rtcodex.codex_launch_preflight()

    assert observed.state == _rtcodex.SERVICE_READY
    assert calls == ["ensure-daemon"]


def test_loaded_but_unresponsive_daemon_is_not_treated_as_cold_start(
    monkeypatch,
):
    socket_path = Path("/tmp/roundtable-loaded-test.sock")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-current")
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: None)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (False, "connection refused", ConnectionRefusedError()),
    )
    monkeypatch.setattr(_rtcodex, "launchd_loaded", lambda _label: True)
    monkeypatch.setattr(_rtcodex, "inspect_host_harness_seats", lambda _h: [])

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_RELOAD_DEFERRED_BUSY
    assert "loaded app-server is unavailable" in observed.detail


def test_launchctl_inspection_failure_is_unsafe_not_cold(monkeypatch):
    socket_path = Path("/tmp/roundtable-launchctl-error.sock")
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: {})
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: ((0, 144, 6), "codex-cli 0.144.6"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (False, "missing socket", FileNotFoundError()),
    )
    monkeypatch.setattr(
        _rtcodex,
        "launchd_loaded",
        lambda _label: (_ for _ in ()).throw(
            _rtcodex.CodexRuntimeError("launchctl print failed: not authorized")
        ),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_UNSAFE
    assert "launchctl print failed" in observed.detail


def test_launchctl_only_treats_exit_113_as_not_loaded(monkeypatch):
    outcomes = iter(
        [
            SimpleNamespace(returncode=113, stdout="", stderr="not found"),
            SimpleNamespace(returncode=1, stdout="", stderr="not authorized"),
        ]
    )
    monkeypatch.setattr(_rtcodex.subprocess, "run", lambda *_a, **_k: next(outcomes))

    assert _rtcodex.launchd_loaded(_rtcodex.APP_SERVER_LABEL) is False
    with pytest.raises(_rtcodex.CodexRuntimeError, match="not authorized"):
        _rtcodex.launchd_loaded(_rtcodex.APP_SERVER_LABEL)


def test_bridge_repair_never_reloads_app_server(monkeypatch):
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_BRIDGE_DOWN),
            status(_rtcodex.SERVICE_BRIDGE_DOWN),
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    calls: list[str] = []
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    monkeypatch.setattr(
        _rtcodex,
        "_restart_wake_bridge",
        lambda *_a, **_k: calls.append("restart-wake"),
    )
    def fake_reload_pair(*_a, on_daemon_ready=None, **_k):
        calls.append("reload-pair")
        if on_daemon_ready is not None:
            on_daemon_ready()

    monkeypatch.setattr(_rtcodex, "_reload_service_pair", fake_reload_pair)

    _rtcodex.codex_launch_preflight()

    assert calls == ["restart-wake"]


def _write_bridge_takeover_fixture(
    runtime: Path,
    socket_path: Path,
    *,
    heartbeat_pid: int,
) -> None:
    pid_path = runtime / "rt-codex-wake.pid"
    heartbeat_path = runtime / "rt-codex-wake-heartbeat.json"
    pid_path.write_text("222\n", encoding="utf-8")
    pid_path.chmod(0o600)
    now = datetime.now(timezone.utc).isoformat()
    heartbeat_path.write_text(
        json.dumps(
            {
                "schema": "roundtable.codex-wake-heartbeat.v1",
                "pid": heartbeat_pid,
                "heartbeatAt": now,
                "lastRpcOkAt": now,
                "lastError": None,
                "socketPath": str(socket_path),
                "bridgeBuildFingerprint": "sha256:current",
            }
        ),
        encoding="utf-8",
    )
    heartbeat_path.chmod(0o600)


def _prepare_bridge_takeover_test(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = tmp_path / "app.sock"
    _write_bridge_takeover_fixture(runtime, socket_path, heartbeat_pid=111)
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(_rtcodex, "launchd_running", lambda _label: True)
    monkeypatch.setattr(
        _rtcodex,
        "pid_is_running",
        lambda *_args: (True, "pid 222"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "wake_bridge_build_fingerprint",
        lambda: "sha256:current",
    )
    return runtime, socket_path


def test_bridge_repair_waits_for_heartbeat_pid_takeover(tmp_path, monkeypatch):
    runtime, socket_path = _prepare_bridge_takeover_test(tmp_path, monkeypatch)
    clock = [0.0]
    sleeps: list[float] = []

    def fake_sleep(interval: float) -> None:
        sleeps.append(interval)
        clock[0] += interval
        if len(sleeps) == 2:
            _write_bridge_takeover_fixture(
                runtime,
                socket_path,
                heartbeat_pid=222,
            )

    monkeypatch.setattr(_rtcodex.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_rtcodex.time, "sleep", fake_sleep)

    _rtcodex._wait_for_wake_bridge_takeover(
        socket_path,
        timeout=1.0,
        poll_interval=0.1,
    )

    assert sleeps == [0.1, 0.1]


def test_bridge_repair_takeover_timeout_reports_observed_pid_and_age(
    tmp_path,
    monkeypatch,
):
    _runtime, socket_path = _prepare_bridge_takeover_test(tmp_path, monkeypatch)
    clock = [0.0]

    def fake_sleep(interval: float) -> None:
        clock[0] += interval

    monkeypatch.setattr(_rtcodex.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_rtcodex.time, "sleep", fake_sleep)

    with pytest.raises(
        _rtcodex.CodexRuntimeError,
        match=r"wake bridge repair failed: bridge heartbeat pid 111 != live pid 222; "
        r"heartbeat age=\d+\.\d+s",
    ):
        _rtcodex._wait_for_wake_bridge_takeover(
            socket_path,
            timeout=0.25,
            poll_interval=0.1,
        )


def test_ready_preflight_does_not_wait_for_bridge_takeover(monkeypatch):
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    monkeypatch.setattr(
        _rtcodex,
        "_wait_for_wake_bridge_takeover",
        lambda *_a, **_k: pytest.fail("healthy preflight must not wait for bridge repair"),
    )

    observed = _rtcodex.codex_launch_preflight()

    assert observed.state == _rtcodex.SERVICE_READY


def test_daemon_reload_requires_explicit_approval(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        _rtcodex,
        "inspect_codex_services",
        lambda *_a: status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE, "version mismatch"),
    )
    def fake_reload_pair(*_a, on_daemon_ready=None, **_k):
        calls.append("reload-pair")
        if on_daemon_ready is not None:
            on_daemon_ready()

    monkeypatch.setattr(_rtcodex, "_reload_service_pair", fake_reload_pair)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="was not approved"):
        _rtcodex.codex_launch_preflight()

    assert calls == []


def test_approved_reload_rechecks_inside_host_lock(monkeypatch):
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE, "version mismatch"),
            status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE, "version mismatch"),
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    calls: list[str] = []
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    def fake_reload_pair(*_a, on_daemon_ready=None, **_k):
        calls.append("reload-pair")
        if on_daemon_ready is not None:
            on_daemon_ready()

    monkeypatch.setattr(_rtcodex, "_reload_service_pair", fake_reload_pair)

    observed = _rtcodex.codex_launch_preflight(
        confirm_reload=lambda _status: calls.append("approved") or True
    )

    assert observed.state == _rtcodex.SERVICE_READY
    assert calls == ["approved", "reload-pair"]


def test_ready_fast_path_rechecks_and_claims_inside_host_lock(monkeypatch):
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_READY),
            status(_rtcodex.SERVICE_READY),
        ]
    )
    events: list[str] = []

    @contextmanager
    def tracked_lock(*_args, **_kwargs):
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def claim():
        assert events == ["lock-enter"]
        events.append("claim")

    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", tracked_lock)

    observed = _rtcodex.codex_launch_preflight(ready_action=claim)

    assert observed.state == _rtcodex.SERVICE_READY
    assert events == ["lock-enter", "claim", "lock-exit"]


def test_busy_reload_is_deferred_without_asking(monkeypatch):
    decisions: list[str] = []
    monkeypatch.setattr(
        _rtcodex,
        "inspect_codex_services",
        lambda *_a: status(
            _rtcodex.SERVICE_RELOAD_DEFERRED_BUSY,
            "codex-build@/project is active",
        ),
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match="deferred_busy"):
        _rtcodex.codex_launch_preflight(
            confirm_reload=lambda _status: decisions.append("asked") or True
        )

    assert decisions == []


def test_reload_status_blocks_the_calling_codex_thread(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-current")
    monkeypatch.setattr(
        _rtcodex,
        "inspect_host_harness_seats",
        lambda _harness: [],
    )

    observed = _rtcodex._reload_status(
        "version mismatch",
        cli_version=(0, 144, 6),
        daemon={},
        app_plist="current",
        wake_plist="current",
    )

    assert observed.state == _rtcodex.SERVICE_RELOAD_DEFERRED_BUSY
    assert "caller is itself a Codex thread" in observed.detail


def test_reload_status_blocks_any_active_host_codex_lease(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    inspection = SimpleNamespace(
        status="active_unhealthy",
        detail="owner is live; wake has no heartbeat",
        token=SimpleNamespace(agent_id="codex-build", project_root=Path("/project")),
    )
    monkeypatch.setattr(
        _rtcodex,
        "inspect_host_harness_seats",
        lambda _harness: [inspection],
    )

    observed = _rtcodex._reload_status(
        "version mismatch",
        cli_version=(0, 144, 6),
        daemon={},
        app_plist="current",
        wake_plist="current",
    )

    assert observed.state == _rtcodex.SERVICE_RELOAD_DEFERRED_BUSY
    assert "codex-build@/project is active_unhealthy" in observed.detail


def test_reload_status_preserves_migrated_runtime_cleanup_remedy(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    runtime_dir = Path("/runtime/projects/old-hash")

    def fail_with_residue(_harness):
        raise RuntimeStateError(
            f"stale migrated-project runtime residue at {runtime_dir}: recorded "
            "projectRoot /old now canonicalizes to /new, so the canonical "
            "project hash changed; after verifying no live or ambiguous owner "
            "remains, move this exact directory out of /runtime/projects"
        )

    monkeypatch.setattr(
        _rtcodex,
        "inspect_host_harness_seats",
        fail_with_residue,
    )

    observed = _rtcodex._reload_status(
        "version mismatch",
        cli_version=(0, 147, 0),
        daemon={},
        app_plist="current",
        wake_plist="current",
    )

    assert observed.state == _rtcodex.SERVICE_RELOAD_DEFERRED_BUSY
    assert str(runtime_dir) in observed.detail
    assert "projectRoot /old now canonicalizes to /new" in observed.detail
    assert "move this exact directory out of /runtime/projects" in observed.detail


def test_service_path_symlink_and_permission_drift_fail_closed(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    target = tmp_path / "plist-target"
    target.write_text("foreign")
    plist = tmp_path / "agent.plist"
    plist.symlink_to(target)
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: plist)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="symlink"):
        _rtcodex._validate_service_paths(tmp_path / "missing" / "app.sock")

    plist.unlink()
    plist.write_text("payload")
    plist.chmod(0o644)
    with pytest.raises(
        _rtcodex.CodexRuntimeError,
        match="exposes group/other permissions",
    ):
        _rtcodex._validate_service_paths(tmp_path / "missing" / "app.sock")


def _prepare_inspection(
    monkeypatch,
    socket_path: Path,
    selected_codex: Path,
    version: str = "0.145.0",
) -> dict:
    """Stub one identity-proven standalone service pair on `version`."""

    parsed = tuple(int(part) for part in version.split("."))
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", None)
    monkeypatch.setattr(_rtcodex, "require_default_socket", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "_setup_manifest", lambda: None)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "wake_plist", lambda *_a, **_k: {})
    monkeypatch.setattr(_rtcodex, "_plist_state", lambda *_a, **_k: "current")
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "codex_version",
        lambda: (parsed, f"codex-cli {version}"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *_a, **_k: (True, "ready", None),
    )
    daemon = {
        "status": "running",
        "socketPath": str(socket_path),
        "managedCodexPath": str(selected_codex),
        "managedCodexVersion": version,
        "cliVersion": version,
        "appServerVersion": version,
    }
    monkeypatch.setattr(_rtcodex, "daemon_version", lambda _path: (daemon, ""))
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "wake_bridge_health",
        lambda *_a, **_k: (True, "healthy"),
    )
    return daemon


def test_standalone_release_probes_then_ready(monkeypatch):
    socket_path = Path("/tmp/roundtable-probe.sock")
    daemon = _prepare_inspection(
        monkeypatch, socket_path, Path("/tmp/standalone/current/codex")
    )
    probes = []
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: probes.append("probe") or (True, "probe passed"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_READY
    assert observed.daemon == daemon
    assert observed.detail == "Codex app-server and wake bridge are ready"
    assert probes == ["probe"]


def test_release_probe_failure_is_unsupported(monkeypatch):
    socket_path = Path("/tmp/roundtable-probe.sock")
    _prepare_inspection(
        monkeypatch, socket_path, Path("/tmp/standalone/current/codex")
    )
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: (False, "hooks/list probe failed: closed"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_UNSUPPORTED
    assert "failed the app-server protocol probe" in observed.detail
    assert "hooks/list probe failed: closed" in observed.detail


def test_probe_runs_only_after_daemon_identity(monkeypatch):
    socket_path = Path("/tmp/roundtable-probe.sock")
    _prepare_inspection(
        monkeypatch, socket_path, Path("/tmp/standalone/current/codex")
    )
    order = []
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: order.append("identity") or (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: order.append("probe") or (True, "probe passed"),
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_READY
    assert order == ["identity", "probe"]


def test_below_floor_release_is_unsupported_with_floor_detail(monkeypatch):
    socket_path = Path("/tmp/roundtable-below-floor.sock")
    _prepare_inspection(
        monkeypatch,
        socket_path,
        Path("/tmp/standalone/current/codex"),
        version="0.143.0",
    )

    observed = _rtcodex.inspect_codex_services(socket_path)

    assert observed.state == _rtcodex.SERVICE_UNSUPPORTED
    assert "below the minimum supported app-server release 0.144.6" in observed.detail


def test_bridge_convergence_failure_still_clears_the_reload_marker(
    tmp_path,
    monkeypatch,
):
    """The marker tracks plist activation, not bridge convergence. When the
    repaired bridge misses its takeover window the launch may still fail,
    but a stranded marker made every later launch re-prompt and bounce the
    daemon again (observed live on 2026-08-17)."""

    prefix = tmp_path / "installed"
    prefix.mkdir(mode=0o700)
    app_path = tmp_path / "app-server.plist"
    app_payload = {"Label": _rtcodex.APP_SERVER_LABEL}
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: app_path)
    marker_path = _write_reload_marker(prefix, app_payload)
    outcomes = iter(
        [
            status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE),
            status(_rtcodex.SERVICE_RELOAD_REQUIRED_IDLE),
        ]
    )
    monkeypatch.setattr(_rtcodex, "inspect_codex_services", lambda *_a: next(outcomes))
    monkeypatch.setattr(_rtcodex, "codex_service_repair_lock", unlocked)
    monkeypatch.setattr(_rtcodex, "codex_setup_state_lock", unlocked)
    monkeypatch.setattr(
        _rtcodex,
        "app_server_plist",
        lambda *_a, **_k: app_payload,
    )

    def failing_reload_pair(*_a, on_daemon_ready=None, **_k):
        if on_daemon_ready is not None:
            on_daemon_ready()
        raise _rtcodex.CodexRuntimeError(
            "wake bridge repair failed: bridge heartbeat pid 1 != live pid 2"
        )

    monkeypatch.setattr(_rtcodex, "_reload_service_pair", failing_reload_pair)

    with pytest.raises(_rtcodex.CodexRuntimeError):
        _rtcodex.codex_launch_preflight(confirm_reload=lambda _status: True)

    assert not marker_path.exists()


def test_bridge_takeover_window_covers_a_full_pair_restart():
    """A daemon+bridge pair restart delivers the first fresh heartbeat at
    ~15s on a loaded host (measured 15.1s twice); the window must keep a
    real margin above that or launches abort after successful reloads."""

    assert _rtcodex.WAKE_BRIDGE_TAKEOVER_TIMEOUT_SECONDS >= 60
