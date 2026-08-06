from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

adapter = importlib.import_module("integrations.grok.roundtable")
import _rtlauncher  # noqa: E402
import _rtruntime  # noqa: E402


class _Process:
    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode


class _FakeClient:
    def __init__(self, *, prompt_error=None, initialize=None, session=None):
        self.process = _Process()
        self.prompt_error = prompt_error
        self.initialize_response = initialize or {"jsonrpc": "2.0", "id": 1, "result": {}}
        self.session_response = session or {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"sessionId": "grok-session"},
        }
        self.prompts = []
        self.closed = False
        self.permission_decisions = []

    def request(self, method, params, *, timeout):
        if method == "initialize":
            return self.initialize_response, {"method": method}
        if method == "session/new":
            return self.session_response, {"method": method}
        if method == "session/prompt":
            self.prompts.append(params)
            if self.prompt_error is not None:
                raise self.prompt_error
            return {
                "jsonrpc": "2.0",
                "id": 3,
                "result": {"stopReason": "end_turn"},
            }, {"method": method}
        raise AssertionError(method)

    def close(self, *, force=False):
        self.closed = True
        self.process.returncode = -9 if force else -15


def _instance(tmp_path, factory, **kwargs):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime")
    instance = adapter.GrokAdapter(
        project,
        agent_id="grok",
        session_id="session-1",
        revision="1",
        isolation=isolation,
        executable=executable,
        api_key_resolver=lambda: "xai-test-key",
        client_factory=factory,
        **kwargs,
    )
    instance._lease_valid = lambda: None
    return instance


