#!/usr/bin/env python3
"""Run a scratch-only Grok ACP Stage 2 experiment.

This is a lab harness, not a Roundtable adapter. It creates a temporary
Roundtable project and registry, launches one user-owned Grok ACP stdio child
under an atomic lease marker, and records protocol, process, and mail
evidence. It never uses the default Roundtable registry or this checkout as
the lab project.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GROK = (
    Path.home()
    / ".local"
    / "share"
    / "rt-grok-stage1"
    / "node_modules"
    / ".bin"
    / "grok"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def scrub(value: str) -> str:
    return re.sub(
        r"(?i)(xai[_-]?api[_-]?key\s*[=:]\s*)\S+",
        r"\1<redacted>",
        value,
    )


def run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 20.0,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        return {
            "argv": args,
            "returncode": None,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "stdout": scrub(error.stdout or ""),
            "stderr": scrub(error.stderr or ""),
            "timeout": True,
        }
    return {
        "argv": args,
        "returncode": completed.returncode,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
        "stdout": scrub(completed.stdout),
        "stderr": scrub(completed.stderr),
        "timeout": False,
    }


class ACPClient:
    """Line-oriented JSON-RPC client with timestamped raw evidence."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        events_path: Path,
    ) -> None:
        self.command = command
        self.events_path = events_path
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self._events_lock = threading.Lock()
        self._received: queue.Queue[dict[str, Any]] = queue.Queue()
        self._next_id = 1
        self._threads: list[threading.Thread] = []
        self.permission_decisions: list[dict[str, Any]] = []
        self.process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        for name, stream in (
            ("stdout", self.process.stdout),
            ("stderr", self.process.stderr),
        ):
            assert stream is not None
            thread = threading.Thread(
                target=self._read_stream,
                args=(name, stream),
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def _record(self, event: dict[str, Any]) -> None:
        with self._events_lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(encode(event) + "\n")
        self._received.put(event)

    def _read_stream(self, name: str, stream: Any) -> None:
        while True:
            line = stream.readline()
            if line == "":
                self._record(
                    {"at": utc_now(), "kind": "stream_eof", "stream": name}
                )
                return
            line = line.rstrip("\n")
            event: dict[str, Any] = {
                "at": utc_now(),
                "kind": "receive",
                "stream": name,
                "line": scrub(line),
            }
            if name == "stdout":
                try:
                    event["json"] = json.loads(line)
                except json.JSONDecodeError:
                    event["json_error"] = True
            self._record(event)

    @staticmethod
    def _allowed_lab_command(command: str) -> bool:
        if command == "rt-inbox -f json":
            return True
        if not command.startswith("RT_FROM=grok rt-ack "):
            return False
        # Keep the lab approval narrow: one direct ack command, no shell
        # operators, substitutions, redirects, or backgrounding.
        if any(
            token in command
            for token in (";", "&", "|", ">", "<", "`", "$", "(", ")")
        ):
            return False
        return bool(
            re.fullmatch(
                r"RT_FROM=grok rt-ack [A-Za-z0-9_-]+(?: [^\r\n]*)?", command
            )
        )

    def _answer_permission(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        tool_call = params.get("toolCall") or {}
        raw_input = tool_call.get("rawInput") or {}
        command = str(raw_input.get("command") or "")
        allowed = self._allowed_lab_command(command)
        option_id = "allow-once" if allowed else "reject-once"
        response = {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "outcome": {
                    "outcome": "selected",
                    "optionId": option_id,
                }
            },
        }
        if self.process.stdin is None:
            raise RuntimeError("ACP stdin is unavailable")
        self.process.stdin.write(encode(response) + "\n")
        self.process.stdin.flush()
        decision = {
            "at": utc_now(),
            "command": command,
            "allowed": allowed,
            "option_id": option_id,
            "request_id": message.get("id"),
        }
        self.permission_decisions.append(decision)
        self._record(
            {
                "at": decision["at"],
                "kind": "permission_response",
                "json": response,
                "decision": decision,
            }
        )

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 12.0,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        request_id = self._next_id
        self._next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        started = time.monotonic()
        if self.process.stdin is None:
            raise RuntimeError("ACP stdin is unavailable")
        self.process.stdin.write(encode(payload) + "\n")
        self.process.stdin.flush()
        self._record({"at": utc_now(), "kind": "send", "json": payload})
        deadline = time.monotonic() + timeout
        notifications: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                event = self._received.get(timeout=0.1)
            except queue.Empty:
                if self.process.poll() is not None:
                    break
                continue
            if event.get("stream") != "stdout" or "json" not in event:
                continue
            message = event["json"]
            if not isinstance(message, dict):
                continue
            if (
                message.get("method") == "session/request_permission"
                and message.get("id") is not None
            ):
                self._answer_permission(message)
                notifications.append(message)
                continue
            if message.get("id") == request_id:
                return message, {
                    "request_id": request_id,
                    "method": method,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "alive_at_response": self.process.poll() is None,
                    "notifications_seen": notifications,
                }
            notifications.append(message)
        return None, {
            "request_id": request_id,
            "method": method,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "alive_at_response": self.process.poll() is None,
            "notifications_seen": notifications,
            "timeout_or_exit": True,
        }

    def terminate(self, *, force: bool = False) -> dict[str, Any]:
        if self.process.poll() is None:
            if force:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for thread in self._threads:
            thread.join(timeout=1)
        return {
            "pid": self.process.pid,
            "returncode": self.process.returncode,
            "alive": self.process.poll() is None,
        }


def isolated_env(
    *,
    lab_root: Path,
    project: Path,
    registry: Path,
    grok_home: Path,
    invalid_api_key: bool = False,
) -> dict[str, str]:
    home = lab_root / "home"
    for path in (
        home,
        lab_root / "xdg-config",
        lab_root / "xdg-data",
        lab_root / "xdg-cache",
        grok_home,
    ):
        path.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "HOME": str(home),
            "GROK_HOME": str(grok_home),
            "XDG_CONFIG_HOME": str(lab_root / "xdg-config"),
            "XDG_DATA_HOME": str(lab_root / "xdg-data"),
            "XDG_CACHE_HOME": str(lab_root / "xdg-cache"),
            "ROUNDTABLE_PROJECT_DIR": str(project),
            "RT_PROJECTS_FILE": str(registry),
            "RT_FROM": "grok",
            "PATH": f"{PROJECT_ROOT / 'bin'}:{os.environ.get('PATH', '')}",
        }
    )
    if invalid_api_key:
        environment["XAI_API_KEY"] = "xai-stage2-invalid"
    return environment


