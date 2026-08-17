from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

adapter = importlib.import_module("integrations.openclaw.roundtable")
import _rtlauncher
import _rtruntime


class FakeGateway:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_args):
        self.close()

    def request(self, method, params, *, timeout=None):
        self.calls.append((method, params, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_session_key_and_wake_prompt_are_bounded(tmp_path):
    key = adapter.session_key(tmp_path, "openclaw")
    assert key.startswith("agent:openclaw:roundtable-")
    assert len(key.rsplit("-", 1)[-1]) == 20
    prompt = adapter.wake_prompt(tmp_path, "openclaw", ("one.md", "two.md"))
    assert "rt-inbox --fenced --archive-quiet-acks -f json" in prompt
    assert "rt-ack --fenced" in prompt
    assert "one.md, two.md" in prompt


def test_isolation_contains_home_xdg_tmp_and_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    executable = runtime / "bin" / "openclaw"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=runtime, port=19401, token="token")
    environment = isolation.environment(project_root=project, executable=executable)
    isolation.assert_isolated(environment)
    assert Path(environment["HOME"]).is_relative_to(isolation.root)
    assert Path(environment["OPENCLAW_CONFIG_PATH"]).is_relative_to(isolation.root)
    assert Path(environment["TMPDIR"]).is_relative_to(isolation.root)
    monkeypatch.setitem(environment, "HOME", str(tmp_path / "personal"))
    with pytest.raises(adapter.OpenClawError, match="HOME"):
        isolation.assert_isolated(environment)


def test_isolation_rejects_project_child_before_creating_state(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    runtime = project / "operator-runtime"
    monkeypatch.setenv("RT_OPENCLAW_RUNTIME_DIR", str(runtime))

    with pytest.raises(
        adapter.OpenClawError,
        match="isolation root must not be inside the project",
    ):
        adapter.create_isolation(project, port=19405, token="must-not-be-written")

    assert not runtime.exists()


def test_adapter_waits_for_final_run_and_exact_mail_drain(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "openclaw"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime", port=19402)
    fake = FakeGateway(
        [
            {"runId": "run-1", "status": "accepted"},
            {"runId": "run-1", "status": "timeout"},
            {"runId": "run-1", "status": "ok", "endedAt": "now"},
        ]
    )
    adapter_instance = adapter.OpenClawAdapter(
        project,
        agent_id="openclaw",
        session_id="session-1",
        revision="1",
        isolation=isolation,
        executable=executable,
        gateway_factory=lambda _url, _token: fake,
        sleep=lambda _seconds: None,
    )
    generations = [["message.md"], ["message.md"], []]
    adapter_instance._mail_generation = lambda: tuple(generations.pop(0))
    updates = []
    adapter_instance._fence_update = lambda **kwargs: updates.append(kwargs)

    result = adapter_instance.run_generation(("message.md",), timeout=2, drain_timeout=2)

    assert result == adapter.GatewayRun("run-1", "ok", {"runId": "run-1", "status": "ok", "endedAt": "now"})
    assert [call[0] for call in fake.calls] == ["agent", "agent.wait", "agent.wait"]
    assert fake.calls[0][1]["idempotencyKey"] == "roundtable:message"
    assert updates[-1]["names"] == ("message.md",)


def _adapter_for_gateway_test(tmp_path, gateway_factory, **kwargs):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    executable = tmp_path / "openclaw"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime")
    return adapter.OpenClawAdapter(
        project,
        agent_id="openclaw",
        session_id="session-1",
        revision="1",
        isolation=isolation,
        executable=executable,
        gateway_factory=gateway_factory,
        **kwargs,
    )


def test_gateway_down_keeps_durable_generation_and_recovers_after_return(tmp_path):
    attempts = []
    clock = [0.0]

    class Down:
        def connect(self):
            attempts.append("down")
            raise adapter.GatewayUnavailableError(
                "OpenClaw Gateway unavailable at ws://127.0.0.1:1: refused"
            )

        def close(self):
            pass

        def __enter__(self):
            self.connect()
            return self

        def __exit__(self, *_args):
            self.close()

    class Ready:
        def connect(self):
            attempts.append("ready")

        def request(self, *_args, **_kwargs):
            return {"ok": True}

        def close(self):
            pass

    def factory(_url, _token):
        return Down() if len(attempts) == 0 else Ready()

    def monotonic():
        value = clock[0]
        clock[0] += 0.1
        return value

    instance = _adapter_for_gateway_test(
        tmp_path,
        factory,
        monotonic=monotonic,
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    instance.process = type("Process", (), {"poll": lambda _self: None})()
    instance.wait_ready(timeout=1)
    assert attempts == ["down", "ready"]

    durable = instance.project_root / "durable-message.md"
    durable.write_text("still in the authoritative maildir\n", encoding="utf-8")
    instance.gateway_factory = lambda _url, _token: Down()
    with pytest.raises(adapter.GatewayUnavailableError, match="actionable|unavailable"):
        instance.run_generation((durable.name,), timeout=1)
    assert durable.is_file()


def test_gateway_killed_mid_turn_does_not_ack_or_archive_generation(tmp_path):
    class Killed(FakeGateway):
        def request(self, method, params, *, timeout=None):
            self.calls.append((method, params, timeout))
            if method == "agent":
                return {"runId": "run-killed", "status": "accepted"}
            raise adapter.GatewayUnavailableError(
                "OpenClaw Gateway unavailable at ws://127.0.0.1:1: process exited"
            )

    fake = Killed([])
    instance = _adapter_for_gateway_test(tmp_path, lambda _url, _token: fake)
    new_dir = tmp_path / "authoritative-maildir" / "new"
    new_dir.mkdir(parents=True)
    message = new_dir / "20260806T000000Z-claude-to-openclaw-deadbeef.md"
    message.write_text("must remain durable\n", encoding="utf-8")
    with pytest.raises(adapter.GatewayUnavailableError, match="process exited"):
        instance.run_generation((message.name,), timeout=1)
    assert message.is_file()
    assert not list((new_dir.parent / "cur").glob("*")) if (new_dir.parent / "cur").exists() else True


def test_gateway_auth_failure_is_immediate_and_has_no_retry_storm(tmp_path):
    attempts = []
    sleeps = []

    class BadAuth:
        def connect(self):
            attempts.append(1)
            raise adapter.GatewayAuthenticationError(
                "OpenClaw Gateway authentication rejected; check the isolated token"
            )

        def close(self):
            pass

    instance = _adapter_for_gateway_test(
        tmp_path,
        lambda _url, _token: BadAuth(),
        sleep=sleeps.append,
    )
    instance.process = type("Process", (), {"poll": lambda _self: None})()
    with pytest.raises(adapter.GatewayAuthenticationError, match="isolated token"):
        instance.wait_ready(timeout=30)
    assert attempts == [1]
    assert sleeps == []


def test_run_timeout_is_bounded_and_terminal_error_is_not_swallowed(tmp_path):
    class NeverTerminal(FakeGateway):
        def request(self, method, params, *, timeout=None):
            self.calls.append((method, params, timeout))
            if method == "agent":
                return {"runId": "run-never", "status": "accepted"}
            return {"runId": "run-never", "status": "timeout"}

    clock = [0.0]

    def monotonic():
        value = clock[0]
        clock[0] += 0.4
        return value

    never = NeverTerminal([])
    instance = _adapter_for_gateway_test(tmp_path, lambda _url, _token: never, monotonic=monotonic)
    with pytest.raises(adapter.GatewayRunTimeout, match="timed out"):
        instance.run_generation(("never.md",), timeout=1)

    error = FakeGateway(
        [
            {"runId": "run-error", "status": "accepted"},
            {"runId": "run-error", "status": "error", "error": "provider rejected request"},
        ]
    )
    instance = _adapter_for_gateway_test(tmp_path / "error", lambda _url, _token: error)
    with pytest.raises(adapter.GatewayRunError, match="provider rejected request"):
        instance.run_generation(("error.md",), timeout=1)


def test_rapid_duplicate_wakes_reuse_idempotency_key(tmp_path):
    fake = FakeGateway(
        [
            {"runId": "run-once", "status": "accepted"},
            {"runId": "run-once", "status": "ok", "endedAt": "one"},
            {"runId": "run-once", "status": "accepted"},
            {"runId": "run-once", "status": "ok", "endedAt": "two"},
        ]
    )
    instance = _adapter_for_gateway_test(tmp_path, lambda _url, _token: fake)
    instance._mail_generation = lambda: ()
    first = instance.run_generation(("rapid.md",), timeout=1, drain_timeout=1)
    second = instance.run_generation(("rapid.md",), timeout=1, drain_timeout=1)
    agent_calls = [call for call in fake.calls if call[0] == "agent"]
    assert first.run_id == second.run_id == "run-once"
    assert agent_calls[0][1]["idempotencyKey"] == agent_calls[1][1]["idempotencyKey"]
    assert agent_calls[0][1]["sessionKey"] == agent_calls[1][1]["sessionKey"]


@pytest.mark.parametrize("bad_field", ["session_id", "agent_id"])
def test_stale_or_wrong_identity_is_refused_before_gateway_start(tmp_path, monkeypatch, bad_field):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    lease = _rtruntime.claim(project, "openclaw", "openclaw", owner_pid=os.getpid())
    try:
        executable = tmp_path / "openclaw"
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        instance = adapter.OpenClawAdapter(
            project,
            agent_id="other" if bad_field == "agent_id" else "openclaw",
            session_id="wrong-session" if bad_field == "session_id" else lease.session_id,
            revision=str(lease.revision),
            isolation=adapter.create_isolation(project, runtime_root=tmp_path / "openclaw-runtime"),
            executable=executable,
        )
        started = []
        monkeypatch.setattr(instance, "start_gateway", lambda: started.append(True))
        with pytest.raises(_rtruntime.FenceRejected):
            instance.run(once=True)
        assert started == []
    finally:
        _rtruntime.release(lease)


def test_adapter_rejects_non_loopback_gateway_url():
    with pytest.raises(adapter.GatewayProtocolError, match="loopback"):
        adapter._WebSocket("ws://example.invalid:1234", "token")


def test_adapter_constrains_gateway_config_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "openclaw"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime", port=19403)
    adapter_instance = adapter.OpenClawAdapter(
        project,
        agent_id="openclaw",
        session_id="session-1",
        revision="1",
        isolation=isolation,
        executable=executable,
    )

    adapter_instance._prepare_isolated_config()
    payload = json.loads(isolation.config_path.read_text(encoding="utf-8"))
    assert Path(payload["logging"]["file"]).is_relative_to(isolation.root)
    assert str(Path(sys.executable).resolve().parent) in payload["tools"]["exec"]["pathPrepend"]


def test_adapter_rejects_gateway_log_path_escape(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "openclaw"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime", port=19404)
    isolation.config_path.write_text(
        json.dumps({"logging": {"file": str(tmp_path / "outside.log")}}) + "\n",
        encoding="utf-8",
    )
    adapter_instance = adapter.OpenClawAdapter(
        project,
        agent_id="openclaw",
        session_id="session-1",
        revision="1",
        isolation=isolation,
        executable=executable,
    )

    with pytest.raises(adapter.OpenClawError, match="logging.file escapes"):
        adapter_instance._prepare_isolated_config()


def test_openclaw_seat_stub_refuses_without_claiming_a_lease(tmp_path):
    runtime = tmp_path / "runtime"
    environment = os.environ.copy()
    environment["RT_RUNTIME_DIR"] = str(runtime)
    environment["RT_CODEX_RUNTIME_DIR"] = str(runtime)

    result = subprocess.run(
        [sys.executable, str(BIN / "rt-openclaw")],
        cwd=tmp_path,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "2026-08-17 owner decision" in result.stderr
    assert "shipped seat surface" in result.stderr
    assert "rt-openclaw-wake" in result.stderr
    assert "internal lab machinery" in result.stderr
    assert not runtime.exists()
    assert "_rtlauncher" not in (BIN / "rt-openclaw").read_text(encoding="utf-8")
    for table in (
        _rtlauncher.COMMANDS,
        _rtlauncher.HARNESS_LABELS,
        _rtlauncher.HARNESS_INSTALL_HINTS,
        _rtlauncher.EXECUTABLE_OVERRIDES,
        _rtlauncher.CONFIG_HARNESSES,
    ):
        assert "openclaw" not in table

    with pytest.raises(_rtlauncher.SelectionError, match="unknown Roundtable harness"):
        _rtlauncher.launch("openclaw", [])