def test_auth_reader_uses_key_and_ignores_refresh_token(tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text(
        json.dumps({"account": {"key": "xai-test-key", "refresh_token": "do-not-use"}}),
        encoding="utf-8",
    )
    assert adapter.resolve_grok_api_key({"GROK_AUTH_PATH": str(auth)}) == "xai-test-key"


def test_isolation_bounds_child_state_and_path(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    isolation = adapter.create_isolation(project, runtime_root=tmp_path / "runtime")
    environment = isolation.environment(
        project_root=project,
        executable=executable,
        api_key="xai-test-key",
    )
    environment["RT_GROK_BIN"] = str(executable)
    isolation.assert_isolated(environment)
    assert Path(environment["HOME"]).is_relative_to(isolation.root)
    assert Path(environment["GROK_HOME"]).is_relative_to(isolation.root)
    monkeypatch.setitem(environment, "GROK_HOME", str(tmp_path / "personal"))
    with pytest.raises(adapter.GrokError, match="GROK_HOME"):
        isolation.assert_isolated(environment)


def test_wake_prompt_and_permission_policy_are_mail_only(tmp_path):
    prompt = adapter.wake_prompt(tmp_path, "grok", ("one.md", "two.md"))
    assert "rt-inbox --fenced --archive-quiet-acks -f json" in prompt
    assert "RT_FROM=grok rt-ack <msg_id> handled" in prompt
    assert adapter._mail_command_allowed("rt-inbox -f json")
    assert adapter._mail_command_allowed("rt-inbox --fenced --archive-quiet-acks -f json")
    assert adapter._mail_command_allowed("RT_FROM=grok rt-ack message-id handled")
    assert not adapter._mail_command_allowed("pip install PyYAML")
    assert not adapter._mail_command_allowed("RT_FROM=grok rt-ack message-id; rm -rf x")


def test_authenticated_session_reuses_child_and_waits_for_exact_drain(tmp_path):
    client = _FakeClient()
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    generations = [("message.md",), ()]
    instance._mail_generation = lambda: generations.pop(0)
    fences = []
    instance._fence_update = lambda **kwargs: fences.append(kwargs)

    result = instance.run_generation(("message.md",), prompt_timeout=1, drain_timeout=1)

    assert result.stop_reason == "end_turn"
    assert len(client.prompts) == 1
    assert client.prompts[0]["sessionId"] == "grok-session"
    assert fences[-1]["names"] == ("message.md",)


def test_child_death_restarts_once_and_preserves_mail_until_retry(tmp_path):
    first = _FakeClient(prompt_error=adapter.GrokUnavailableError("child died"))
    second = _FakeClient()
    clients = iter((first, second))
    instance = _instance(tmp_path, lambda _command, **_kwargs: next(clients))
    generations = [("message.md",), ("message.md",), ()]
    instance._mail_generation = lambda: generations.pop(0)
    instance._fence_update = lambda **_kwargs: None

    result = instance.run_generation(("message.md",), prompt_timeout=1, drain_timeout=1)

    assert result.stop_reason == "end_turn"
    assert first.closed
    assert len(second.prompts) == 1


def test_auth_failure_does_not_retry_or_start_a_second_child(tmp_path):
    bad = _FakeClient(
        initialize={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Incorrect API key provided"},
        }
    )
    calls = []
    instance = _instance(tmp_path, lambda _command, **_kwargs: calls.append(bad) or bad)
    with pytest.raises(adapter.GrokAuthenticationError):
        instance._start_client()
    assert calls == [bad]


def test_prompt_auth_failure_is_classified_and_child_is_closed(tmp_path):
    class BadPrompt(_FakeClient):
        def request(self, method, params, *, timeout):
            if method == "session/prompt":
                return {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {
                        "code": -32603,
                        "data": {
                            "http_status": 403,
                            "message": "unauthenticated:bad-credentials",
                        },
                    },
                }, {"method": method}
            return super().request(method, params, timeout=timeout)

    client = BadPrompt()
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    instance._mail_generation = lambda: ("message.md",)
    with pytest.raises(adapter.GrokAuthenticationError, match="wake turn"):
        instance.run_generation(("message.md",), prompt_timeout=1, drain_timeout=1)
    assert client.closed


def test_hung_prompt_is_bounded_and_does_not_ack(tmp_path):
    client = _FakeClient(prompt_error=adapter.GrokRunTimeout("hung"))
    instance = _instance(tmp_path, lambda _command, **_kwargs: client)
    instance._mail_generation = lambda: ("message.md",)
    with pytest.raises(adapter.GrokRunTimeout):
        instance.run_generation(("message.md",), prompt_timeout=0.01, drain_timeout=0.01)
    assert instance._mail_generation() == ("message.md",)
    assert client.closed


@pytest.mark.parametrize("bad_field", ["session_id", "agent_id", "revision"])
def test_stale_or_wrong_identity_is_refused_before_child_start(tmp_path, monkeypatch, bad_field):
    project = tmp_path / "project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    lease = _rtruntime.claim(project, "grok", "grok", owner_pid=os.getpid())
    try:
        executable = tmp_path / "grok"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
        isolation = adapter.create_isolation(project, runtime_root=tmp_path / "grok-runtime")
        values = {
            "agent_id": "other",
            "session_id": "wrong-session",
                "revision": "wrong-revision",
        }
        instance = adapter.GrokAdapter(
            project,
            agent_id=values["agent_id"] if bad_field == "agent_id" else "grok",
            session_id=values["session_id"] if bad_field == "session_id" else lease.session_id,
            revision=values["revision"] if bad_field == "revision" else str(lease.revision),
            isolation=isolation,
            executable=executable,
            api_key_resolver=lambda: "xai-test-key",
        )
        started = []
        monkeypatch.setattr(instance, "_start_client", lambda: started.append(True))
        with pytest.raises(_rtruntime.FenceRejected):
            instance.run(once=True)
        assert started == []
    finally:
        _rtruntime.release(lease)


def test_grok_launcher_transfers_claimed_lease_to_managed_adapter(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    executable = tmp_path / "grok"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    wake = tmp_path / "rt-grok-wake"
    wake.write_text("#!/bin/sh\n", encoding="utf-8")
    wake.chmod(0o755)
    observed = {}

    monkeypatch.setattr(_rtlauncher, "choose_launch_cwd", lambda _harness: project)
    monkeypatch.setattr(_rtlauncher, "set_launch_identity", lambda *_args: "grok")
    monkeypatch.setattr(_rtlauncher, "normalize_runtime_environment", lambda: tmp_path)
    monkeypatch.setattr(_rtlauncher, "harness_bin", lambda _harness: executable)
    monkeypatch.setattr(_rtlauncher, "grok_adapter_bin", lambda: wake)
    monkeypatch.setattr(_rtlauncher, "claim_launch_seat", lambda *_args: object())

    def fake_exec(path, argv):
        observed["path"] = path
        observed["argv"] = argv
        observed["grok_bin"] = _rtlauncher.os.environ["RT_GROK_BIN"]
        raise RuntimeError("exec captured")

    monkeypatch.setattr(_rtlauncher.os, "execv", fake_exec)
    with pytest.raises(RuntimeError, match="exec captured"):
        _rtlauncher.launch("grok", ["--once"])

    assert observed == {
        "path": str(wake),
        "argv": [str(wake), "--grok-bin", str(executable), "--once"],
        "grok_bin": str(executable),
    }