def bootstrap_project(lab_root: Path, environment: dict[str, str]) -> Path:
    project = lab_root / "project"
    project.mkdir(parents=True, exist_ok=True)
    result = run_command(
        [
            str(PROJECT_ROOT / "bin" / "roundtable-init"),
            "--here",
            "--no-git",
        ],
        cwd=project,
        env=environment,
    )
    if result["returncode"] != 0:
        raise RuntimeError(f"roundtable-init failed: {result}")
    (project / ".roundtable" / "agents.yaml").write_text(
        """schema: roundtable.agents.v1
project: "."
agents:
  codex:
    harness: codex
    instances:
      - id: codex
  grok:
    harness: grok-build
    instances:
      - id: grok
""",
        encoding="utf-8",
    )
    (project / ".roundtable" / "agents.yaml").chmod(0o600)
    return project


def send_lab_mail(environment: dict[str, str], project: Path) -> dict[str, Any]:
    sender_environment = dict(environment)
    sender_environment["RT_FROM"] = "codex"
    return run_command(
        [
            str(PROJECT_ROOT / "bin" / "rt-say"),
            "grok",
            "directive",
            "STAGE2 LAB MAIL: run rt-inbox -f json, act only in this scratch project, then RT_FROM=grok rt-ack the exact message id and report the result.",
        ],
        cwd=project,
        env=sender_environment,
    )


def list_lab_inbox(environment: dict[str, str], project: Path) -> dict[str, Any]:
    result = run_command(
        [str(PROJECT_ROOT / "bin" / "rt-inbox"), "-f", "json"],
        cwd=project,
        env=environment,
    )
    try:
        result["records"] = json.loads(result["stdout"] or "[]")
    except json.JSONDecodeError:
        result["records_error"] = True
    return result


def lease(lease_path: Path, command: list[str], project: Path) -> dict[str, Any]:
    payload = {
        "created_at": utc_now(),
        "project": str(project),
        "command": command,
        "owner_pid": os.getpid(),
        "revision": 1,
    }
    descriptor = os.open(
        lease_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.write(descriptor, (encode(payload) + "\n").encode())
    finally:
        os.close(descriptor)
    return payload


def start_client(
    grok: Path,
    project: Path,
    environment: dict[str, str],
    events_path: Path,
) -> ACPClient:
    command = [str(grok), "--no-auto-update", "agent", "--no-leader", "stdio"]
    return ACPClient(
        command,
        cwd=project,
        env=environment,
        events_path=events_path,
    )


def initialize(client: ACPClient) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    return client.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientInfo": {
                "name": "roundtable-grok-stage2-lab",
                "version": "0",
            },
            "capabilities": {},
        },
    )


