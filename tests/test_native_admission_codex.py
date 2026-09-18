"""Contracts of the opt-in probe, without requiring an installed Codex."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import threading
from urllib.request import Request, urlopen

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts/native_admission/codex_probe.py"
SPEC = importlib.util.spec_from_file_location("native_admission_codex_probe", SOURCE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class Transport:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.sent = []

    def send_json(self, value):
        self.sent.append(value)

    def recv_json(self, _timeout):
        return next(self.replies)


class Evidence:
    def __init__(self):
        self.rows = []

    def record(self, **row):
        self.rows.append(row)


def test_probe_child_env_does_not_inherit_credentials_or_runtime_routes(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "CUSTOM_PROVIDER_TOKEN", "CODEX_THREAD_ID",
                 "CODEX_APP_SERVER_USE_LOCAL_DAEMON", "RT_SESSION_ID", "HTTP_PROXY"):
        monkeypatch.setenv(name, "must-not-propagate")
    environment = probe.isolated_env(tmp_path)
    assert set(environment) == {"HOME", "CODEX_HOME", "TMPDIR", "PATH", "LANG"}
    assert "must-not-propagate" not in environment.values()
    for key in ("HOME", "CODEX_HOME", "TMPDIR"):
        assert Path(environment[key]).is_relative_to(tmp_path)


def test_rpc_does_not_answer_approval_even_when_server_request_id_matches():
    approval = {"id": 1, "method": "item/commandExecution/requestApproval", "params": {}}
    notification = {"method": "thread/status/changed", "params": {"status": {"type": "active"}}}
    response = {"id": 1, "result": {"ok": True}}
    transport = Transport([approval, notification, response])
    rpc = probe.Rpc(transport, Evidence(), "test")
    assert rpc.call("thread/read", {}) == response
    assert rpc.pending == [approval, notification]
    assert transport.sent == [{"id": 1, "method": "thread/read", "params": {}}]


def test_rpc_preserves_unrelated_response_and_reports_protocol_error():
    unrelated = {"id": 99, "result": {}}
    denied = {"id": 1, "error": {"code": -1, "message": "active writer"}}
    rpc = probe.Rpc(Transport([unrelated, denied]), Evidence(), "test")
    assert rpc.call("thread/resume", {}) == denied
    assert rpc.pending == [unrelated]
    with pytest.raises(RuntimeError, match="active writer"):
        probe.result(denied)


def test_completion_matches_exact_turn_and_retains_other_notifications():
    unrelated = {"method": "turn/completed", "params": {"turn": {"id": "other"}}}
    expected = {"method": "turn/completed", "params": {"turn": {"id": "expected"}}}
    rpc = probe.Rpc(Transport([]), Evidence(), "test")
    rpc.pending = [unrelated, expected]
    assert rpc.event("turn/completed", "expected") == expected
    assert rpc.pending == [unrelated]


def test_duplicate_experiment_replays_same_wire_id_without_local_dedup():
    transport = Transport([])
    rpc = probe.Rpc(transport, Evidence(), "test")
    payload = probe.mail_params("isolated-thread", "mail-marker")
    rpc.send("turn/start", payload, request_id=9000)
    rpc.send("turn/start", payload, request_id=9000)
    assert transport.sent[0] == transport.sent[1]
    assert payload["input"] == []
    assert payload["toolOutput"]["output"] == "mail-marker"


def test_model_gate_holds_sampling_and_captures_external_input():
    evidence = Evidence()
    model = probe.MockModel(evidence)
    gate = model.hold_next()
    finished = threading.Event()
    replies = []

    def request():
        body = json.dumps({"input": [{"type": "function_call_output", "output": "mail"}]}).encode()
        with urlopen(Request(f"http://127.0.0.1:{model.server.server_port}/v1/responses",
                             data=body, headers={"Content-Type": "application/json"}), timeout=5) as response:
            replies.append(response.read())
        finished.set()

    worker = threading.Thread(target=request)
    worker.start()
    try:
        model.wait_requests(1)
        assert not finished.is_set()
        assert model.requests[0]["input"][0]["output"] == "mail"
        gate.set()
        assert finished.wait(5)
        assert b"response.completed" in replies[0]
        assert b"PROBE_OK" in replies[0]
    finally:
        gate.set()
        worker.join(timeout=5)
        model.close()


def test_failed_start_records_bounded_failure_and_cleans_up(tmp_path, monkeypatch):
    class StuckProcess:
        terminated = False

        def poll(self):
            return 0 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout):
            assert self.terminated
            return 0

    process = StuckProcess()
    times = iter([0, 11])
    monkeypatch.setattr(probe.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(probe.subprocess, "check_output", lambda *args, **kwargs: "codex-cli fixture\n")
    summary = probe.run(Path("/fixture/codex"), tmp_path)
    assert summary["failure_type"] == "RuntimeError"
    assert summary["all_probe_servers_exited"] is True
    assert process.terminated
    assert summary["all_observations_reproduced"] is False
    assert summary["visible_session_attach"] == "unverified"
    assert str(tmp_path) not in json.dumps(summary)
    assert json.loads((tmp_path / "summary.json").read_text()) == summary
