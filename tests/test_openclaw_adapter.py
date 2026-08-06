from __future__ import annotations

import importlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

adapter = importlib.import_module("integrations.openclaw.roundtable")
import _rtlauncher


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


def test_openclaw_launcher_claims_then_execs_managed_adapter(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "openclaw"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    wake = tmp_path / "rt-openclaw-wake"
    wake.write_text("#!/bin/sh\n")
    wake.chmod(0o755)
    observed = {}

    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher, "set_launch_identity", lambda *_args: "openclaw")
    monkeypatch.setattr(_rtlauncher, "normalize_runtime_environment", lambda: tmp_path)
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: executable)
    monkeypatch.setattr(_rtlauncher, "openclaw_adapter_bin", lambda: wake)
    monkeypatch.setattr(_rtlauncher, "claim_launch_seat", lambda *_args: object())

    def fake_exec(path, argv):
        observed["path"] = path
        observed["argv"] = argv
        observed["gateway_bin"] = _rtlauncher.os.environ["RT_OPENCLAW_BIN"]
        raise RuntimeError("exec captured")

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_exec)
    with pytest.raises(RuntimeError, match="exec captured"):
        _rtlauncher.launch("openclaw", ["--once"])

    assert observed == {
        "path": str(wake),
        "argv": [str(wake), "--gateway-bin", str(executable), "--once"],
        "gateway_bin": str(executable),
    }