def new_session(
    client: ACPClient,
    project: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    return client.request(
        "session/new",
        {"cwd": str(project), "mcpServers": []},
    )


def prompt(
    client: ACPClient,
    session_id: str,
    text: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    return client.request(
        "session/prompt",
        {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        },
        timeout=60.0,
    )


def session_id(response: dict[str, Any] | None) -> str | None:
    if not response or not isinstance(response.get("result"), dict):
        return None
    result = response["result"]
    for key in ("sessionId", "session_id", "id"):
        if result.get(key):
            return str(result[key])
    return None


def agent_id(response: dict[str, Any] | None) -> str | None:
    if not response or not isinstance(response.get("result"), dict):
        return None
    metadata = response["result"].get("_meta")
    if isinstance(metadata, dict) and metadata.get("agentId"):
        return str(metadata["agentId"])
    return None


def agent_instance_id(response: dict[str, Any] | None) -> str | None:
    if not response or not isinstance(response.get("result"), dict):
        return None
    metadata = response["result"].get("_meta")
    if isinstance(metadata, dict) and metadata.get("agentInstanceId"):
        return str(metadata["agentInstanceId"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="grok_acp_stage2_lab")
    parser.add_argument("--grok", type=Path, default=DEFAULT_GROK)
    parser.add_argument(
        "--lab-root",
        type=Path,
        help="existing directory under the system temp directory",
    )
    args = parser.parse_args()

    grok = args.grok.expanduser().resolve()
    if not grok.is_file() or not os.access(grok, os.X_OK):
        raise SystemExit(f"grok executable is not runnable: {grok}")
    if args.lab_root is None:
        lab_root = Path(tempfile.mkdtemp(prefix="rt-grok-stage2-"))
    else:
        lab_root = args.lab_root.expanduser().resolve()
        temp_roots = {
            Path(tempfile.gettempdir()).resolve(),
            Path("/private/tmp").resolve(),
        }
        if not any(
            root == lab_root or root in lab_root.parents for root in temp_roots
        ):
            roots = ", ".join(str(root) for root in sorted(temp_roots))
            raise SystemExit(f"--lab-root must be under one of {roots}: {lab_root}")
        lab_root.mkdir(parents=True, exist_ok=True)
    if lab_root == PROJECT_ROOT or PROJECT_ROOT in lab_root.parents:
        raise SystemExit("refusing to use the checkout or a checkout child as lab root")

    registry = lab_root / "projects.yaml"
    grok_home = lab_root / "grok-home"
    environment = isolated_env(
        lab_root=lab_root,
        project=lab_root,
        registry=registry,
        grok_home=grok_home,
    )
    project = bootstrap_project(lab_root, environment)
    environment["ROUNDTABLE_PROJECT_DIR"] = str(project)
    events_path = lab_root / "acp-events.jsonl"
    lease_path = lab_root / "grok-stdio.lease"
    command = [str(grok), "--no-auto-update", "agent", "--no-leader", "stdio"]
    result: dict[str, Any] = {
        "schema": "roundtable.grok_stage2_lab.v1",
        "started_at": utc_now(),
        "lab_root": str(lab_root),
        "project": str(project),
        "registry": str(registry),
        "grok": str(grok),
        "command": command,
        "mail_delivery": {},
        "mail_before_prompt": {},
        "mail_after_prompt": {},
        "primary": {},
        "invalid_auth_probe": {},
        "death_probe": {},
        "restart_probe": {},
    }
    result["lease"] = lease(lease_path, command, project)

    primary: ACPClient | None = None
    try:
        primary = start_client(grok, project, environment, events_path)
        result["lease"]["child_pid"] = primary.process.pid
        initialized, init_meta = initialize(primary)
        result["primary"]["initialize"] = {
            "response": initialized,
            "meta": init_meta,
            "agent_id": agent_id(initialized),
            "agent_instance_id": agent_instance_id(initialized),
            "auth_methods": (
                initialized.get("result", {}).get("authMethods", [])
                if initialized
                else []
            ),
        }
        created, new_meta = new_session(primary, project)
        result["primary"]["session_new"] = {
            "response": created,
            "meta": new_meta,
        }
        current_session = session_id(created)
        result["primary"]["session_id"] = current_session
        first_prompt: dict[str, Any] | None = None
        if current_session:
            first_response, first_meta = prompt(
                primary,
                current_session,
                "Reply with exactly ACP-STAGE2-ALIVE. Do not use tools.",
            )
            first_prompt = {
                "response": first_response,
                "meta": first_meta,
            }
        result["primary"]["prompt_one"] = first_prompt
        result["mail_delivery"] = send_lab_mail(environment, project)
        result["mail_before_prompt"] = list_lab_inbox(environment, project)
        if current_session and primary.process.poll() is None:
            second_response, second_meta = prompt(
                primary,
                current_session,
                "A lab Roundtable message has landed. Run rt-inbox -f json, act only on that scratch mail, then run RT_FROM=grok rt-ack on the exact message id. Report the final inbox output and do not touch anything outside the lab project.",
            )
            result["primary"]["prompt_two_mail_drain"] = {
                "response": second_response,
                "meta": second_meta,
            }
        result["mail_after_prompt"] = list_lab_inbox(environment, project)
        result["primary"]["permission_decisions"] = primary.permission_decisions
        result["primary"]["alive_before_close"] = primary.process.poll() is None
    finally:
        if primary is not None:
            result["primary"]["close"] = primary.terminate()

    invalid_environment = isolated_env(
        lab_root=lab_root,
        project=project,
        registry=registry,
        grok_home=grok_home,
        invalid_api_key=True,
    )
    invalid: ACPClient | None = None
    try:
        invalid = start_client(grok, project, invalid_environment, events_path)
        initialized, init_meta = initialize(invalid)
        created, new_meta = new_session(invalid, project)
        invalid_session = session_id(created)
        invalid_prompt = None
        if invalid_session:
            invalid_response, invalid_meta = prompt(
                invalid,
                invalid_session,
                "Reply with exactly ACP-STAGE2-INVALID-AUTH.",
            )
            invalid_prompt = {
                "response": invalid_response,
                "meta": invalid_meta,
            }
        result["invalid_auth_probe"] = {
            "initialize": {"response": initialized, "meta": init_meta},
            "session_new": {"response": created, "meta": new_meta},
            "prompt": invalid_prompt,
            "close": None,
        }
    finally:
        if invalid is not None:
            result["invalid_auth_probe"]["close"] = invalid.terminate()

    death: ACPClient | None = None
    try:
        death = start_client(grok, project, environment, events_path)
        initialized, init_meta = initialize(death)
        killed_pid = death.process.pid
        death.terminate(force=True)
        result["death_probe"] = {
            "initialize": {"response": initialized, "meta": init_meta},
            "killed_pid": killed_pid,
            "after_kill": {
                "returncode": death.process.returncode,
                "alive": death.process.poll() is None,
            },
        }
    finally:
        if death is not None and death.process.poll() is None:
            death.terminate(force=True)

    restart: ACPClient | None = None
    try:
        restart = start_client(grok, project, environment, events_path)
        initialized, init_meta = initialize(restart)
        result["restart_probe"] = {
            "initialize": {"response": initialized, "meta": init_meta},
            "new_pid": restart.process.pid,
            "new_agent_id": agent_id(initialized),
            "new_agent_instance_id": agent_instance_id(initialized),
        }
    finally:
        if restart is not None:
            result["restart_probe"]["close"] = restart.terminate()

    result["finished_at"] = utc_now()
    result["events_path"] = str(events_path)
    result_path = lab_root / "result.json"
    result_path.write_text(encode(result) + "\n", encoding="utf-8")
    lease_path.unlink(missing_ok=True)
    summary = {
        "schema": result["schema"],
        "lab_root": str(lab_root),
        "result": str(result_path),
        "events": str(events_path),
        "primary_session_error": (
            result["primary"].get("session_new", {}).get("response", {}).get("error")
            if result["primary"].get("session_new", {}).get("response")
            else "no-response"
        ),
        "mail_before_records": len(
            result["mail_before_prompt"].get("records", [])
        ),
        "mail_after_records": len(result["mail_after_prompt"].get("records", [])),
        "primary_alive_before_close": result["primary"].get("alive_before_close"),
        "invalid_auth_error": (
            result["invalid_auth_probe"].get("prompt", {})
            .get("response", {})
            .get("error")
            or result["invalid_auth_probe"].get("session_new", {})
            .get("response", {})
            .get("error")
        ),
        "death_returncode": result["death_probe"].get("after_kill", {}).get(
            "returncode"
        ),
        "restart_agent_id": result["restart_probe"].get("new_agent_id"),
    }
    print(encode(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
