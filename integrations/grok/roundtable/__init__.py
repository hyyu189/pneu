"""Internal Grok ACP mail-drain lab for a fenced pneu seat.

The user-facing ``rt-grok`` seat is the native interactive Grok TUI. This
module remains directly invocable for isolated ACP regression labs only; it is
never selected by the seat launcher. pneu's authoritative maildir stays the
source of truth, and this supervisor continues to fail closed on lease,
authentication, permission, and protocol ambiguity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping


ADAPTER_VERSION = "0.3.0"
DEFAULT_READY_TIMEOUT = 45.0
DEFAULT_PROMPT_TIMEOUT = 600.0
DEFAULT_DRAIN_TIMEOUT = 600.0
DEFAULT_POLL_SECONDS = 0.25
TERMINAL_OK = frozenset({"end_turn", "completed", "success"})
SHELL_OPERATORS = frozenset({";", "&", "|", ">", "<", "`", "$", "(", ")"})


class GrokError(RuntimeError):
    """A fail-closed Grok adapter error."""


class GrokProtocolError(GrokError):
    """Grok did not speak the expected ACP protocol."""


class GrokUnavailableError(GrokProtocolError):
    """The ACP child disappeared or could not be reached."""


class GrokAuthenticationError(GrokProtocolError):
    """The authenticated Grok session could not be created."""


class GrokPermissionError(GrokError):
    """The ACP permission policy rejected a tool execution."""


class GrokRunError(GrokError):
    """A prompt ended without a usable terminal result."""


class GrokRunTimeout(GrokRunError):
    """A prompt or mail drain exceeded its bounded timeout."""


def _as_path(value: str | Path) -> Path:
    return Path(value).expanduser().absolute()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _project_key(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:32]


def _scrub(value: str) -> str:
    return re.sub(
        r"(?i)(xai[_-]?api[_-]?key\s*[=:]\s*)\S+",
        r"\1<redacted>",
        value,
    )


def _auth_error(response: dict[str, Any] | None) -> bool:
    if not isinstance(response, dict):
        return False
    error = response.get("error")
    if not isinstance(error, dict):
        return False
    rendered = json.dumps(error, sort_keys=True).lower()
    return any(
        marker in rendered
        for marker in (
            "authentication",
            "unauthenticated",
            "bad-credentials",
            "could not be validated",
            "expired",
            "incorrect api key",
            "invalid api key",
            "unauthorized",
            "401",
            "403",
        )
    )


def resolve_grok_api_key(environment: dict[str, str] | None = None) -> str:
    """Read an existing API key without copying or logging auth state.

    The preferred boundary is an already-exported ``XAI_API_KEY``.  For a
    normal logged-in host, the current Grok auth file is read once and only
    the key field is passed to the child environment.  Refresh tokens and all
    other auth material are deliberately ignored.
    """

    environment = environment or os.environ
    value = environment.get("XAI_API_KEY", "").strip()
    if value:
        return value
    auth_path = _as_path(
        environment.get("GROK_AUTH_PATH")
        or (Path.home() / ".grok" / "auth.json")
    )
    try:
        payload = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GrokAuthenticationError(
            "no usable Grok API key is available; set XAI_API_KEY or complete Grok login"
        ) from error

    candidates: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower().replace("-", "_") in {"key", "api_key", "apikey"}:
                    if isinstance(item, str) and item.strip():
                        candidates.append(item.strip())
                elif str(key).lower().replace("-", "_") not in {
                    "refresh_token",
                    "refreshtoken",
                    "access_token",
                    "accesstoken",
                }:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(payload)
    if candidates:
        return candidates[0]
    raise GrokAuthenticationError(
        "no usable Grok API key is available; set XAI_API_KEY or complete Grok login"
    )


def _managed_tool_dirs() -> tuple[Path, ...]:
    candidates: list[Path] = []
    install_prefix = os.environ.get("ROUNDTABLE_INSTALL_PREFIX", "").strip()
    if install_prefix:
        candidates.append(_as_path(install_prefix) / "bin")
    candidates.extend(
        (
            Path(__file__).resolve().parents[3] / "bin",
            Path(__file__).resolve().parents[4] / "bin",
            Path(sys.executable).resolve().parent,
        )
    )
    return tuple(dict.fromkeys(path for path in candidates if path.is_dir()))


@dataclass(frozen=True)
class GrokIsolation:
    """All process-owned Grok state for one project seat."""

    root: Path
    state_dir: Path
    home: Path
    tmp: Path
    logs: Path
    workspace: Path

    def environment(
        self,
        *,
        project_root: Path,
        executable: Path,
        api_key: str,
        registry: str = "",
    ) -> dict[str, str]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (self.state_dir, self.home, self.tmp, self.logs):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        node = shutil.which("node", path=os.environ.get("PATH", os.defpath))
        path_parts = [str(executable.parent), str(Path(sys.executable).resolve().parent)]
        if node:
            path_parts.append(str(Path(node).resolve().parent))
        path_parts.extend(str(path) for path in _managed_tool_dirs())
        path_parts.extend(part for part in os.defpath.split(os.pathsep) if part)
        purelib = sysconfig.get_paths().get("purelib", "")
        environment = {
            "PATH": os.pathsep.join(dict.fromkeys(path_parts)),
            "HOME": str(self.home),
            "GROK_HOME": str(self.state_dir),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "TMPDIR": str(self.tmp),
            "PYTHONPATH": purelib,
            "XAI_API_KEY": api_key,
            "ROUNDTABLE_PROJECT_DIR": str(project_root.resolve()),
            "RT_PROJECT_ROOT": str(project_root.resolve()),
            "RT_GROK_ISOLATION_ROOT": str(self.root.resolve()),
        }
        if registry:
            environment["RT_PROJECTS_FILE"] = registry
        return environment

    def assert_isolated(self, environment: dict[str, str]) -> None:
        root = self.root.resolve()
        for name in (
            "HOME",
            "GROK_HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "RT_GROK_ISOLATION_ROOT",
        ):
            value = environment.get(name)
            if not value or not _under(Path(value), root):
                raise GrokError(f"Grok isolation variable {name} escapes {root}")
        allowed_system = {
            Path(item).resolve()
            for item in os.defpath.split(os.pathsep)
            if item
        }
        executable_parent = Path(environment.get("RT_GROK_BIN", "/")).resolve().parent
        managed = {path.resolve() for path in _managed_tool_dirs()}
        for entry in environment.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            path = Path(entry).resolve()
            if path in allowed_system or path == executable_parent or path in managed:
                continue
            if not _under(path, root):
                if not any(
                    (path / name).is_file()
                    for name in ("node", "grok", "python3", "python")
                ):
                    raise GrokError(f"Grok PATH entry is not isolated: {entry}")


def create_isolation(
    project_root: str | Path,
    *,
    runtime_root: str | Path | None = None,
) -> GrokIsolation:
    project = _as_path(project_root).resolve()
    base = runtime_root or os.environ.get("RT_GROK_RUNTIME_DIR") or os.environ.get("RT_RUNTIME_DIR")
    base_path = _as_path(base) if base else _as_path(tempfile.gettempdir()) / "roundtable-runtime"
    root = base_path / "grok" / _project_key(project)
    if _under(root, project):
        raise GrokError("Grok isolation root must not be inside the project")
    isolation = GrokIsolation(
        root=root,
        state_dir=root / "state",
        home=root / "home",
        tmp=root / "tmp",
        logs=root / "logs",
        workspace=project,
    )
    isolation.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return isolation


def resolve_grok_bin(environment: dict[str, str] | None = None) -> Path:
    """Resolve one real Grok executable, never a cmux shim."""

    environment = environment or os.environ
    override = environment.get("RT_GROK_BIN", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(_as_path(override))
    home = _as_path(environment.get("HOME") or Path.home())
    candidates.extend((home / ".local" / "bin" / "grok", home / ".npm-global" / "bin" / "grok"))
    candidates.extend(
        _as_path(item) / "grok"
        for item in environment.get("PATH", "").split(os.pathsep)
        if item
    )
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.absolute()
        if str(candidate) in seen:
            continue
        seen.add(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            if "cmux-cli-shims" not in {part.lower() for part in candidate.parts}:
                return candidate
    hint = "RT_GROK_BIN" if override else "PATH or RT_GROK_BIN"
    raise GrokError(f"could not resolve an executable Grok binary; set {hint}")


def session_key(project_root: str | Path, agent_id: str = "grok") -> str:
    if not agent_id or not re.fullmatch(r"[A-Za-z0-9_-]+", agent_id):
        raise GrokError(f"invalid Grok agent id: {agent_id!r}")
    return f"grok:{agent_id}:roundtable-{_project_key(_as_path(project_root))[:20]}"


def wake_prompt(project_root: str | Path, agent_id: str, names: tuple[str, ...]) -> str:
    rendered = ", ".join(names)
    return (
        "[Roundtable Grok seat] A fenced durable mail generation is waiting "
        f"for agent {agent_id!r} in project {Path(project_root).resolve()}. "
        f"The exact files are: {rendered}. Work from the project root. First run "
        "`rt-inbox --fenced --archive-quiet-acks -f json`, then act on every "
        "non-ack message. When each message is handled, run "
        "`RT_FROM=grok rt-ack <msg_id> handled` once. Do not use hooks, "
        "another Grok process, or a second wake. Do not report success until "
        "the exact generation has left maildir new/."
    )


def _any_command_allowed(command: str) -> bool:
    """Default wake policy: the communication layer is not a permission gate.

    Harness-side permission models own execution policy (owner decision
    2026-08-11); the supervisor still records every decision in the audit
    trail. The pre-1.3.2 fenced mailroom-only policy remains available as an
    explicit opt-in through RT_GROK_WAKE_MAILROOM_ONLY=1.
    """

    del command
    return True


def wake_permission_policy(
    environment: Mapping[str, str] | None = None,
) -> Callable[[str], bool]:
    env = os.environ if environment is None else environment
    if env.get("RT_GROK_WAKE_MAILROOM_ONLY") == "1":
        return _mail_command_allowed
    return _any_command_allowed


def _mail_command_allowed(command: str) -> bool:
    if not command or any(token in command for token in SHELL_OPERATORS):
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    if tokens[:1] == ["RT_FROM=grok"]:
        tokens = tokens[1:]
    if tokens and tokens[0] == "rt-inbox":
        return tokens[1:] in (
            ["-f", "json"],
            ["--fenced", "--archive-quiet-acks", "-f", "json"],
        )
    if not tokens or tokens[0] != "rt-ack":
        return False
    if tokens[1:2] == ["--fenced"]:
        tokens = [tokens[0], *tokens[2:]]
    return (
        len(tokens) >= 2
        and bool(re.fullmatch(r"[A-Za-z0-9_-]+", tokens[1]))
        and len(tokens) <= 3
        and (len(tokens) == 2 or bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .,_:/-]*", tokens[2])))
    )


class ACPClient:
    """Small line-oriented ACP JSON-RPC client with bounded permissions."""

    def __init__(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        events_path: Path,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        permission_policy: Callable[[str], bool] | None = None,
    ) -> None:
        self.command = command
        self.events_path = events_path
        self.events_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.permission_policy = (
            permission_policy
            if permission_policy is not None
            else wake_permission_policy()
        )
        self.permission_decisions: list[dict[str, Any]] = []
        self._received: queue.Queue[dict[str, Any]] = queue.Queue()
        self._events_lock = threading.Lock()
        self._next_id = 1
        self._threads: list[threading.Thread] = []
        try:
            self.process = popen_factory(
                command,
                cwd=str(cwd),
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as error:
            raise GrokUnavailableError(f"could not start Grok ACP child: {error}") from error
        for name, stream in (("stdout", self.process.stdout), ("stderr", self.process.stderr)):
            if stream is None:
                continue
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
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self._received.put(event)

    def _read_stream(self, name: str, stream: Any) -> None:
        while True:
            line = stream.readline()
            if line == "":
                self._record({"at": time.time(), "kind": "stream_eof", "stream": name})
                return
            line = line.rstrip("\n")
            event: dict[str, Any] = {
                "at": time.time(),
                "kind": "receive",
                "stream": name,
                "line": _scrub(line),
            }
            if name == "stdout":
                try:
                    event["json"] = json.loads(line)
                except json.JSONDecodeError:
                    event["json_error"] = True
            self._record(event)

    def _answer_permission(self, message: dict[str, Any]) -> None:
        params = message.get("params") or {}
        tool_call = params.get("toolCall") or {}
        raw_input = tool_call.get("rawInput") or {}
        command = str(raw_input.get("command") or "")
        allowed = bool(self.permission_policy(command))
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
            raise GrokUnavailableError("Grok ACP stdin is unavailable")
        try:
            self.process.stdin.write(json.dumps(response) + "\n")
            self.process.stdin.flush()
        except OSError as error:
            raise GrokUnavailableError("Grok ACP permission response failed") from error
        decision = {
            "at": time.time(),
            "command": command,
            "allowed": allowed,
            "option_id": option_id,
            "request_id": message.get("id"),
        }
        self.permission_decisions.append(decision)
        self._record({"at": decision["at"], "kind": "permission_response", "decision": decision})

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        if self.process.poll() is not None:
            raise GrokUnavailableError(
                f"Grok ACP child exited with {self.process.returncode} before {method}"
            )
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        if self.process.stdin is None:
            raise GrokUnavailableError("Grok ACP stdin is unavailable")
        started = time.monotonic()
        try:
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except OSError as error:
            raise GrokUnavailableError(f"Grok ACP request failed for {method}") from error
        self._record({"at": time.time(), "kind": "send", "method": method, "request_id": request_id})
        deadline = time.monotonic() + timeout
        notifications: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                event = self._received.get(timeout=0.1)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise GrokUnavailableError(
                        f"Grok ACP child exited with {self.process.returncode} during {method}"
                    )
                continue
            if event.get("stream") != "stdout" or not isinstance(event.get("json"), dict):
                continue
            message = event["json"]
            if message.get("method") == "session/request_permission" and message.get("id") is not None:
                self._answer_permission(message)
                notifications.append(message)
                continue
            if message.get("id") == request_id:
                return message, {
                    "method": method,
                    "request_id": request_id,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    "alive": self.process.poll() is None,
                    "notifications": notifications,
                }
            notifications.append(message)
        raise GrokRunTimeout(f"Grok ACP {method} timed out after {timeout:.3f}s")

    def close(self, *, force: bool = False) -> None:
        if self.process.poll() is None:
            if force:
                self.process.kill()
            else:
                self.process.terminate()
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)
        for thread in self._threads:
            thread.join(timeout=1.0)


@dataclass(frozen=True)
class GrokRun:
    session_id: str
    stop_reason: str
    response: dict[str, Any]


class GrokAdapter:
    """Supervise one isolated Grok ACP child and exact mail generations."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        agent_id: str,
        session_id: str,
        revision: str,
        isolation: GrokIsolation,
        executable: str | Path,
        api_key_resolver: Callable[[], str] | None = None,
        client_factory: Callable[..., ACPClient] = ACPClient,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        prompt_timeout: float = DEFAULT_PROMPT_TIMEOUT,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
        max_restarts: int = 1,
    ) -> None:
        self.project_root = _as_path(project_root).resolve()
        self.agent_id = agent_id
        self.session_id = session_id
        self.revision = str(revision)
        self.isolation = isolation
        self.executable = _as_path(executable).resolve()
        self.api_key_resolver = api_key_resolver or (lambda: resolve_grok_api_key())
        self.client_factory = client_factory
        self.sleep = sleep
        self.monotonic = monotonic
        self.prompt_timeout = prompt_timeout
        self.drain_timeout = drain_timeout
        self.max_restarts = max_restarts
        self.client: ACPClient | None = None
        self.grok_session_id: str | None = None
        self.wake_claimed = False
        self.environment: dict[str, str] = {}
        self._last_generation: tuple[str, ...] | None = None

    def _lease_valid(self) -> None:
        try:
            from _rtruntime import load_validated_lease

            load_validated_lease(
                self.project_root,
                self.agent_id,
                self.session_id,
                self.revision,
            )
        except ImportError as error:
            raise GrokError("Roundtable runtime library is unavailable") from error

    def _start_client(self) -> None:
        if self.client is not None and self.client.process.poll() is None:
            return
        if self.client is not None:
            self.client.close(force=True)
        api_key = self.api_key_resolver()
        registry = os.environ.get("RT_PROJECTS_FILE", "").strip()
        self.environment = self.isolation.environment(
            project_root=self.project_root,
            executable=self.executable,
            api_key=api_key,
            registry=registry,
        )
        self.environment.update(
            {
                "RT_FROM": self.agent_id,
                "RT_SESSION_ID": self.session_id,
                "RT_LEASE_REVISION": self.revision,
                "RT_GROK_BIN": str(self.executable),
            }
        )
        for name in ("RT_RUNTIME_DIR", "RT_CODEX_RUNTIME_DIR"):
            value = os.environ.get(name, "").strip()
            if value:
                self.environment[name] = value
        self.isolation.assert_isolated(self.environment)
        events_path = self.isolation.logs / "acp-events.jsonl"
        command = [str(self.executable), "--no-auto-update", "agent", "--no-leader", "stdio"]
        client: ACPClient | None = None
        try:
            client = self.client_factory(
                command,
                cwd=self.project_root,
                environment=self.environment,
                events_path=events_path,
            )
            self.client = client
            initialized, _meta = client.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "roundtable-grok", "version": ADAPTER_VERSION},
                    "capabilities": {},
                },
                timeout=10.0,
            )
            if initialized is None:
                raise GrokProtocolError("Grok ACP initialize returned no response")
            if initialized.get("error"):
                if _auth_error(initialized):
                    raise GrokAuthenticationError("Grok ACP initialize failed")
                raise GrokProtocolError("Grok ACP initialize failed")
            created, _meta = client.request(
                "session/new",
                {"cwd": str(self.project_root), "mcpServers": []},
                timeout=30.0,
            )
            if created is None or created.get("error"):
                if _auth_error(created):
                    raise GrokAuthenticationError("Grok authentication failed while creating an ACP session")
                raise GrokProtocolError("Grok ACP session/new failed")
            result = created.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("sessionId"), str):
                raise GrokProtocolError("Grok ACP session/new returned no session id")
            self.grok_session_id = result["sessionId"]
        except Exception:
            if client is not None:
                try:
                    client.close(force=True)
                except Exception:
                    pass
            self.client = None
            self.grok_session_id = None
            raise

    def stop(self, *, force: bool = False) -> None:
        client, self.client = self.client, None
        self.grok_session_id = None
        if client is not None:
            client.close(force=force)

    def _mail_generation(self) -> tuple[str, ...]:
        try:
            from _rtlib import locked_project_mailbox_checked
        except ImportError as error:
            raise GrokError("Roundtable mailbox library is unavailable") from error
        with locked_project_mailbox_checked(self.project_root) as mailbox:
            directory = mailbox.inbox_dir / self.agent_id / "new"
            if not directory.is_dir():
                return ()
            return tuple(
                path.name
                for path in sorted(directory.iterdir())
                if not path.name.startswith((".", "ack-"))
            )

    def _fence_update(self, *, names: tuple[str, ...] = (), attempts: int = 0) -> None:
        try:
            from _rtruntime import update_wake

            update_wake(
                self.project_root,
                self.agent_id,
                self.session_id,
                self.revision,
                watcher_pid=os.getpid(),
                last_wake_messages=list(names),
                wake_attempts=attempts,
                expected_watcher_pid=os.getpid() if self.wake_claimed else None,
            )
            self.wake_claimed = True
        except ImportError as error:
            raise GrokError("Roundtable runtime library is unavailable") from error

    def _clear_fence(self) -> None:
        try:
            from _rtruntime import clear_wake

            clear_wake(
                self.project_root,
                self.agent_id,
                self.session_id,
                self.revision,
                expected_watcher_pid=os.getpid(),
            )
        except ImportError as error:
            raise GrokError("Roundtable runtime library is unavailable") from error

    def run_generation(
        self,
        names: tuple[str, ...],
        *,
        prompt_timeout: float | None = None,
        drain_timeout: float | None = None,
    ) -> GrokRun:
        if not names:
            raise GrokError("cannot wake Grok without a mail generation")
        self._lease_valid()
        prompt_timeout = self.prompt_timeout if prompt_timeout is None else prompt_timeout
        drain_timeout = self.drain_timeout if drain_timeout is None else drain_timeout
        restarts = 0
        while True:
            if self.client is None or self.client.process.poll() is not None or not self.grok_session_id:
                self._start_client()
            assert self.client is not None and self.grok_session_id is not None
            prompt = wake_prompt(self.project_root, self.agent_id, names)
            try:
                response, _meta = self.client.request(
                    "session/prompt",
                    {
                        "sessionId": self.grok_session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                    timeout=prompt_timeout,
                )
            except GrokUnavailableError:
                remaining = self._mail_generation()
                if not set(names).intersection(remaining):
                    return GrokRun(self.grok_session_id, "drained_after_child_death", {})
                if restarts >= self.max_restarts:
                    raise
                restarts += 1
                self.stop(force=True)
                continue
            except GrokRunTimeout:
                self.stop(force=True)
                raise
            if response is None or response.get("error"):
                if response and _auth_error(response):
                    self.stop(force=True)
                    raise GrokAuthenticationError("Grok authentication failed during the wake turn")
                if response and response.get("error") and "permission" in json.dumps(response).lower():
                    raise GrokPermissionError("Grok ACP permission policy rejected the wake turn")
                raise GrokRunError("Grok ACP prompt failed")
            result = response.get("result")
            if not isinstance(result, dict):
                raise GrokProtocolError("Grok ACP prompt returned no result")
            stop_reason = str(result.get("stopReason") or result.get("stop_reason") or "").lower()
            if stop_reason not in TERMINAL_OK:
                raise GrokRunError(f"Grok ACP prompt ended with {stop_reason or 'unknown stop reason'}")
            run = GrokRun(self.grok_session_id, stop_reason, response)
            break

        deadline = self.monotonic() + drain_timeout
        while self.monotonic() < deadline:
            remaining = self._mail_generation()
            if not set(names).intersection(remaining):
                self._last_generation = names
                return run
            self._fence_update(names=names, attempts=1)
            self.sleep(min(DEFAULT_POLL_SECONDS, max(0.05, deadline - self.monotonic())))
        raise GrokRunTimeout(
            f"Grok prompt finalized but mail generation remained in new/: {names}"
        )

    def run(self, *, once: bool = False, ready_timeout: float = DEFAULT_READY_TIMEOUT) -> int:
        """Run the fenced Grok wake loop until interrupted or one turn ends."""

        self._lease_valid()
        try:
            self._start_client()
            deadline = self.monotonic() + ready_timeout
            if self.client is None or self.grok_session_id is None:
                raise GrokProtocolError("Grok ACP child did not become ready")
            if self.monotonic() > deadline:
                raise GrokRunTimeout("Grok ACP readiness timed out")
            self._fence_update()
            while True:
                names = self._mail_generation()
                if names:
                    self._fence_update(names=names, attempts=1)
                    self.run_generation(names)
                    if once:
                        return 0
                elif once:
                    return 0
                self.sleep(DEFAULT_POLL_SECONDS)
        finally:
            if self.wake_claimed:
                try:
                    self._clear_fence()
                except Exception:
                    pass
            self.stop()


def _load_adapter_from_environment(args: argparse.Namespace) -> GrokAdapter:
    project = os.environ.get("RT_PROJECT_ROOT", "").strip()
    agent = os.environ.get("RT_FROM", "").strip()
    session = os.environ.get("RT_SESSION_ID", "").strip()
    revision = os.environ.get("RT_LEASE_REVISION", "").strip()
    if not project or not agent or not session or not revision:
        raise GrokError("rt-grok-wake requires a complete fenced seat environment")
    executable = _as_path(args.grok_bin).resolve() if args.grok_bin else resolve_grok_bin()
    isolation = create_isolation(project, runtime_root=args.runtime_root)
    return GrokAdapter(
        project,
        agent_id=agent,
        session_id=session,
        revision=revision,
        isolation=isolation,
        executable=executable,
        prompt_timeout=args.prompt_timeout,
        drain_timeout=args.drain_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rt-grok-wake",
        description=(
            "INTERNAL LAB TOOL: run the isolated Grok ACP mail-drain "
            "supervisor. Normal Grok seats use the native TUI via rt-grok."
        ),
    )
    parser.add_argument("--grok-bin")
    parser.add_argument("--runtime-root")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT)
    parser.add_argument("--prompt-timeout", type=float, default=DEFAULT_PROMPT_TIMEOUT)
    parser.add_argument("--drain-timeout", type=float, default=DEFAULT_DRAIN_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        adapter = _load_adapter_from_environment(args)
        return adapter.run(once=args.once, ready_timeout=args.ready_timeout)
    except KeyboardInterrupt:
        return 130
    except (GrokError, OSError, TimeoutError) as error:
        print(f"rt-grok-wake: {error}", file=sys.stderr)
        return 2


__all__ = [
    "ACPClient",
    "GrokAdapter",
    "GrokAuthenticationError",
    "GrokError",
    "GrokIsolation",
    "GrokPermissionError",
    "GrokProtocolError",
    "GrokRun",
    "GrokRunError",
    "GrokRunTimeout",
    "GrokUnavailableError",
    "create_isolation",
    "main",
    "resolve_grok_api_key",
    "resolve_grok_bin",
    "session_key",
    "wake_permission_policy",
    "wake_prompt",
]
