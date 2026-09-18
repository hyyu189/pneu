"""Probe contracts only; these tests do not establish Claude runtime support."""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import select
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/native_admission/claude_probe.py"
spec = importlib.util.spec_from_file_location("claude_native_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.fixture
def db(tmp_path):
    with probe.ledger(tmp_path) as connection:
        yield connection


def test_inventory_preserves_unknown_and_waiting(db):
    summary = probe.summarize_agents([
        {"kind": "background", "state": "blocked", "sessionId": "a"},
        {"kind": "interactive", "status": "waiting", "waitingFor": "permission prompt", "pid": 1},
        {"kind": "interactive", "status": "idle", "pid": 2},
        {"kind": "interactive", "status": "future-value"},
    ])
    assert [s["observed_idle"] for s in summary["sessions"]] == [False, False, True, False]
    assert summary["sessions"][0]["status"] == "unknown"
    assert summary["sessions"][1]["waiting_for"] == "permission prompt"
    assert summary["sessions"][3]["status"] == "unknown"
    assert all(s["atomic_admission"] == "unverified" for s in summary["sessions"])
    assert probe.summarize_agents([], "missing")["matched_expected_id"] is False


def test_inventory_redacts_identity_and_rejects_unknown_shape():
    text = json.dumps(probe.summarize_agents([
        {"sessionId": "private-uuid", "pid": 99999, "cwd": "/private/path", "name": "private title", "status": "idle",
         "kind": "/private/path", "state": "private title", "waitingFor": "secret command"},
    ], "private-uuid"))
    for private in ("private-uuid", "99999", "/private/path", "private title", "secret command"):
        assert private not in text
    with pytest.raises(ValueError):
        probe.summarize_agents({"newSchema": []})


def test_duplicate_is_suppressed_until_explicit_repeat(db):
    assert probe.enqueue(db, "mail-1")["queued"]
    assert probe.enqueue(db, "mail-1")["duplicate"]
    assert probe.enqueue(db, "mail-1", repeat=True)["attempt"] == 2
    with pytest.raises(ValueError):
        probe.enqueue(db, "injected\ntext")


def test_transport_write_is_not_observation_or_ack(db):
    probe.enqueue(db, "mail-1")
    output = io.StringIO()
    assert probe.emit_one(db, output, "generation-1")
    message = json.loads(output.getvalue())
    assert message["method"] == "notifications/claude/channel"
    assert message["params"]["meta"]["message_id"] == "mail-1"
    report = probe.report(db)
    assert report["attempts"][0]["state"] == "written"
    assert report["observations"] == []
    assert report["native_admission"] == "unverified"
    assert not probe.emit_one(db, output, "generation-2")


def test_failed_write_and_crash_ambiguity_are_never_replayed(db):
    class Broken(io.StringIO):
        def flush(self):
            raise BrokenPipeError("receipt lost")

    probe.enqueue(db, "mail-1")
    with pytest.raises(BrokenPipeError):
        probe.emit_one(db, Broken(), "generation-1")
    assert probe.report(db)["attempts"][0]["state"] == "unknown"
    assert not probe.emit_one(db, io.StringIO(), "generation-2")
    with db:
        db.execute("UPDATE attempts SET state='writing'")
    assert not probe.emit_one(db, io.StringIO(), "generation-3")
    assert probe.enqueue(db, "mail-1", repeat=True)["queued"]


def test_observation_survives_lost_tool_response_and_restart(tmp_path):
    with probe.ledger(tmp_path) as db:
        probe.enqueue(db, "mail-1")
        with pytest.raises(ValueError):
            probe.observe(db, "mail-1", "generation-1")
        probe.emit_one(db, io.StringIO(), "generation-1")
        request = {"id": 1, "method": "tools/call", "params": {
            "name": "probe_observed", "arguments": {"message_id": "mail-1"}}}
        response = probe.handle(db, request, "generation-1")
        # Deliberately discard response. State was committed before it was built.
        assert "application_ack" in response["result"]["content"][0]["text"]
    with probe.ledger(tmp_path) as restarted:
        probe.handle(restarted, request, "generation-2")
        observations = probe.report(restarted)["observations"]
        assert len(observations) == 1
        assert observations[0]["generation"] == "generation-1"
        # Calls are not native executions: this one could be a lost-reply retry.
        assert len(probe.report(restarted)["observation_calls"]) == 2
        assert not probe.emit_one(restarted, io.StringIO(), "generation-2")


def test_protocol_omits_approval_capability_and_rejects_unknown_tools(db):
    response = probe.handle(db, {"id": 1, "method": "initialize", "params": {
        "protocolVersion": "2026-07-28"}}, "generation")
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert response["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
    error = probe.handle(db, {"id": 2, "method": "tools/call", "params": {"name": "approve"}}, "generation")
    assert error["result"]["isError"] is True


def test_stdio_lifecycle_handshake_disconnect_and_restart(tmp_path):
    run_dir = tmp_path / "probe"
    subprocess.run([sys.executable, str(SCRIPT), "init", "--run-dir", str(run_dir)], check=True, capture_output=True)
    config = json.loads((run_dir / "mcp.json").read_text())
    server = config["mcpServers"]["pneu-probe"]
    with probe.ledger(run_dir) as db:
        probe.enqueue(db, "mail-1")

    def receive(proc):
        assert select.select([proc.stdout], [], [], 5)[0], "MCP response timed out"
        return json.loads(proc.stdout.readline())

    def send(proc, *messages):
        proc.stdin.write(b"".join(json.dumps(message).encode() + b"\n" for message in messages))
        proc.stdin.flush()

    proc = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        assert not select.select([proc.stdout], [], [], 0.15)[0]
        send(proc, {"id": 1, "method": "initialize"}, {"method": "notifications/initialized"})
        assert receive(proc)["result"]["protocolVersion"] == probe.PROTOCOL
        notification = receive(proc)
        assert notification["params"]["meta"]["message_id"] == "mail-1"
        # Two request lines in a single write must both be read without hanging.
        send(proc, {"id": 2, "method": "ping"}, {"id": 3, "method": "ping"})
        assert [receive(proc)["id"], receive(proc)["id"]] == [2, 3]
        proc.stdin.close()
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    with probe.ledger(run_dir) as db:
        assert probe.report(db)["attempts"][0]["state"] == "written"
        assert not probe.emit_one(db, io.StringIO(), "reconnected-generation")
