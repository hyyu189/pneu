"""OpenClaw Gateway adapter for a fenced Roundtable seat.

OpenClaw is a Gateway process, not a terminal protocol.  This adapter owns
only the Gateway wake side of Delivery v2: the durable Roundtable maildir is
still the source of truth and the OpenClaw agent must read, act on, and ack
mail itself.  The adapter waits for a terminal ``agent.wait`` result and for
the exact triggered generation to leave ``new/`` before it reports success.

The client intentionally uses the small Gateway WebSocket protocol directly.
That keeps the installed Roundtable product stdlib-only and makes the
authentication, loopback, and finalization boundaries explicit.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any, Callable


PROTOCOL_VERSION = 3
ADAPTER_VERSION = "0.2.1"
DEFAULT_READY_TIMEOUT = 45.0
DEFAULT_RUN_TIMEOUT = 600.0
DEFAULT_DRAIN_TIMEOUT = 600.0
DEFAULT_POLL_SECONDS = 0.25
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
TERMINAL_OK = frozenset({"ok", "success", "completed"})
TERMINAL_ERROR = frozenset({"error", "failed", "failure", "cancelled", "canceled"})


class OpenClawError(RuntimeError):
    """A fail-closed adapter or Gateway error."""


class GatewayProtocolError(OpenClawError):
    """The Gateway did not speak the expected authenticated protocol."""


class GatewayRunError(OpenClawError):
    """An agent run ended without a successful terminal result."""


def _as_path(value: str | Path) -> Path:
    return Path(value).expanduser().absolute()


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_name(value: str) -> str:
    rendered = "".join(char if char.isalnum() or char in "-_" else "-" for char in value)
    return rendered.strip("-") or "openclaw"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _project_key(project_root: Path) -> str:
    return hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class OpenClawIsolation:
    """All process-owned OpenClaw paths for one project seat."""

    root: Path
    state_dir: Path
    config_path: Path
    workspace: Path
    home: Path
    tmp: Path
    logs: Path
    port: int
    token: str

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def environment(self, *, project_root: Path, executable: Path) -> dict[str, str]:
        """Return a deliberately bounded child environment.

        HOME/XDG/TMP and all OpenClaw-owned paths live below ``root``.  PATH
        contains the resolved executable, its Node interpreter directory, and
        system paths only; the user's shell PATH is never copied wholesale.
        """

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in (
            self.state_dir,
            self.workspace,
            self.home,
            self.tmp,
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        node = shutil.which("node", path=os.environ.get("PATH", os.defpath))
        path_parts = [str(executable.parent)]
        path_parts.append(str(Path(sys.executable).resolve().parent))
        if node:
            path_parts.append(str(Path(node).resolve().parent))
        path_parts.extend(part for part in os.defpath.split(os.pathsep) if part)
        env = {
            "PATH": os.pathsep.join(dict.fromkeys(path_parts)),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "XDG_DATA_HOME": str(self.home / ".local" / "share"),
            "XDG_CACHE_HOME": str(self.home / ".cache"),
            "TMPDIR": str(self.tmp),
            "OPENCLAW_STATE_DIR": str(self.state_dir),
            "OPENCLAW_CONFIG_PATH": str(self.config_path),
            "OPENCLAW_GATEWAY_TOKEN": self.token,
            "RT_OPENCLAW_BIN": str(executable.resolve()),
            "RT_PROJECT_ROOT": str(project_root.resolve()),
            "RT_OPENCLAW_ISOLATION_ROOT": str(self.root),
        }
        # The agent needs the managed Roundtable tools, but this path is
        # caller-controlled only through the resolved adapter installation.
        install_prefix = os.environ.get("ROUNDTABLE_INSTALL_PREFIX", "").strip()
        if install_prefix:
            candidate = Path(install_prefix).expanduser().resolve() / "bin"
            if candidate.is_dir():
                env["PATH"] = str(candidate) + os.pathsep + env["PATH"]
        else:
            source_bin = Path(__file__).resolve().parents[3] / "bin"
            if source_bin.is_dir():
                env["PATH"] = str(source_bin) + os.pathsep + env["PATH"]
        return env

    def assert_isolated(self, environment: dict[str, str]) -> None:
        """Reject a configuration that can resolve OpenClaw state globally."""

        root = self.root.resolve()
        for name in (
            "HOME",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "TMPDIR",
            "OPENCLAW_STATE_DIR",
            "OPENCLAW_CONFIG_PATH",
            "RT_OPENCLAW_ISOLATION_ROOT",
        ):
            value = environment.get(name)
            if not value or not _under(Path(value), root):
                raise OpenClawError(f"OpenClaw isolation variable {name} escapes {root}")
        system_paths = {Path(item).resolve() for item in os.defpath.split(os.pathsep) if item}
        executable_parent = Path(environment.get("RT_OPENCLAW_BIN", "/")).resolve().parent
        for entry in environment.get("PATH", "").split(os.pathsep):
            if entry and Path(entry).resolve() not in system_paths and Path(entry).resolve() != executable_parent and not _under(Path(entry), root):
                # Homebrew/managed executable parents are allowed only when
                # they contain the exact resolved executable and are not state
                # or skill roots.
                if not (
                    (Path(entry) / "node").is_file()
                    or (Path(entry) / "openclaw").is_file()
                    or (Path(entry) / "rt-inbox").is_file()
                    or (Path(entry) / "python3").is_file()
                    or (Path(entry) / "python").is_file()
                ):
                    raise OpenClawError(f"OpenClaw PATH entry is not isolated: {entry}")


def create_isolation(
    project_root: str | Path,
    *,
    runtime_root: str | Path | None = None,
    port: int | None = None,
    token: str | None = None,
) -> OpenClawIsolation:
    """Derive and create an isolated OpenClaw root for a project."""

    project = _as_path(project_root).resolve()
    base = runtime_root or os.environ.get("RT_OPENCLAW_RUNTIME_DIR") or os.environ.get("RT_RUNTIME_DIR")
    if base:
        base_path = _as_path(base)
    else:
        base_path = _as_path(tempfile.gettempdir()) / "roundtable-runtime"
    root = base_path / "openclaw" / _project_key(project)
    paths = OpenClawIsolation(
        root=root,
        state_dir=root / "state",
        config_path=root / "state" / "openclaw.json",
        workspace=project,
        home=root / "home",
        tmp=root / "tmp",
        logs=root / "logs",
        port=port or _free_loopback_port(),
        token=token or secrets.token_urlsafe(32),
    )
    paths.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.tmp.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    return paths


def write_default_config(isolation: OpenClawIsolation) -> None:
    """Write a minimal local Gateway config without touching user state."""

    isolation.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if isolation.config_path.exists():
        if not _under(isolation.config_path, isolation.root):
            raise OpenClawError("OpenClaw config is outside the isolation root")
        return
    payload = {
        "gateway": {
            "mode": "local",
            "bind": "loopback",
            "port": isolation.port,
            "auth": {"mode": "token", "token": isolation.token},
        },
        "logging": {"file": str(isolation.logs / "openclaw.log")},
        "agents": {"defaults": {"workspace": str(isolation.workspace)}},
        "hooks": {"enabled": False},
        "cron": {"enabled": False},
    }
    temporary = isolation.config_path.with_name(f".{isolation.config_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, isolation.config_path)


def resolve_openclaw_bin(environment: dict[str, str] | None = None) -> Path:
    """Resolve one real OpenClaw executable; never attach to a service."""

    environment = environment or os.environ
    override = environment.get("RT_OPENCLAW_BIN", "").strip()
    candidates: list[Path] = []
    if override:
        candidates.append(_as_path(override))
    home = Path(environment.get("HOME") or Path.home()).expanduser()
    candidates.extend(
        (
            home / ".local" / "bin" / "openclaw",
            home / ".npm-global" / "bin" / "openclaw",
        )
    )
    candidates.extend(Path(item) / "openclaw" for item in environment.get("PATH", "").split(os.pathsep) if item)
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.absolute()
        if str(candidate) in seen:
            continue
        seen.add(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            if "cmux-cli-shims" in {part.lower() for part in candidate.parts}:
                continue
            return candidate
    hint = "RT_OPENCLAW_BIN" if override else "PATH or RT_OPENCLAW_BIN"
    raise OpenClawError(f"could not resolve an executable OpenClaw binary; set {hint}")


def session_key(project_root: str | Path, agent_id: str = "main") -> str:
    """Return a bounded, deterministic Gateway session key."""

    if not agent_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in agent_id):
        raise OpenClawError(f"invalid OpenClaw agent id: {agent_id!r}")
    return f"agent:{agent_id}:roundtable-{_project_key(_as_path(project_root))[:20]}"


def wake_prompt(project_root: str | Path, agent_id: str, names: tuple[str, ...]) -> str:
    rendered = ", ".join(names)
    return (
        "[Roundtable OpenClaw seat] A fenced durable mail generation is waiting "
        f"for agent {agent_id!r} in project {Path(project_root).resolve()}. "
        f"The exact files are: {rendered}. Work from the project root. First run "
        "`rt-inbox --fenced --archive-quiet-acks -f json`, then act on every "
        "non-ack message. Do not acknowledge work that is incomplete. When each "
        "message is handled, run `rt-ack --fenced <msg_id> \"brief outcome\"` "
        "once; the Roundtable mailbox owns archival. Do not use hooks or a "
        "second Gateway, and do not report success until the exact generation "
        "has left maildir new/."
    )


class _WebSocket:
    """Small authenticated client for the OpenClaw Gateway protocol."""

    def __init__(self, url: str, token: str, *, timeout: float = 10.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in LOOPBACK_HOSTS:
            raise GatewayProtocolError("OpenClaw Gateway URL must be loopback ws:// or wss://")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise GatewayProtocolError("OpenClaw Gateway URL must not contain a path or query")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.token = token
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self._buffer = bytearray()
        self._fragments: list[bytes] = []

    def close(self) -> None:
        sock, self.sock = self.sock, None
        if sock is None:
            return
        try:
            sock.close()
        except OSError:
            pass

    def connect(self) -> None:
        if self.sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        if self.scheme == "wss":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = self._read_http_headers(sock)
        if not response.startswith(b"HTTP/1.1 101") and not response.startswith(b"HTTP/1.0 101"):
            sock.close()
            raise GatewayProtocolError(f"Gateway WebSocket upgrade failed: {response[:120]!r}")
        self.sock = sock
        self._handshake()

    def _read_http_headers(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise GatewayProtocolError("Gateway closed during WebSocket upgrade")
            data.extend(chunk)
            if len(data) > 65536:
                raise GatewayProtocolError("Gateway WebSocket headers are too large")
        marker = data.index(b"\r\n\r\n") + 4
        self._buffer.extend(data[marker:])
        return bytes(data[:marker])

    def _handshake(self) -> None:
        challenge = self._receive_json(deadline=time.monotonic() + self.timeout)
        if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
            raise GatewayProtocolError("Gateway did not send connect.challenge")
        params = {
            "minProtocol": PROTOCOL_VERSION,
            "maxProtocol": PROTOCOL_VERSION,
            "client": {
                "id": "gateway-client",
                "version": ADAPTER_VERSION,
                "platform": sys.platform,
                "mode": "backend",
            },
            "role": "operator",
            "scopes": ["operator.read", "operator.write"],
            "caps": [],
            "commands": [],
            "permissions": {},
            "auth": {"token": self.token},
            "locale": "en-US",
            "userAgent": f"roundtable-openclaw/{ADAPTER_VERSION}",
        }
        response = self._request_raw("connect", params, deadline=time.monotonic() + self.timeout)
        if not response.get("ok"):
            raise GatewayProtocolError(f"Gateway authentication failed: {response.get('error')}")
        payload = response.get("payload") or {}
        if payload.get("type") != "hello-ok" or payload.get("protocol") != PROTOCOL_VERSION:
            raise GatewayProtocolError("Gateway hello-ok did not negotiate protocol 3")

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        if self.sock is None:
            self.connect()
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        response = self._request_raw(method, params or {}, deadline=deadline)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise GatewayProtocolError(f"Gateway {method} failed: {error}")
        return response.get("payload")

    def _request_raw(self, method: str, params: dict[str, Any], *, deadline: float) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        self._send_json({"type": "req", "id": request_id, "method": method, "params": params})
        while True:
            frame = self._receive_json(deadline=deadline)
            if frame.get("type") == "event":
                continue
            if frame.get("type") != "res" or frame.get("id") != request_id:
                continue
            return frame

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"))

    def _send_frame(self, payload: bytes) -> None:
        if self.sock is None:
            raise GatewayProtocolError("Gateway WebSocket is not connected")
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def _receive_json(self, *, deadline: float) -> dict[str, Any]:
        payload = self._receive_message(deadline=deadline)
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GatewayProtocolError(f"Gateway sent invalid JSON: {error}") from error
        if not isinstance(value, dict):
            raise GatewayProtocolError("Gateway frame is not an object")
        return value

    def _receive_message(self, *, deadline: float) -> bytes:
        while True:
            first, second = self._read_exact(2, deadline)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
            if length > 26 * 1024 * 1024:
                raise GatewayProtocolError("Gateway WebSocket frame is too large")
            mask = self._read_exact(4, deadline) if masked else b""
            data = self._read_exact(length, deadline)
            if masked:
                data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
            if opcode == 0x9:
                self._send_control(0xA, data)
                continue
            if opcode == 0x8:
                raise GatewayProtocolError("Gateway closed the WebSocket")
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                self._fragments = [data]
            elif opcode == 0x0:
                if not self._fragments:
                    raise GatewayProtocolError("Gateway sent an unexpected continuation frame")
                self._fragments.append(data)
            else:
                continue
            if fin:
                result = b"".join(self._fragments)
                self._fragments = []
                return result

    def _send_control(self, opcode: int, payload: bytes) -> None:
        if self.sock is None:
            return
        if len(payload) > 125:
            payload = payload[:125]
        mask = os.urandom(4)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.sock.sendall(bytes((0x80 | opcode, 0x80 | len(payload))) + mask + masked)

    def _read_exact(self, count: int, deadline: float) -> bytes:
        while len(self._buffer) < count:
            if self.sock is None:
                raise GatewayProtocolError("Gateway WebSocket is not connected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for Gateway response")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(max(4096, count - len(self._buffer)))
            if not chunk:
                raise GatewayProtocolError("Gateway closed the WebSocket")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:count])
        del self._buffer[:count]
        return result


class OpenClawGateway:
    """Gateway client wrapper with an injectable transport for unit tests."""

    def __init__(self, url: str, token: str, *, transport_factory: Callable[..., Any] | None = None) -> None:
        self.url = url
        self.token = token
        self.transport_factory = transport_factory or _WebSocket
        self.transport: Any = None

    def connect(self) -> "OpenClawGateway":
        self.transport = self.transport_factory(self.url, self.token)
        self.transport.connect()
        return self

    def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        if self.transport is None:
            self.connect()
        return self.transport.request(method, params or {}, timeout=timeout)

    def __enter__(self) -> "OpenClawGateway":
        return self.connect()

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()


@dataclass(frozen=True)
class GatewayRun:
    run_id: str
    status: str
    payload: dict[str, Any]


class OpenClawAdapter:
    """Supervise one isolated Gateway and deliver exact mail generations."""

    def __init__(
        self,
        project_root: str | Path,
        *,
        agent_id: str,
        session_id: str,
        revision: str,
        isolation: OpenClawIsolation,
        executable: str | Path,
        gateway_factory: Callable[[str, str], OpenClawGateway] | None = None,
        popen_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        run_timeout: float = DEFAULT_RUN_TIMEOUT,
        drain_timeout: float = DEFAULT_DRAIN_TIMEOUT,
    ) -> None:
        self.project_root = _as_path(project_root).resolve()
        self.agent_id = agent_id
        self.session_id = session_id
        self.revision = str(revision)
        self.isolation = isolation
        self.executable = _as_path(executable).resolve()
        # Roundtable's sender id and OpenClaw's configured Gateway agent id
        # are separate namespaces. Most isolated Gateways expose `main` even
        # when the Roundtable seat is named `openclaw`.
        self.gateway_agent_id = os.environ.get("RT_OPENCLAW_AGENT_ID", "main").strip() or "main"
        self.gateway_factory = gateway_factory or (lambda url, token: OpenClawGateway(url, token))
        self.popen_factory = popen_factory
        self.sleep = sleep
        self.monotonic = monotonic
        self.run_timeout = run_timeout
        self.drain_timeout = drain_timeout
        self.process: subprocess.Popen | None = None
        self.log_handle: Any = None
        self.wake_claimed = False
        self.environment = self.isolation.environment(project_root=self.project_root, executable=self.executable)
        self.environment.update(
            {
                "RT_FROM": self.agent_id,
                "RT_SESSION_ID": self.session_id,
                "RT_LEASE_REVISION": self.revision,
            }
        )
        for name in ("RT_RUNTIME_DIR", "RT_CODEX_RUNTIME_DIR"):
            value = os.environ.get(name, "").strip()
            if value:
                self.environment[name] = value
        registry = os.environ.get("RT_PROJECTS_FILE", "").strip()
        if registry:
            self.environment["RT_PROJECTS_FILE"] = registry
        self.isolation.assert_isolated(self.environment)

    def _managed_tool_dirs(self) -> tuple[Path, ...]:
        """Return Roundtable and runtime Python directories for Gateway exec."""

        candidates: list[Path] = []
        install_prefix = os.environ.get("ROUNDTABLE_INSTALL_PREFIX", "").strip()
        if install_prefix:
            candidates.append(Path(install_prefix).expanduser().resolve() / "bin")
        candidates.extend(
            (
                Path(__file__).resolve().parents[3] / "bin",
                Path(__file__).resolve().parents[4] / "bin",
                Path(sys.executable).resolve().parent,
            )
        )
        return tuple(dict.fromkeys(candidate for candidate in candidates if candidate.is_dir()))

    def _prepare_isolated_config(self) -> None:
        """Constrain logging and Roundtable tool resolution in Gateway config."""

        write_default_config(self.isolation)
        config_path = self.isolation.config_path.resolve()
        if not _under(config_path, self.isolation.root):
            raise OpenClawError("OpenClaw config is outside the isolation root")
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OpenClawError("OpenClaw isolated config must be valid JSON") from error
        if not isinstance(payload, dict):
            raise OpenClawError("OpenClaw isolated config must be a JSON object")

        logging = payload.setdefault("logging", {})
        if not isinstance(logging, dict):
            raise OpenClawError("OpenClaw logging config must be an object")
        log_file = logging.get("file")
        if log_file is None:
            logging["file"] = str(self.isolation.logs / "openclaw.log")
        elif not isinstance(log_file, str) or not log_file.strip():
            raise OpenClawError("OpenClaw logging.file must be a non-empty path")
        else:
            rendered = Path(log_file).expanduser()
            if not rendered.is_absolute():
                rendered = config_path.parent / rendered
            rendered = rendered.resolve()
            if not _under(rendered, self.isolation.root):
                raise OpenClawError(f"OpenClaw logging.file escapes {self.isolation.root}")
            logging["file"] = str(rendered)

        tools = payload.setdefault("tools", {})
        if not isinstance(tools, dict):
            raise OpenClawError("OpenClaw tools config must be an object")
        exec_config = tools.setdefault("exec", {})
        if not isinstance(exec_config, dict):
            raise OpenClawError("OpenClaw tools.exec config must be an object")
        path_prepend = exec_config.setdefault("pathPrepend", [])
        if not isinstance(path_prepend, list) or any(not isinstance(item, str) for item in path_prepend):
            raise OpenClawError("OpenClaw tools.exec.pathPrepend must be a string list")
        for directory in reversed(self._managed_tool_dirs()):
            rendered = str(directory)
            if rendered not in path_prepend:
                path_prepend.insert(0, rendered)

        temporary = config_path.with_name(f".{config_path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, config_path)

    def start_gateway(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self._prepare_isolated_config()
        log_path = self.isolation.logs / "gateway.log"
        self.log_handle = log_path.open("ab")
        command = [
            str(self.executable),
            "gateway",
            "run",
            "--port",
            str(self.isolation.port),
            "--bind",
            "loopback",
            "--auth",
            "token",
            "--token",
            self.isolation.token,
            "--verbose",
        ]
        try:
            self.process = self.popen_factory(
                command,
                cwd=str(self.project_root),
                env=self.environment,
                stdin=subprocess.DEVNULL,
                stdout=self.log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except Exception:
            self.log_handle.close()
            self.log_handle = None
            raise

    def wait_ready(self, timeout: float = DEFAULT_READY_TIMEOUT) -> None:
        deadline = self.monotonic() + timeout
        last_error: Exception | None = None
        while self.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise OpenClawError(f"OpenClaw Gateway exited during startup with {self.process.returncode}")
            client = self.gateway_factory(self.isolation.url, self.isolation.token)
            try:
                client.connect()
                client.request("health", {}, timeout=min(5.0, max(0.1, deadline - self.monotonic())))
                client.close()
                return
            except (Exception) as error:  # readiness must tolerate sidecar warmup
                last_error = error
                try:
                    client.close()
                except Exception:
                    pass
                self.sleep(min(0.5, max(0.05, deadline - self.monotonic())))
        raise OpenClawError(f"OpenClaw Gateway did not become ready: {last_error}")

    def _mail_generation(self) -> tuple[str, ...]:
        """Read only the authoritative fenced mailbox generation boundary."""

        try:
            from _rtlib import locked_project_mailbox_checked
        except ImportError as error:
            raise OpenClawError("Roundtable mailbox library is unavailable") from error
        with locked_project_mailbox_checked(self.project_root) as mailbox:
            directory = mailbox.inbox_dir / self.agent_id / "new"
            if not directory.is_dir():
                return ()
            names = []
            for path in sorted(directory.iterdir()):
                if path.name.startswith((".", "ack-")):
                    continue
                # Mirror rt-wait-inbox's wake predicate, including malformed
                # entries, so the model can surface and repair the exact
                # durable generation instead of the adapter hiding it.
                names.append(path.name)
            return tuple(names)

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
            raise OpenClawError("Roundtable runtime library is unavailable") from error

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
            raise OpenClawError("Roundtable runtime library is unavailable") from error

    def run_generation(self, names: tuple[str, ...], *, timeout: float = DEFAULT_RUN_TIMEOUT, drain_timeout: float = DEFAULT_DRAIN_TIMEOUT) -> GatewayRun:
        if not names:
            raise OpenClawError("cannot wake OpenClaw without a mail generation")
        message_id = names[0].removesuffix(".md")
        prompt = wake_prompt(self.project_root, self.agent_id, names)
        with self.gateway_factory(self.isolation.url, self.isolation.token) as gateway:
            accepted = gateway.request(
                "agent",
                {
                    "message": prompt,
                    "agentId": self.gateway_agent_id,
                    "sessionKey": session_key(self.project_root, self.gateway_agent_id),
                    "idempotencyKey": f"roundtable:{message_id}",
                    "deliver": False,
                    "timeout": max(1, int(timeout)),
                },
                timeout=min(15.0, timeout),
            )
            if not isinstance(accepted, dict) or not isinstance(accepted.get("runId"), str) or not accepted["runId"]:
                raise GatewayProtocolError(f"OpenClaw agent RPC did not return a runId: {accepted!r}")
            run_id = accepted["runId"]
            deadline = self.monotonic() + timeout
            terminal: dict[str, Any] | None = None
            while self.monotonic() < deadline:
                remaining = max(0.1, deadline - self.monotonic())
                snapshot = gateway.request(
                    "agent.wait",
                    {"runId": run_id, "timeoutMs": int(min(5000.0, remaining * 1000))},
                    timeout=min(10.0, remaining + 1.0),
                )
                if not isinstance(snapshot, dict):
                    raise GatewayProtocolError(f"OpenClaw agent.wait returned invalid payload: {snapshot!r}")
                status = str(snapshot.get("status") or "").lower()
                if status == "timeout":
                    continue
                if status in TERMINAL_OK | TERMINAL_ERROR or snapshot.get("endedAt"):
                    terminal = snapshot
                    break
            if terminal is None:
                raise GatewayRunError(f"OpenClaw run {run_id} did not reach a terminal result")
            status = str(terminal.get("status") or "").lower()
            if status not in TERMINAL_OK:
                raise GatewayRunError(f"OpenClaw run {run_id} ended with {status}: {terminal.get('error')}")
            result = GatewayRun(run_id=run_id, status=status, payload=terminal)

        deadline = self.monotonic() + drain_timeout
        while self.monotonic() < deadline:
            remaining = self._mail_generation()
            if not set(names).intersection(remaining):
                return result
            self._fence_update(names=names, attempts=1)
            self.sleep(min(DEFAULT_POLL_SECONDS, max(0.05, deadline - self.monotonic())))
        raise GatewayRunError(f"OpenClaw run {result.run_id} finalized but mail generation remained in new/: {names}")

    def run(self, *, once: bool = False, ready_timeout: float = DEFAULT_READY_TIMEOUT) -> int:
        """Run the supervised watcher until interrupted or one lab turn ends."""

        try:
            try:
                from _rtruntime import load_validated_lease

                load_validated_lease(
                    self.project_root,
                    self.agent_id,
                    self.session_id,
                    self.revision,
                )
            except ImportError as error:
                raise OpenClawError("Roundtable runtime library is unavailable") from error
            self.start_gateway()
            self.wait_ready(ready_timeout)
            self._fence_update()
            while True:
                names = self._mail_generation()
                if names:
                    self._fence_update(names=names, attempts=1)
                    self.run_generation(
                        names,
                        timeout=self.run_timeout,
                        drain_timeout=self.drain_timeout,
                    )
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
                    # Preserve the original Gateway/lease error; doctor can
                    # report a stale wake record after a fenced process dies.
                    pass
            self.stop_gateway()

    def stop_gateway(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        if self.log_handle is not None:
            self.log_handle.close()
            self.log_handle = None


def _load_adapter_from_environment(args: argparse.Namespace) -> OpenClawAdapter:
    project = os.environ.get("RT_PROJECT_ROOT", "").strip()
    agent = os.environ.get("RT_FROM", "").strip()
    session = os.environ.get("RT_SESSION_ID", "").strip()
    revision = os.environ.get("RT_LEASE_REVISION", "").strip()
    if not project or not agent or not session or not revision:
        raise OpenClawError("rt-openclaw-wake requires a complete fenced seat environment")
    if agent == "":
        raise OpenClawError("rt-openclaw-wake requires RT_FROM")
    isolation = create_isolation(project, runtime_root=args.runtime_root, port=args.port)
    executable = _as_path(args.gateway_bin).resolve() if args.gateway_bin else resolve_openclaw_bin()
    return OpenClawAdapter(
        project,
        agent_id=agent,
        session_id=session,
        revision=revision,
        isolation=isolation,
        executable=executable,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rt-openclaw-wake")
    parser.add_argument("--gateway-bin")
    parser.add_argument("--runtime-root")
    parser.add_argument("--port", type=int)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT)
    args = parser.parse_args(argv)
    try:
        adapter = _load_adapter_from_environment(args)
        return adapter.run(once=args.once, ready_timeout=args.ready_timeout)
    except KeyboardInterrupt:
        return 130
    except (OpenClawError, OSError, TimeoutError) as error:
        print(f"rt-openclaw-wake: {error}", file=sys.stderr)
        return 2


__all__ = [
    "GatewayRun",
    "GatewayProtocolError",
    "GatewayRunError",
    "OpenClawAdapter",
    "OpenClawError",
    "OpenClawGateway",
    "OpenClawIsolation",
    "create_isolation",
    "main",
    "resolve_openclaw_bin",
    "session_key",
    "wake_prompt",
    "write_default_config",
]
