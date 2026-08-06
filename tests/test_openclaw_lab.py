"""Opt-in real OpenClaw Gateway E2E.

The normal suite never installs or starts OpenClaw. Set
``RT_OPENCLAW_LAB_BIN`` to an isolated package executable to run this test.
The fake provider only returns a deterministic ``exec`` tool call which
acknowledges the test message, then a final assistant response.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))
sys.path.insert(0, str(ROOT))

from _rtlib import register_project, resolve_project_mailbox  # noqa: E402
from _rtruntime import claim, release  # noqa: E402
from integrations.openclaw.roundtable import (  # noqa: E402
    OpenClawAdapter,
    create_isolation,
)


class _FakeModelHandler(BaseHTTPRequestHandler):
    server: "_FakeModelServer"

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(request)
        has_tool_result = any(message.get("role") == "tool" for message in request.get("messages", []))
        if has_tool_result:
            chunks = [
                {
                    "id": "lab-final",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "lab ack complete"}, "finish_reason": None}],
                },
                {
                    "id": "lab-final",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
        else:
            command = f"rt-ack --fenced {self.server.message_id} lab-ack"
            arguments = json.dumps({"command": command})
            chunks = [
                {
                    "id": "lab-tool",
                    "object": "chat.completion.chunk",
                    "choices": [{
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [{
                                "index": 0,
                                "id": "call-lab-ack",
                                "type": "function",
                                "function": {"name": "exec", "arguments": arguments},
                            }],
                        },
                        "finish_reason": None,
                    }],
                },
                {
                    "id": "lab-tool",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                },
            ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode("utf-8") + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *_args):
        return


class _FakeModelServer(ThreadingHTTPServer):
    def __init__(self, message_id: str):
        super().__init__(("127.0.0.1", 0), _FakeModelHandler)
        self.message_id = message_id
        self.requests: list[dict] = []


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int, int]]:
    """Capture paths and metadata without reading a potentially large log."""

    if not root.exists():
        return {}
    snapshot = {}
    for path in sorted(root.rglob("*")):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        snapshot[str(path.relative_to(root))] = (stat.st_mode, stat.st_size, stat.st_mtime_ns)
    return snapshot


def _write_lab_project(project: Path, registry: Path) -> None:
    state = project / ".roundtable"
    (state / "messages").mkdir(parents=True)
    (state / "locks").mkdir()
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  claude:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude\n"
        "  openclaw:\n"
        "    harness: openclaw\n"
        "    instances:\n"
        "      - id: openclaw\n",
        encoding="utf-8",
    )
    register_project(project, path=registry)


@pytest.mark.integration
def test_real_isolated_gateway_send_wake_drain_ack(tmp_path, monkeypatch):
    executable = os.environ.get("RT_OPENCLAW_LAB_BIN", "").strip()
    if not executable:
        pytest.skip("set RT_OPENCLAW_LAB_BIN to run the real OpenClaw lab")
    gateway_bin = Path(executable).expanduser().resolve()
    if not gateway_bin.is_file():
        pytest.skip(f"OpenClaw lab executable is unavailable: {gateway_bin}")

    project = tmp_path / "project"
    project.mkdir()
    registry = tmp_path / "projects.yaml"
    _write_lab_project(project, registry)
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "roundtable-runtime"))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(tmp_path / "roundtable-runtime"))
    monkeypatch.setenv("RT_OPENCLAW_AGENT_ID", "main")

    sent = subprocess.run(
        [
            sys.executable,
            str(BIN / "rt-say"),
            "--no-nudge",
            "openclaw",
            "directive",
            "perform the isolated lab acknowledgement",
        ],
        cwd=project,
        env={**os.environ, "RT_FROM": "claude"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert sent.returncode == 0, sent.stderr
    mailbox = resolve_project_mailbox(project, registry_path=registry)
    new_dir = mailbox.inbox_dir / "openclaw" / "new"
    message_paths = sorted(new_dir.glob("*.md"))
    assert len(message_paths) == 1
    message_id = message_paths[0].stem

    lease = claim(project, "openclaw", "openclaw", owner_pid=os.getpid())
    fake_model = _FakeModelServer(message_id)
    thread = threading.Thread(target=fake_model.serve_forever, daemon=True)
    thread.start()
    isolation = create_isolation(project, runtime_root=tmp_path / "openclaw-runtime")
    isolation.config_path.write_text(
        json.dumps(
            {
                "gateway": {
                    "mode": "local",
                    "bind": "loopback",
                    "port": isolation.port,
                    "auth": {"mode": "token", "token": isolation.token},
                },
                "models": {
                    "mode": "replace",
                    "providers": {
                        "lab": {
                            "baseUrl": f"http://127.0.0.1:{fake_model.server_port}/v1",
                            "api": "openai-completions",
                            "apiKey": "lab-key",
                            "models": [{"id": "lab-model", "name": "lab-model", "contextWindow": 32768, "maxTokens": 2048}],
                        }
                    },
                },
                "agents": {
                    "defaults": {
                        "workspace": str(project),
                        "model": {"primary": "lab/lab-model"},
                        "models": {"lab/lab-model": {}},
                    },
                    "list": [{"id": "main", "workspace": str(project)}],
                },
                "tools": {
                    "exec": {
                        "host": "gateway",
                        "security": "full",
                        "ask": "off",
                        "pathPrepend": [str(BIN)],
                    }
                },
                "hooks": {"enabled": False},
                "cron": {"enabled": False},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    global_tmp = Path("/tmp/openclaw")
    global_tmp_before = _snapshot_tree(global_tmp)
    adapter = OpenClawAdapter(
        project,
        agent_id="openclaw",
        session_id=lease.session_id,
        revision=str(lease.revision),
        isolation=isolation,
        executable=gateway_bin,
        run_timeout=30,
        drain_timeout=30,
    )
    try:
        try:
            assert adapter.run(once=True, ready_timeout=60) == 0
        except Exception as error:
            log_path = isolation.logs / "gateway.log"
            detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            pytest.fail(
                f"real OpenClaw lab failed: {error}; fake provider requests="
                f"{len(fake_model.requests)}; gateway log tail=\n{detail[-12000:]}"
            )
    finally:
        fake_model.shutdown()
        fake_model.server_close()
        thread.join(timeout=2)
        release(lease)

    assert not list(new_dir.iterdir())
    assert (mailbox.inbox_dir / "openclaw" / "cur" / message_paths[0].name).is_file()
    assert len(fake_model.requests) >= 2
    assert _snapshot_tree(global_tmp) == global_tmp_before
    config = json.loads(isolation.config_path.read_text(encoding="utf-8"))
    assert Path(config["logging"]["file"]).is_relative_to(isolation.root)
