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
import re
import shutil
import socket
import subprocess
import sys
import threading

import pytest

import _kit as kit


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
        messages = request.get("messages", [])
        user_indexes = [
            index for index, message in enumerate(messages)
            if message.get("role") == "user"
        ]
        latest_user = user_indexes[-1] if user_indexes else -1
        has_tool_result = any(
            message.get("role") == "tool"
            for message in messages[latest_user + 1:]
        )
        message_id = self.server.message_id_for_request(request, has_tool_result)
        if has_tool_result:
            chunks = [
                {
                    "id": f"lab-final-{message_id}",
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
            command = f"rt-ack --fenced {message_id.removesuffix('.md')} lab-ack"
            arguments = json.dumps({"command": command})
            chunks = [
                {
                    "id": f"lab-tool-{message_id}",
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
    def __init__(self, message_id: str | None = None, message_ids: list[str] | None = None):
        super().__init__(("127.0.0.1", 0), _FakeModelHandler)
        self.message_ids = list(message_ids or ([] if message_id is None else [message_id]))
        self.message_id = message_id or ""
        self._next_message = 0
        self._current_message = message_id or None
        self.requests: list[dict] = []

    def message_id_for_request(self, request: dict, has_tool_result: bool) -> str:
        if has_tool_result and self._current_message:
            return self._current_message
        for message in reversed(request.get("messages", [])):
            if message.get("role") != "user":
                continue
            content = message.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
            match = re.search(r"The exact files are:\s*([^,]+?\.md)", str(content))
            if match:
                self._current_message = match.group(1)
                return self._current_message
        if self._next_message >= len(self.message_ids):
            raise RuntimeError("fake model received more generations than the soak plan")
        self._current_message = self.message_ids[self._next_message]
        self._next_message += 1
        return self._current_message


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
    kit.write_project(
        project,
        [kit.CLAUDE, kit.OPENCLAW],
        project=str(project),
    )
    state = project / ".roundtable"
    (state / "messages").mkdir()
    (state / "locks").mkdir()
    register_project(project, path=registry)


@pytest.mark.integration
def test_real_isolated_gateway_send_wake_drain_ack(tmp_path, monkeypatch):
    executable = os.environ.get("RT_OPENCLAW_LAB_BIN", "").strip()
    if not executable:
        pytest.skip("set RT_OPENCLAW_LAB_BIN to run the real OpenClaw lab")
    gateway_bin = Path(executable).expanduser().resolve()
    if not gateway_bin.is_file():
        pytest.skip(f"OpenClaw lab executable is unavailable: {gateway_bin}")
    try:
        cycles = int(os.environ.get("RT_OPENCLAW_SOAK_CYCLES", "1"))
    except ValueError:
        pytest.fail("RT_OPENCLAW_SOAK_CYCLES must be an integer")
    if cycles < 1:
        pytest.fail("RT_OPENCLAW_SOAK_CYCLES must be positive")

    project = tmp_path / "project"
    project.mkdir()
    registry = tmp_path / "projects.yaml"
    _write_lab_project(project, registry)
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "roundtable-runtime"))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(tmp_path / "roundtable-runtime"))
    monkeypatch.setenv("RT_OPENCLAW_AGENT_ID", "main")

    mailbox = resolve_project_mailbox(project, registry_path=registry)
    new_dir = mailbox.inbox_dir / "openclaw" / "new"

    lease = claim(project, "openclaw", "openclaw", owner_pid=os.getpid())
    fake_model = _FakeModelServer()
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
                                "models": [{"id": "lab-model", "name": "lab-model", "contextWindow": 131072, "maxTokens": 2048}],
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
    gateway_pid = None
    rss_samples: list[int] = []
    fd_samples: list[int] = []
    message_paths: list[Path] = []

    def sample_process() -> None:
        if gateway_pid is None:
            return
        rss = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(gateway_pid)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            rss_samples.append(int(rss.stdout.strip()))
        except ValueError:
            pass
        lsof = shutil.which("lsof")
        if lsof:
            descriptors = subprocess.run(
                [lsof, "-p", str(gateway_pid)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if descriptors.returncode == 0:
                fd_samples.append(max(0, len(descriptors.stdout.splitlines()) - 1))

    def send_one(index: int) -> Path:
        sent = subprocess.run(
            [
                sys.executable,
                str(BIN / "rt-say"),
                "--no-nudge",
                "openclaw",
                "directive",
                f"perform isolated lab acknowledgement cycle {index}",
            ],
            cwd=project,
            env={**os.environ, "RT_FROM": "claude"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert sent.returncode == 0, sent.stderr
        normal = sorted(path for path in new_dir.glob("*.md") if not path.name.startswith("ack-"))
        assert len(normal) == 1
        return normal[0]

    try:
        try:
            adapter.start_gateway()
            adapter.wait_ready(60)
            gateway_pid = adapter.process.pid if adapter.process is not None else None
            for index in range(cycles):
                message_path = send_one(index)
                message_paths.append(message_path)
                fake_model.message_ids.append(message_path.stem)
                adapter._fence_update(names=(message_path.name,), attempts=1)
                adapter.run_generation((message_path.name,), timeout=30, drain_timeout=30)
                sample_process()
        except Exception as error:
            log_path = isolation.logs / "gateway.log"
            detail = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            pytest.fail(
                f"real OpenClaw lab failed: {error}; fake provider requests="
                f"{len(fake_model.requests)}; gateway log tail=\n{detail[-12000:]}"
            )
    finally:
        if adapter.wake_claimed:
            try:
                adapter._clear_fence()
            except Exception:
                pass
        adapter.stop_gateway()
        fake_model.shutdown()
        fake_model.server_close()
        thread.join(timeout=2)
        release(lease)

    normal_new = [path for path in new_dir.iterdir() if not path.name.startswith("ack-")]
    assert not normal_new
    for message_path in message_paths:
        assert (mailbox.inbox_dir / "openclaw" / "cur" / message_path.name).is_file()
    assert len(message_paths) == cycles
    assert len(fake_model.requests) >= 2 * cycles
    if cycles >= 5:
        assert max(rss_samples) - min(rss_samples) <= 256 * 1024
        if fd_samples:
            assert max(fd_samples) - min(fd_samples) <= 32
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        with pytest.raises(OSError):
            probe.connect(("127.0.0.1", isolation.port))
    assert _snapshot_tree(global_tmp) == global_tmp_before
    config = json.loads(isolation.config_path.read_text(encoding="utf-8"))
    assert Path(config["logging"]["file"]).is_relative_to(isolation.root)
