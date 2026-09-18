#!/usr/bin/env python3
"""Opt-in Codex App Server experiment. Never connects to a real user backend.

Run with the project venv. RT_CODEX_BIN selects a binary via the existing
production resolver. A fresh private temp directory retains raw evidence;
stdout is a path-free summary. No daemon, global config or stored user thread
is used. This proves isolated protocol behavior, NOT visible-session attach.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "bin"))
from _rtcodex import WebSocketUnix, codex_bin  # noqa: E402


def isolated_env(root: Path) -> dict[str, str]:
    """Allowlist, rather than guessing every possible credential variable."""
    return {
        "HOME": str(root / "home"),
        "CODEX_HOME": str(root / "home" / ".codex"),
        "TMPDIR": str(root / "tmp"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
    }


class Evidence:
    def __init__(self, root: Path):
        self.handle = (root / "protocol.jsonl").open("w")
        self.lock = threading.Lock()

    def record(self, **fields):
        with self.lock:
            self.handle.write(json.dumps(fields) + "\n")
            self.handle.flush()


class Rpc:
    """Retain notifications/server requests; never auto-answer approvals."""

    def __init__(self, transport, evidence: Evidence, label: str):
        self.transport, self.evidence, self.label = transport, evidence, label
        self.sequence = 0
        self.pending = []

    def send(self, method, params, request_id=None):
        self.sequence += 1
        ident = self.sequence if request_id is None else request_id
        value = {"id": ident, "method": method, "params": params}
        self.evidence.record(client=self.label, direction="send", message=value)
        self.transport.send_json(value)
        return ident

    def receive(self, timeout=20):
        value = self.transport.recv_json(timeout)
        self.evidence.record(client=self.label, direction="recv", message=value)
        return value

    def call(self, method, params, request_id=None):
        ident = self.send(method, params, request_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = self.receive(max(0.01, deadline - time.monotonic()))
            if value.get("id") == ident and ("result" in value or "error" in value):
                return value
            self.pending.append(value)
        raise TimeoutError(method)

    def event(self, method, turn_id):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            for index, value in enumerate(self.pending):
                if (value.get("method") == method
                        and value.get("params", {}).get("turn", {}).get("id") == turn_id):
                    return self.pending.pop(index)
            self.pending.append(self.receive(max(0.01, deadline - time.monotonic())))
        raise TimeoutError(method)

    def close(self):
        self.transport.close()


def result(response):
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]


class MockModel:
    """Only a local Responses SSE fixture; no real model or credentials."""

    def __init__(self, evidence):
        self.evidence = evidence
        self.requests = []
        self.plans = queue.Queue()
        self.condition = threading.Condition()
        self.gates = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.evidence.record(model_path=self.path, model_request=body)
                with owner.condition:
                    owner.requests.append(body)
                    sequence = len(owner.requests)
                    owner.condition.notify_all()
                try:
                    gate = owner.plans.get_nowait()
                except queue.Empty:
                    gate = None
                if gate is not None and not gate.wait(20):
                    self.send_error(504, "probe gate timeout")
                    return
                item = {"type": "message", "id": f"msg_{sequence}",
                        "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "PROBE_OK",
                                     "annotations": []}]}
                events = [
                    {"type": "response.created", "response": {
                        "id": f"resp_{sequence}", "status": "in_progress", "output": []}},
                    {"type": "response.output_item.done", "output_index": 0, "item": item},
                    {"type": "response.completed", "response": {
                        "id": f"resp_{sequence}", "object": "response",
                        "status": "completed", "output": [item],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}},
                ]
                data = "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                               for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def hold_next(self):
        gate = threading.Event()
        self.gates.append(gate)
        self.plans.put(gate)
        return gate

    def wait_requests(self, count):
        with self.condition:
            if not self.condition.wait_for(lambda: len(self.requests) >= count, timeout=20):
                raise TimeoutError("mock model request")

    def close(self):
        for gate in self.gates:
            gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def mail_params(thread_id, marker):
    return {"threadId": thread_id, "input": [], "toolOutput": {
        "name": "pneu_probe_mail", "namespace": None, "output": marker}}


def run(binary: Path, root: Path) -> dict:
    env = isolated_env(root)
    for path in (root / "workspace", Path(env["HOME"]),
                 Path(env["CODEX_HOME"]), Path(env["TMPDIR"])):
        path.mkdir(parents=True, exist_ok=True)
    evidence = Evidence(root)
    mock = MockModel(evidence)
    children, clients, logs = [], [], []
    summary = {
        "evidence_class": "isolated_protocol", "visible_session_attach": "unverified",
        "approval_wait": "unverified", "compaction": "unverified", "observations": {},
    }
    checks = summary["observations"]
    config = (
        'model = "probe-model"\nmodel_provider = "probe"\n'
        '[model_providers.probe]\nname = "Local fixture"\n'
        f'base_url = "http://127.0.0.1:{mock.server.server_port}/v1"\n'
        'wire_api = "responses"\nrequires_openai_auth = false\n'
        '[analytics]\nenabled = false\n'
    )
    Path(env["CODEX_HOME"], "config.toml").write_text(config)

    def server(label):
        sock = root / (label + ".sock")
        log = (root / (label + ".log")).open("wb")
        logs.append(log)
        process = subprocess.Popen(
            [str(binary), "app-server", "--listen", "unix://" + str(sock)],
            cwd=root / "workspace", env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, start_new_session=True,
        )
        children.append(process)
        deadline = time.monotonic() + 10
        while not sock.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError("isolated app-server failed to start; see local log")
            time.sleep(0.05)
        return process, sock

    def connect(sock, label):
        client = Rpc(WebSocketUnix(sock, timeout=5), evidence, label)
        clients.append(client)
        result(client.call("initialize", {"clientInfo": {
            "name": "pneu_native_admission_probe", "version": "1"},
            "capabilities": {"experimentalApi": True}}))
        client.transport.send_json({"method": "initialized", "params": {}})
        return client

    def history(client, thread_id):
        return result(client.call("thread/read", {
            "threadId": thread_id, "includeTurns": True}))["thread"]

    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    try:
        summary["binary_version"] = subprocess.check_output(
            [str(binary), "--version"], env=env, text=True, timeout=10).strip()
        evidence.record(binary=str(binary), version=summary["binary_version"])
        owner, sock = server("owner")
        first, second = connect(sock, "first"), connect(sock, "second")
        thread = result(first.call("thread/start", {
            "cwd": str(root / "workspace"), "model": "probe-model", "modelProvider": "probe",
            "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": False}))["thread"]
        thread_id = thread["id"]
        initial = result(first.call("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "Local fixture initial turn."}]}))["turn"]
        first.event("turn/completed", initial["id"])
        resumed = result(second.call("thread/resume", {"threadId": thread_id}))
        checks["same_backend_second_client_same_thread"] = resumed["thread"]["id"] == thread_id

        # Deterministic TOCTOU: an idle observation, then human input wins,
        # then ordinary tool-authority mail uses the public start operation.
        observed = history(second, thread_id)["status"]
        gate = mock.hold_next()
        request_count = len(mock.requests)
        human = result(first.call("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": "Fixture human turn held busy."}]}))["turn"]
        mock.wait_requests(request_count + 1)
        busy = history(second, thread_id)["status"]
        mail = result(second.call("turn/start", mail_params(thread_id, "RACE_MAIL_1")))["turn"]
        checks["idle_observed_before_human"] = observed["type"] == "idle"
        checks["human_active_before_mail"] = busy["type"] == "active"
        checks["ordinary_mail_steered_human_turn"] = human["id"] == mail["id"]
        gate.set()
        completed_first = first.event("turn/completed", human["id"])
        completed_second = second.event("turn/completed", human["id"])
        checks["same_turn_completion_seen_by_both_clients"] = (
            completed_first["params"]["turn"]["status"] == "completed"
            and completed_second["params"]["turn"]["status"] == "completed")
        checks["mail_reached_model_request_within_human_turn"] = any(
            "RACE_MAIL_1" in json.dumps(body.get("input"))
            for body in mock.requests[request_count + 1:])

        first.close()
        clients.remove(first)
        reconnect = connect(sock, "reconnected")
        resumed = result(reconnect.call("thread/resume", {"threadId": thread_id}))
        checks["reconnect_same_running_backend"] = (
            owner.poll() is None and resumed["thread"]["id"] == thread_id)

        # Withhold the caller's receipt, observe submission independently,
        # then replay exactly the same JSON-RPC id and mail payload.
        lost = connect(sock, "lost_receipt")
        result(lost.call("thread/resume", {"threadId": thread_id}))
        before = history(second, thread_id)
        gate = mock.hold_next()
        request_count = len(mock.requests)
        payload = mail_params(thread_id, "DUPLICATE_MAIL_1")
        lost.send("turn/start", payload, request_id=9000)
        mock.wait_requests(request_count + 1)
        lost.close()  # intentionally never read the turn/start response
        clients.remove(lost)
        gate.set()
        current = history(second, thread_id)
        accepted_id = current["turns"][-1]["id"]
        second.event("turn/completed", accepted_id)
        retry = connect(sock, "retry")
        result(retry.call("thread/resume", {"threadId": thread_id}))
        replayed = result(retry.call("turn/start", payload, request_id=9000))["turn"]
        retry.event("turn/completed", replayed["id"])
        after = history(retry, thread_id)
        checks["lost_receipt_submit_survived_disconnect"] = len(current["turns"]) > len(before["turns"])
        checks["replayed_request_created_distinct_turn"] = replayed["id"] != accepted_id
        checks["duplicate_mail_persisted_twice"] = sum(
            item.get("type") == "functionCallOutput" and item.get("output") == "DUPLICATE_MAIL_1"
            for turn in after["turns"] for item in turn["items"]) == 2

        _, other_sock = server("other")
        other = connect(other_sock, "other_backend")
        denied = other.call("thread/resume", {"threadId": thread_id})
        checks["other_backend_writer_rejected"] = (
            "already has an active writer" in denied.get("error", {}).get("message", ""))
        for client in list(clients):
            if client is not other:
                client.close()
                clients.remove(client)
        denied = other.call("thread/resume", {"threadId": thread_id})
        checks["disconnect_all_clients_keeps_writer"] = (
            owner.poll() is None
            and "already has an active writer" in denied.get("error", {}).get("message", ""))
        stop(owner)
        cold = result(other.call("thread/resume", {"threadId": thread_id}))["thread"]
        checks["cold_resume_after_owner_exit"] = cold["id"] == thread_id
        checks["cold_resume_preserves_turn_count"] = len(cold["turns"]) == len(after["turns"])
    except Exception as error:
        evidence.record(failure=repr(error))
        summary["failure_type"] = type(error).__name__
    finally:
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        for process in children:
            stop(process)
        mock.close()
        for log in logs:
            log.close()
        evidence.handle.close()
        summary["all_probe_servers_exited"] = all(p.poll() is not None for p in children)
        summary["all_observations_reproduced"] = (
            "failure_type" not in summary and bool(checks) and all(checks.values()))
        (root / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def main():
    # Pin one resolved executable before replacing the subprocess environment.
    binary = codex_bin().resolve(strict=True)
    # Short /tmp path stays under macOS Unix socket path-length limits.
    root = Path(tempfile.mkdtemp(prefix="pneu-codex-", dir="/tmp"))
    root.chmod(0o700)
    print(f"Local raw evidence (do not commit): {root}", file=sys.stderr)
    summary = run(binary, root)
    print(json.dumps(summary, indent=2))
    return 0 if summary["all_observations_reproduced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
