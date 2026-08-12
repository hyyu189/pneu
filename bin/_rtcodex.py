"""Codex app-server plumbing shared by rt-codex-wake and rt-doctor.

The local app-server control socket speaks WebSocket-over-UDS (not JSONL).
This module intentionally uses only the Python standard library so launchd
does not depend on a particular virtual environment.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import select
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from _rtruntime import (
    RuntimeStateError,
    inspect_host_harness_seats,
    runtime_root,
)


class CodexRuntimeError(RuntimeError):
    pass


class RpcError(CodexRuntimeError):
    pass


class UnsupportedVersion(CodexRuntimeError):
    pass


def require_thread_project_cwd(
    project: Path,
    thread: dict,
    *,
    expected_thread_id: str | None = None,
) -> Path:
    """Prove that a persisted Codex thread belongs to one seat project.

    Codex persists its thread cwd independently from Roundtable's seat lease.
    Both paths must therefore be resolved through the filesystem before they
    are compared: this accepts honest symlink aliases while refusing stale
    paths left behind by moved or renamed worktrees.
    """

    try:
        seat_project = project.expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise CodexRuntimeError(
            f"Codex seat project cannot be canonicalized: {project} ({error})"
        ) from error
    if not seat_project.is_dir():
        raise CodexRuntimeError(
            f"Codex seat project is not a directory: {seat_project}"
        )

    thread_id = expected_thread_id or thread.get("id")
    rendered_thread_id = (
        thread_id if isinstance(thread_id, str) and thread_id else "<thread-id>"
    )
    raw_cwd = thread.get("cwd")
    rendered_cwd = raw_cwd if isinstance(raw_cwd, str) and raw_cwd else "<missing>"
    reanchor = (
        "rt-codex-wake reanchor "
        f"{shlex.quote(str(seat_project))} --thread-id "
        f"{shlex.quote(rendered_thread_id)}"
    )
    remedies = (
        "Resume this thread in its own project (restore that path first if it "
        f"moved), or explicitly re-anchor it with `{reanchor}`."
    )

    if not isinstance(raw_cwd, str) or not raw_cwd:
        raise CodexRuntimeError(
            f"Codex thread {rendered_thread_id} recorded cwd {rendered_cwd}; "
            f"the selected seat project is {seat_project}. {remedies}"
        )
    try:
        recorded = Path(raw_cwd).expanduser()
        if not recorded.is_absolute():
            raise ValueError("recorded cwd is not absolute")
        thread_cwd = recorded.resolve(strict=True)
    except FileNotFoundError as error:
        raise CodexRuntimeError(
            f"Codex thread {rendered_thread_id} recorded cwd {rendered_cwd}, "
            f"but that path no longer exists; the selected seat project is "
            f"{seat_project}. {remedies}"
        ) from error
    except (OSError, RuntimeError, ValueError) as error:
        raise CodexRuntimeError(
            f"Codex thread {rendered_thread_id} recorded cwd {rendered_cwd}, "
            f"which cannot be canonicalized ({error}); the selected seat "
            f"project is {seat_project}. {remedies}"
        ) from error
    if not thread_cwd.is_dir():
        raise CodexRuntimeError(
            f"Codex thread {rendered_thread_id} recorded cwd {rendered_cwd}, "
            f"which canonicalizes to non-directory {thread_cwd}; the selected "
            f"seat project is {seat_project}. {remedies}"
        )
    if thread_cwd != seat_project:
        raise CodexRuntimeError(
            f"Codex thread {rendered_thread_id} recorded cwd {rendered_cwd} "
            f"(canonical {thread_cwd}), but the selected seat project is "
            f"{seat_project}. {remedies}"
        )
    return thread_cwd


class CodexDaemonReloadRequired(CodexRuntimeError):
    """The live Roundtable job is proven, but its loaded definition is stale."""

    pass


def configured_runtime_dir() -> Path:
    try:
        return runtime_root()
    except RuntimeStateError as error:
        raise CodexRuntimeError(f"invalid Roundtable runtime root: {error}") from error


SOURCE_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PREFIX = os.environ.get("ROUNDTABLE_INSTALL_PREFIX")
ROUND_ROOT = (
    Path(INSTALL_PREFIX).expanduser().absolute() / "current"
    if INSTALL_PREFIX
    else SOURCE_ROOT
)
RUNTIME_DIR = configured_runtime_dir()
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
DEFAULT_SOCKET = (CODEX_HOME / "app-server-control" / "app-server-control.sock").expanduser()
APP_SERVER_LABEL = "com.roundtable.codex-app-server"
WAKE_LABEL = "com.roundtable.codex-wake"
# Version numbers alone neither claim nor deny support.  The floor is the
# oldest protocol surface a live gate ever exercised, not a per-version
# allowlist: Roundtable ships far more slowly than the harness, so acceptance
# is proven by a live read-only protocol probe of the running daemon at each
# readiness check rather than by enumerating every release.  Any release at or
# above the floor is probed; anything below it fails closed because no gate
# ever exercised it.
MINIMUM_CODEX_RELEASE = (0, 144, 6)
WAKE_BRIDGE_BUILD_COMPONENTS = (
    "bin/rt-codex-wake",
    "bin/_rtcodex.py",
    "bin/_rtruntime.py",
    "bin/_rtlib.py",
)
CODEX_RELOAD_MARKER_SCHEMA = "roundtable.codex-app-server-reload-required.v1"
CODEX_RELOAD_MARKER_NAME = "codex-app-server-reload-required.json"
CODEX_RELOAD_MARKER_MAX_BYTES = 16 * 1024
LAUNCHD_APP_SERVER_ENV_ALLOWLIST = frozenset({"OSLogRateLimit", "XPC_SERVICE_NAME"})

SERVICE_READY = "ready"
SERVICE_COLD = "cold"
SERVICE_BRIDGE_DOWN = "bridge_down"
SERVICE_RELOAD_REQUIRED_IDLE = "reload_required_idle"
SERVICE_RELOAD_DEFERRED_BUSY = "reload_deferred_busy"
SERVICE_UNSUPPORTED = "unsupported"
SERVICE_UNSAFE = "unsafe"
SERVICE_SETUP_REQUIRED = "setup_required"
WAKE_BRIDGE_TAKEOVER_TIMEOUT_SECONDS = 15.0
WAKE_BRIDGE_TAKEOVER_POLL_SECONDS = 0.2


@dataclass(frozen=True)
class CodexServiceStatus:
    """One conservative snapshot of the host-wide Codex service pair."""

    state: str
    detail: str
    cli_version: tuple[int, int, int] | None = None
    daemon: dict | None = None
    app_plist: str = "unknown"
    wake_plist: str = "unknown"
    bridge_detail: str = "not checked"


@dataclass(frozen=True)
class CodexDaemonIdentity:
    """Evidence tying one responsive app-server to the Roundtable LaunchAgent."""

    selected_codex: Path
    distribution: str
    managed_codex_hint: Path
    managed_codex_version: str | None
    launchd_pid: int
    peer_pid: int


@dataclass(frozen=True)
class LaunchdJobSnapshot:
    """Security-relevant fields from one ``launchctl print`` snapshot."""

    path: Path
    state: str
    program: Path
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: Path
    pid: int


def ensure_private_runtime_dir(path: Path | None = None) -> Path:
    """Create or tighten the local runtime root without following a leaf symlink."""
    root = Path(path if path is not None else RUNTIME_DIR).expanduser()
    if not root.is_absolute():
        raise CodexRuntimeError(f"runtime directory must be absolute: {root}")
    try:
        existing = root.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise CodexRuntimeError(f"cannot inspect runtime directory {root}: {error}") from error
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise CodexRuntimeError(f"runtime directory is a symlink: {root}")
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDONLY
        flags |= getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(root, flags)
    except OSError as error:
        raise CodexRuntimeError(f"cannot create private runtime directory {root}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise CodexRuntimeError(f"runtime path is not a directory: {root}")
        if info.st_uid != os.getuid():
            raise CodexRuntimeError(
                f"runtime directory owner uid {info.st_uid} != {os.getuid()}: {root}"
            )
        os.fchmod(descriptor, 0o700)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise CodexRuntimeError(f"cannot protect runtime directory {root}")
    except OSError as error:
        raise CodexRuntimeError(f"cannot protect runtime directory {root}: {error}") from error
    finally:
        os.close(descriptor)
    return root


def _env_path() -> str:
    entries = [
        str(Path.home() / ".npm-global" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    return ":".join(entries)


def codex_bin() -> Path:
    override = os.environ.get("RT_CODEX_BIN")
    if override:
        path = Path(override).expanduser().absolute()
        if path.is_file() and os.access(path, os.X_OK):
            return path
        raise CodexRuntimeError(f"RT_CODEX_BIN is not executable: {path}")

    candidates = [
        CODEX_HOME / "packages" / "standalone" / "current" / "codex",
        Path.home() / ".npm-global" / "bin" / "codex",
        Path.home() / ".local" / "bin" / "codex",
    ]
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.absolute()

    found = shutil.which("codex", path=_env_path())
    if found:
        return Path(found).absolute()
    raise CodexRuntimeError("could not find an executable Codex CLI")


def launchctl_bin() -> str:
    return os.environ.get("RT_LAUNCHCTL", "/bin/launchctl")


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def launch_agent_path(label: str) -> Path:
    root = Path(os.environ.get("RT_LAUNCH_AGENTS_DIR", Path.home() / "Library" / "LaunchAgents"))
    return root.expanduser() / f"{label}.plist"


def _plist_runtime_dir(*, ensure_runtime: bool) -> Path:
    if ensure_runtime:
        return ensure_private_runtime_dir()
    runtime_dir = Path(RUNTIME_DIR).expanduser()
    if not runtime_dir.is_absolute():
        raise CodexRuntimeError(
            f"runtime directory must be absolute: {runtime_dir}"
        )
    return runtime_dir


def app_server_plist(
    socket_path: Path = DEFAULT_SOCKET,
    *,
    ensure_runtime: bool = True,
) -> dict:
    runtime_dir = _plist_runtime_dir(ensure_runtime=ensure_runtime)
    selected_codex = codex_bin()
    environment = {
        "HOME": str(Path.home()),
        "PATH": _env_path(),
        "CODEX_HOME": str(CODEX_HOME),
        "RT_RUNTIME_DIR": str(runtime_dir),
        "RT_CODEX_RUNTIME_DIR": str(runtime_dir),
        "RT_CODEX_BIN": str(selected_codex),
    }
    if INSTALL_PREFIX:
        environment["ROUNDTABLE_INSTALL_PREFIX"] = str(
            Path(INSTALL_PREFIX).expanduser().absolute()
        )
    return {
        "Label": APP_SERVER_LABEL,
        "ProgramArguments": [
            str(selected_codex),
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": environment,
        "StandardOutPath": str(runtime_dir / "codex-app-server.stdout.log"),
        "StandardErrorPath": str(runtime_dir / "codex-app-server.stderr.log"),
    }


def wake_plist(
    socket_path: Path = DEFAULT_SOCKET,
    *,
    auto_discover: bool = False,
    ensure_runtime: bool = True,
) -> dict:
    runtime_dir = _plist_runtime_dir(ensure_runtime=ensure_runtime)
    selected_codex = codex_bin()
    arguments = [
        str(ROUND_ROOT / "bin" / "rt-codex-wake"),
        "--socket",
        str(socket_path),
        "run",
    ]
    if auto_discover:
        arguments.append("--auto-discover")
    environment = {
        "HOME": str(Path.home()),
        "PATH": _env_path(),
        "CODEX_HOME": str(CODEX_HOME),
        "RT_RUNTIME_DIR": str(runtime_dir),
        "RT_CODEX_RUNTIME_DIR": str(runtime_dir),
        "RT_CODEX_BIN": str(selected_codex),
    }
    if INSTALL_PREFIX:
        environment["ROUNDTABLE_INSTALL_PREFIX"] = str(
            Path(INSTALL_PREFIX).expanduser().absolute()
        )
    if os.environ.get("RT_PROJECTS_FILE"):
        environment["RT_PROJECTS_FILE"] = str(
            Path(os.environ["RT_PROJECTS_FILE"]).expanduser().resolve()
        )
    return {
        "Label": WAKE_LABEL,
        "ProgramArguments": arguments,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": environment,
        "StandardOutPath": str(runtime_dir / "rt-codex-wake.stdout.log"),
        "StandardErrorPath": str(runtime_dir / "rt-codex-wake.stderr.log"),
    }


def _write_plist(path: Path, payload: dict) -> bool:
    content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    if path.exists() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return True


def _launchd_print(label: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [launchctl_bin(), "print", f"{launch_domain()}/{label}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode not in {0, 113}:
        detail = (proc.stderr or proc.stdout).strip() or "no diagnostic output"
        raise CodexRuntimeError(
            f"launchctl print failed for {label} "
            f"(exit {proc.returncode}): {detail}"
        )
    return proc


def _launchd_scalar(output: str, key: str) -> str:
    """Read one top-level scalar from Darwin's ``launchctl print`` format."""

    prefix = f"\t{key} = "
    values = [line[len(prefix) :] for line in output.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise CodexRuntimeError(
            f"launchctl output has {len(values)} top-level {key!r} values"
        )
    return values[0]


def _launchd_block(output: str, key: str) -> tuple[str, ...]:
    """Read one flat top-level block from Darwin's ``launchctl print`` format."""

    lines = output.splitlines()
    header = f"\t{key} = {{"
    starts = [index for index, line in enumerate(lines) if line == header]
    if len(starts) != 1:
        raise CodexRuntimeError(
            f"launchctl output has {len(starts)} top-level {key!r} blocks"
        )
    values: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line == "\t}":
            return tuple(values)
        if not line.startswith("\t\t") or line == "\t\t":
            raise CodexRuntimeError(f"launchctl {key} block is malformed")
        values.append(line[2:])
    raise CodexRuntimeError(f"launchctl {key} block is unterminated")


def launchd_job_snapshot(label: str) -> LaunchdJobSnapshot:
    """Return the live, top-level identity of a running user LaunchAgent."""

    proc = _launchd_print(label)
    if proc.returncode != 0:
        raise CodexRuntimeError(f"LaunchAgent is not loaded: {label}")
    state = _launchd_scalar(proc.stdout, "state")
    if state != "running":
        raise CodexRuntimeError(f"LaunchAgent is not running: {label} state={state}")
    pid_value = _launchd_scalar(proc.stdout, "pid")
    try:
        pid = int(pid_value)
    except ValueError as error:
        raise CodexRuntimeError(
            f"LaunchAgent has an invalid pid: {label} pid={pid_value!r}"
        ) from error
    if pid <= 0:
        raise CodexRuntimeError(f"LaunchAgent has a non-positive pid: {label} pid={pid}")

    path = Path(_launchd_scalar(proc.stdout, "path")).expanduser()
    program = Path(_launchd_scalar(proc.stdout, "program")).expanduser()
    working_directory = Path(
        _launchd_scalar(proc.stdout, "working directory")
    ).expanduser()
    if not path.is_absolute() or not program.is_absolute() or not working_directory.is_absolute():
        raise CodexRuntimeError(f"LaunchAgent reports a non-absolute path: {label}")

    arguments = _launchd_block(proc.stdout, "arguments")
    if not arguments:
        raise CodexRuntimeError(f"LaunchAgent has no arguments: {label}")
    environment_values: list[tuple[str, str]] = []
    for entry in _launchd_block(proc.stdout, "environment"):
        if " => " not in entry:
            raise CodexRuntimeError(f"LaunchAgent environment entry is malformed: {entry!r}")
        name, value = entry.split(" => ", 1)
        if not name or not value or any(existing == name for existing, _ in environment_values):
            raise CodexRuntimeError(
                f"LaunchAgent environment entry is missing or duplicated: {name!r}"
            )
        environment_values.append((name, value))
    return LaunchdJobSnapshot(
        path=Path(os.path.normpath(str(path))),
        state=state,
        program=Path(os.path.normpath(str(program))),
        arguments=arguments,
        environment=tuple(sorted(environment_values)),
        working_directory=Path(os.path.normpath(str(working_directory))),
        pid=pid,
    )


def launchd_loaded(label: str) -> bool:
    return _launchd_print(label).returncode == 0


def launchd_running(label: str) -> bool:
    proc = _launchd_print(label)
    return proc.returncode == 0 and _launchd_scalar(proc.stdout, "state") == "running"


def _wait_for_launchd_unloaded(
    label: str,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.1,
) -> None:
    """Wait until a successful bootout has actually left the launchd domain."""

    deadline = time.monotonic() + timeout
    while True:
        proc = _launchd_print(label)
        if proc.returncode == 113:
            return
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() or "no diagnostic output"
            raise CodexRuntimeError(
                f"launchctl print failed for {label} "
                f"(exit {proc.returncode}): {detail}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexRuntimeError(
                f"timed out waiting for LaunchAgent to unload: {label}"
            )
        time.sleep(min(poll_interval, remaining))


def install_launch_agent(
    label: str,
    payload: dict,
    *,
    reload: bool = False,
    unload_timeout: float = 10.0,
) -> Path:
    path = launch_agent_path(label)
    _write_plist(path, payload)
    loaded = launchd_loaded(label)
    # Explicit reload also repairs a live job whose plist was rewritten by an
    # earlier process before that process could reload it.
    if loaded and reload:
        proc = subprocess.run(
            [launchctl_bin(), "bootout", f"{launch_domain()}/{label}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise CodexRuntimeError(
                f"launchctl bootout failed for {label}: {proc.stderr.strip()}"
            )
        # launchctl can return from bootout before launchd has removed the
        # service.  Bootstrapping the same label during that transition fails
        # with a misleading I/O error, so observe the not-loaded state first.
        _wait_for_launchd_unloaded(label, timeout=unload_timeout)
        loaded = False
    if not loaded:
        proc = subprocess.run(
            [launchctl_bin(), "bootstrap", launch_domain(), str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise CodexRuntimeError(
                f"launchctl bootstrap failed for {label}: {proc.stderr.strip()}"
            )
    return path


def kickstart(label: str, *, force: bool = True) -> None:
    command = [launchctl_bin(), "kickstart"]
    if force:
        command.append("-k")
    command.append(f"{launch_domain()}/{label}")
    proc = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise CodexRuntimeError(
            f"launchctl kickstart failed for {label}: {proc.stderr.strip()}"
        )


def _secure_path_info(
    path: Path,
    *,
    kind: str,
    private: bool = True,
) -> os.stat_result | None:
    """Inspect a service-owned leaf without following a symlink."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CodexRuntimeError(f"cannot inspect service path {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise CodexRuntimeError(f"service path is a symlink: {path}")
    expected = {
        "directory": stat.S_ISDIR,
        "file": stat.S_ISREG,
        "socket": stat.S_ISSOCK,
    }.get(kind)
    if expected is None or not expected(info.st_mode):
        raise CodexRuntimeError(f"service path is not a {kind}: {path}")
    if info.st_uid != os.getuid():
        raise CodexRuntimeError(
            f"service path owner uid {info.st_uid} != {os.getuid()}: {path}"
        )
    if private and info.st_mode & 0o077:
        raise CodexRuntimeError(
            f"service path exposes group/other permissions: {path}"
        )
    return info


def wake_bridge_build_fingerprint(root: Path | None = None) -> str:
    """Fingerprint the exact code loaded by one wake-bridge process.

    ``SOURCE_ROOT`` resolves the installed ``current`` symlink at interpreter
    startup.  A running bridge therefore keeps the fingerprint of the version
    it actually imported, while a later launcher computes the fingerprint from
    the newly selected install.  Hash every local module used by the bridge so
    dependency-only changes cannot masquerade as the current build.
    """

    selected_root = Path(root if root is not None else SOURCE_ROOT).expanduser()
    if not selected_root.is_absolute():
        raise CodexRuntimeError(
            f"wake bridge source root must be absolute: {selected_root}"
        )
    digest = hashlib.sha256()
    for relative in WAKE_BRIDGE_BUILD_COMPONENTS:
        path = selected_root / relative
        try:
            info = path.lstat()
        except OSError as error:
            raise CodexRuntimeError(
                f"cannot inspect wake bridge build component {path}: {error}"
            ) from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise CodexRuntimeError(
                f"wake bridge build component is not a regular file: {path}"
            )
        if info.st_uid != os.getuid():
            raise CodexRuntimeError(
                f"wake bridge build component owner uid {info.st_uid} != "
                f"{os.getuid()}: {path}"
            )
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise CodexRuntimeError(
                f"cannot read wake bridge build component {path}: {error}"
            ) from error
        encoded_name = relative.encode("utf-8")
        digest.update(len(encoded_name).to_bytes(4, "big"))
        digest.update(encoded_name)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return f"sha256:{digest.hexdigest()}"


def _validate_service_paths(socket_path: Path) -> None:
    runtime_info = _secure_path_info(RUNTIME_DIR, kind="directory")
    if runtime_info is not None:
        projects = RUNTIME_DIR / "projects"
        if projects.exists() or projects.is_symlink():
            _secure_path_info(projects, kind="directory")
    socket_parent = socket_path.parent
    if socket_parent.exists() or socket_parent.is_symlink():
        _secure_path_info(socket_parent, kind="directory")
    if socket_path.exists() or socket_path.is_symlink():
        _secure_path_info(socket_path, kind="socket")
    for label in (APP_SERVER_LABEL, WAKE_LABEL):
        path = launch_agent_path(label)
        if path.exists() or path.is_symlink():
            _secure_path_info(path, kind="file")


def _setup_manifest() -> dict | None:
    """Load the install-scoped ownership record used for safe plist upgrades."""

    if not INSTALL_PREFIX:
        return None
    prefix = Path(INSTALL_PREFIX).expanduser().absolute()
    path = prefix / "harness-setup.json"
    if not (path.exists() or path.is_symlink()):
        return None
    _secure_path_info(path, kind="file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CodexRuntimeError(f"invalid harness setup manifest {path}: {error}") from error
    if (
        not isinstance(value, dict)
        or value.get("schema") != "roundtable.harness-setup.v1"
        or value.get("prefix") != str(prefix)
        or value.get("home") != str(Path.home())
        or not isinstance(value.get("harnesses"), dict)
    ):
        raise CodexRuntimeError(f"unsafe harness setup manifest scope or schema: {path}")
    return value


def codex_reload_marker_path(prefix: Path | None = None) -> Path:
    """Return the install-scoped durable app-server reload marker path."""

    selected = prefix
    if selected is None:
        if not INSTALL_PREFIX:
            raise CodexRuntimeError(
                "Codex reload marker is unavailable without an install prefix"
            )
        selected = Path(INSTALL_PREFIX)
    root = Path(selected).expanduser().absolute()
    return root / ".runtime" / CODEX_RELOAD_MARKER_NAME


def codex_reload_marker_payload(
    app_payload: dict,
    *,
    prefix: Path | None = None,
) -> dict[str, str]:
    """Bind one pending reload to the exact managed app-server plist."""

    marker_path = codex_reload_marker_path(prefix)
    root = marker_path.parent.parent
    content = plistlib.dumps(app_payload, fmt=plistlib.FMT_XML, sort_keys=True)
    return {
        "schema": CODEX_RELOAD_MARKER_SCHEMA,
        "prefix": str(root),
        "label": APP_SERVER_LABEL,
        "appPlistPath": str(launch_agent_path(APP_SERVER_LABEL)),
        "appPlistDigest": hashlib.sha256(content).hexdigest(),
    }


def _read_codex_reload_marker(
    app_payload: dict,
) -> tuple[Path, dict[str, str]] | None:
    """Read and authenticate the pending reload marker without mutating it."""

    if not INSTALL_PREFIX:
        return None
    path = codex_reload_marker_path()
    prefix = path.parent.parent
    prefix_info = _secure_path_info(prefix, kind="directory", private=False)
    if prefix_info is None:
        raise CodexRuntimeError(f"Roundtable install prefix is missing: {prefix}")
    runtime_info = _secure_path_info(path.parent, kind="directory")
    if runtime_info is None:
        return None
    if not (path.exists() or path.is_symlink()):
        return None
    try:
        before = _secure_path_info(path, kind="file")
    except CodexRuntimeError as error:
        raise CodexRuntimeError(
            f"unsafe Codex reload marker {path}: {error}"
        ) from error
    assert before is not None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != os.getuid()
                or opened.st_mode & 0o077
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise CodexRuntimeError(
                    f"Codex reload marker changed or became unsafe: {path}"
                )
            raw = handle.read(CODEX_RELOAD_MARKER_MAX_BYTES + 1)
    except CodexRuntimeError:
        raise
    except OSError as error:
        raise CodexRuntimeError(f"cannot read Codex reload marker {path}: {error}") from error
    if len(raw) > CODEX_RELOAD_MARKER_MAX_BYTES:
        raise CodexRuntimeError(
            f"Codex reload marker exceeds {CODEX_RELOAD_MARKER_MAX_BYTES} bytes: {path}"
        )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CodexRuntimeError(f"invalid Codex reload marker {path}: {error}") from error
    expected = codex_reload_marker_payload(app_payload)
    if value != expected:
        raise CodexRuntimeError(
            "Codex reload marker does not match the current managed app-server "
            f"plist: {path}"
        )
    return path, expected


def codex_reload_required(app_payload: dict) -> bool:
    """Return whether setup durably recorded a pending app-server reload."""

    return _read_codex_reload_marker(app_payload) is not None


def clear_codex_reload_marker(app_payload: dict) -> bool:
    """Clear an authenticated marker after the exact app-server is loaded.

    Callers must hold both the host service repair lock and the install setup
    lock so a concurrent setup cannot replace the plist/marker between the
    successful load and this unlink.
    """

    observed = _read_codex_reload_marker(app_payload)
    if observed is None:
        return False
    path, _value = observed
    current = _secure_path_info(path, kind="file")
    if current is None:
        raise CodexRuntimeError(f"Codex reload marker disappeared before clear: {path}")
    try:
        path.unlink()
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(path.parent, flags)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise CodexRuntimeError(f"cannot clear Codex reload marker {path}: {error}") from error
    return True


def _manifest_owns_plist(
    manifest: dict | None,
    label: str,
    path: Path,
    content: bytes,
) -> bool:
    if manifest is None:
        return False
    codex = manifest.get("harnesses", {}).get("codex")
    if not isinstance(codex, dict) or not isinstance(codex.get("plists"), list):
        return False
    digest = hashlib.sha256(content).hexdigest()
    matches = [
        item
        for item in codex["plists"]
        if isinstance(item, dict) and item.get("label") == label
    ]
    if len(matches) != 1:
        return False
    item = matches[0]
    return item.get("path") == str(path) and item.get("digest") == digest


def _manifest_owns_current_codex_hook(manifest: dict | None) -> bool:
    """Recognize the complete SessionStart ownership record from setup.

    This intentionally validates metadata rather than editing or even reading
    the user's hook file.  The public ``roundtable`` entry performs the full
    ownership-safe config validation; the low-level installed launcher only
    needs to reject pre-hook manifests and send the user through that flow.
    """

    if not INSTALL_PREFIX or manifest is None:
        return False
    codex = manifest.get("harnesses", {}).get("codex")
    if not isinstance(codex, dict):
        return False
    config = codex.get("config")
    if not isinstance(config, dict):
        return False
    prefix = Path(INSTALL_PREFIX).expanduser().absolute()
    expected_group = {
        "matcher": "startup|resume|clear",
        "hooks": [
            {
                "type": "command",
                "command": str(prefix / "bin" / "rt-codex-session-start"),
                "timeout": 5,
            }
        ],
    }
    fragments = config.get("fragments")
    events = config.get("event_containers_added")
    if (
        config.get("path") != str(CODEX_HOME / "hooks.json")
        or not isinstance(config.get("created"), bool)
        or not isinstance(config.get("hooks_container_added"), bool)
        or not isinstance(events, dict)
        or not isinstance(events.get("SessionStart"), bool)
        or not isinstance(fragments, list)
        or len(fragments) != 1
    ):
        return False
    fragment = fragments[0]
    return bool(
        isinstance(fragment, dict)
        and fragment.get("event") == "SessionStart"
        and fragment.get("group") == expected_group
        and isinstance(fragment.get("added"), bool)
    )


def _plist_state(
    label: str,
    expected: dict,
    manifest: dict | None,
) -> str:
    path = launch_agent_path(label)
    if not (path.exists() or path.is_symlink()):
        return "missing"
    _secure_path_info(path, kind="file")
    try:
        content = path.read_bytes()
        current = plistlib.loads(content)
    except (OSError, plistlib.InvalidFileException) as error:
        raise CodexRuntimeError(f"invalid LaunchAgent plist {path}: {error}") from error
    if not isinstance(current, dict) or current.get("Label") != label:
        raise CodexRuntimeError(f"LaunchAgent plist has foreign label at {path}")
    if current == expected:
        # Installed launchers may operate on a plist only after the explicit
        # setup flow has adopted it into the install-scoped ownership record.
        # Source-tree developer runs have no install manifest and retain their
        # existing explicit setup workflow.
        if INSTALL_PREFIX and not _manifest_owns_plist(
            manifest, label, path, content
        ):
            return "unowned_current"
        return "current"
    if _manifest_owns_plist(manifest, label, path, content):
        return "owned_drift"
    raise CodexRuntimeError(
        f"LaunchAgent plist differs from Roundtable and is not proven owned: {path}"
    )


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def wake_bridge_health(
    socket_path: Path = DEFAULT_SOCKET,
    *,
    max_age: float = 15.0,
) -> tuple[bool, str]:
    """Validate launchd state plus the bridge PID/RPC heartbeat."""

    if not launchd_running(WAKE_LABEL):
        return False, "wake LaunchAgent is not running"
    pid_path = RUNTIME_DIR / "rt-codex-wake.pid"
    heartbeat_path = RUNTIME_DIR / "rt-codex-wake-heartbeat.json"
    if not (pid_path.exists() or pid_path.is_symlink()):
        return False, f"missing bridge pid file {pid_path}"
    if not (heartbeat_path.exists() or heartbeat_path.is_symlink()):
        return False, f"missing bridge heartbeat {heartbeat_path}"
    _secure_path_info(pid_path, kind="file")
    _secure_path_info(heartbeat_path, kind="file")
    alive, detail = pid_is_running(pid_path, "rt-codex-wake")
    if not alive:
        return False, detail
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        return False, f"invalid bridge heartbeat: {error}"
    if not isinstance(heartbeat, dict):
        return False, "bridge heartbeat is not an object"
    if heartbeat.get("schema") != "roundtable.codex-wake-heartbeat.v1":
        return False, "bridge heartbeat schema is missing or invalid"
    expected_build = wake_bridge_build_fingerprint()
    reported_build = heartbeat.get("bridgeBuildFingerprint")
    if reported_build != expected_build:
        rendered = reported_build if isinstance(reported_build, str) else "missing"
        return (
            False,
            "wake bridge build fingerprint is stale or invalid: "
            f"reported={rendered} expected={expected_build}",
        )
    heartbeat_at = _parse_timestamp(heartbeat.get("heartbeatAt"))
    heartbeat_age = (
        max(0.0, (datetime.now(timezone.utc) - heartbeat_at).total_seconds())
        if heartbeat_at is not None
        else None
    )
    if heartbeat.get("pid") != pid:
        rendered_age = (
            f"{heartbeat_age:.1f}s" if heartbeat_age is not None else "unknown"
        )
        return (
            False,
            f"bridge heartbeat pid {heartbeat.get('pid')} != live pid {pid}; "
            f"heartbeat age={rendered_age}",
        )
    reported_socket = heartbeat.get("socketPath")
    if not isinstance(reported_socket, str) or not reported_socket:
        return False, "bridge heartbeat socketPath is missing or invalid"
    if Path(reported_socket).expanduser().absolute() != socket_path.expanduser().absolute():
        return False, f"bridge heartbeat socket {reported_socket} != {socket_path}"
    last_error = heartbeat.get("lastError")
    if last_error not in (None, ""):
        if not isinstance(last_error, str):
            return False, "bridge heartbeat lastError is invalid"
        return False, f"bridge reports error: {last_error}"
    last_rpc_ok = _parse_timestamp(heartbeat.get("lastRpcOkAt"))
    if heartbeat_at is None:
        return False, "bridge heartbeat timestamp is missing or invalid"
    if last_rpc_ok is None:
        return False, "last successful bridge RPC timestamp is missing or invalid"
    now = datetime.now(timezone.utc)
    heartbeat_age = max(0.0, (now - heartbeat_at).total_seconds())
    rpc_age = max(0.0, (now - last_rpc_ok).total_seconds())
    if heartbeat_age > max_age:
        return False, f"bridge heartbeat is stale ({heartbeat_age:.1f}s > {max_age:.1f}s)"
    if rpc_age > max_age:
        return False, f"last successful bridge RPC is stale ({rpc_age:.1f}s > {max_age:.1f}s)"
    return True, f"{detail}, heartbeat age={heartbeat_age:.1f}s, last RPC age={rpc_age:.1f}s"


def _reload_status(
    detail: str,
    *,
    cli_version: tuple[int, int, int],
    daemon: dict | None,
    app_plist: str,
    wake_plist: str,
) -> CodexServiceStatus:
    blockers: list[str] = []
    if os.environ.get("CODEX_THREAD_ID"):
        blockers.append("the caller is itself a Codex thread")
    try:
        inspections = inspect_host_harness_seats("codex")
    except RuntimeStateError as error:
        blockers.append(f"host Codex lease state is ambiguous: {error}")
    else:
        for inspection in inspections:
            if inspection.status not in {
                "active_healthy",
                "active_unhealthy",
                "ambiguous",
            }:
                continue
            token = inspection.token
            identity = (
                f"{token.agent_id}@{token.project_root}"
                if token is not None
                else "unknown Codex seat"
            )
            blockers.append(f"{identity} is {inspection.status}: {inspection.detail}")
    state = SERVICE_RELOAD_DEFERRED_BUSY if blockers else SERVICE_RELOAD_REQUIRED_IDLE
    rendered = detail
    if blockers:
        rendered += "; reload deferred: " + " | ".join(blockers)
    return CodexServiceStatus(
        state,
        rendered,
        cli_version,
        daemon,
        app_plist,
        wake_plist,
    )


def inspect_codex_services(
    socket_path: Path = DEFAULT_SOCKET,
    *,
    bridge_max_age: float = 15.0,
) -> CodexServiceStatus:
    """Classify the service pair without mutating files or launchd state."""

    try:
        require_default_socket(socket_path)
        _validate_service_paths(socket_path)
        manifest = _setup_manifest()
        hook_owned = (
            not bool(INSTALL_PREFIX)
            or _manifest_owns_current_codex_hook(manifest)
        )
        expected_app = app_server_plist(socket_path, ensure_runtime=False)
        expected_wake = wake_plist(socket_path, ensure_runtime=False)
        app_state = _plist_state(APP_SERVER_LABEL, expected_app, manifest)
        wake_state = _plist_state(WAKE_LABEL, expected_wake, manifest)
        reload_pending = codex_reload_required(expected_app)
        cli_version, output = codex_version()
        if cli_version is None:
            return CodexServiceStatus(
                SERVICE_UNSUPPORTED,
                f"could not parse Codex version: {output}",
                app_plist=app_state,
                wake_plist=wake_state,
            )
        rendered = ".".join(str(part) for part in cli_version)
        if cli_version < MINIMUM_CODEX_RELEASE:
            return CodexServiceStatus(
                SERVICE_UNSUPPORTED,
                f"Codex {rendered} is below the minimum supported app-server "
                f"release {minimum_release_text()}",
                cli_version,
                app_plist=app_state,
                wake_plist=wake_state,
            )
    except UnsupportedVersion as error:
        return CodexServiceStatus(SERVICE_UNSUPPORTED, str(error))
    except (CodexRuntimeError, RuntimeStateError, OSError) as error:
        return CodexServiceStatus(SERVICE_UNSAFE, str(error))

    # Service repair must never create an ownership gap or make the setup
    # manifest stale.  The public ``roundtable`` entry runs ownership-safe
    # setup before this preflight; direct/internal rt-codex callers get a
    # closed, actionable failure instead of an unmanaged plist rewrite.
    if (
        not hook_owned
        or app_state in {"missing", "owned_drift", "unowned_current"}
        or wake_state in {
            "missing",
            "owned_drift",
            "unowned_current",
        }
    ):
        reasons = []
        if not hook_owned:
            reasons.append("Codex SessionStart hook ownership is missing or outdated")
        if app_state != "current":
            reasons.append(f"app-server plist is {app_state}")
        if wake_state != "current":
            reasons.append(f"wake plist is {wake_state}")
        return CodexServiceStatus(
            SERVICE_SETUP_REQUIRED,
            "; ".join(reasons)
            + "; run `roundtable setup apply --harness codex` before launch",
            cli_version,
            app_plist=app_state,
            wake_plist=wake_state,
        )

    ok, handshake_detail, handshake_error = probe_handshake_detailed(
        socket_path, timeout=1.0
    )
    if not ok:
        try:
            app_loaded = launchd_loaded(APP_SERVER_LABEL)
        except (CodexRuntimeError, OSError) as error:
            return CodexServiceStatus(
                SERVICE_UNSAFE,
                f"cannot inspect app-server LaunchAgent: {error}",
                cli_version,
                app_plist=app_state,
                wake_plist=wake_state,
            )
        if app_loaded:
            return _reload_status(
                f"loaded app-server is unavailable: {handshake_detail}",
                cli_version=cli_version,
                daemon=None,
                app_plist=app_state,
                wake_plist=wake_state,
            )
        if repairable_probe_failure(socket_path, handshake_error):
            return CodexServiceStatus(
                SERVICE_COLD,
                handshake_detail,
                cli_version,
                app_plist=app_state,
                wake_plist=wake_state,
            )
        return CodexServiceStatus(
            SERVICE_UNSAFE,
            f"non-liveness app-server failure: {handshake_detail}",
            cli_version,
            app_plist=app_state,
            wake_plist=wake_state,
        )

    daemon, daemon_detail = daemon_version(socket_path)
    if not daemon or daemon.get("status") != "running":
        return CodexServiceStatus(
            SERVICE_UNSAFE,
            f"responsive app-server could not be version-validated: {daemon_detail}",
            cli_version=cli_version,
            daemon=daemon,
            app_plist=app_state,
            wake_plist=wake_state,
        )
    try:
        require_daemon_identity(daemon, socket_path, cli_version)
    except CodexDaemonReloadRequired as error:
        return _reload_status(
            str(error),
            cli_version=cli_version,
            daemon=daemon,
            app_plist=app_state,
            wake_plist=wake_state,
        )
    except CodexRuntimeError as error:
        return CodexServiceStatus(
            SERVICE_UNSAFE,
            str(error),
            cli_version,
            daemon,
            app_state,
            wake_state,
        )
    # Every identity-proven daemon is probed: the floor is a protocol claim,
    # not a validated allowlist, so a passing read-only probe is the launch
    # evidence for any release.  The probe runs only after the daemon identity
    # is proven, so its target is a confirmed Roundtable daemon.
    probe_ok, probe_detail = codex_protocol_probe(socket_path)
    if not probe_ok:
        return CodexServiceStatus(
            SERVICE_UNSUPPORTED,
            f"Codex {rendered} failed the app-server protocol probe: "
            f"{probe_detail}",
            cli_version,
            daemon,
            app_state,
            wake_state,
        )
    if reload_pending:
        return _reload_status(
            "setup recorded a pending reload for the current app-server plist",
            cli_version=cli_version,
            daemon=daemon,
            app_plist=app_state,
            wake_plist=wake_state,
        )
    try:
        bridge_ok, bridge_detail = wake_bridge_health(
            socket_path, max_age=bridge_max_age
        )
    except (CodexRuntimeError, OSError) as error:
        return CodexServiceStatus(
            SERVICE_UNSAFE,
            str(error),
            cli_version,
            daemon,
            app_state,
            wake_state,
        )
    if not bridge_ok:
        return CodexServiceStatus(
            SERVICE_BRIDGE_DOWN,
            bridge_detail,
            cli_version,
            daemon,
            app_state,
            wake_state,
            bridge_detail,
        )
    return CodexServiceStatus(
        SERVICE_READY,
        "Codex app-server and wake bridge are ready",
        cli_version,
        daemon,
        app_state,
        wake_state,
        bridge_detail,
    )


@contextmanager
def codex_service_repair_lock(timeout: float = 10.0):
    """Serialize host service repairs and reject an unsafe existing lock."""

    if RUNTIME_DIR.exists() or RUNTIME_DIR.is_symlink():
        _secure_path_info(RUNTIME_DIR, kind="directory")
    else:
        ensure_private_runtime_dir()
    path = RUNTIME_DIR / "codex-service-repair.lock"
    if path.exists() or path.is_symlink():
        _secure_path_info(path, kind="file")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise CodexRuntimeError(f"cannot open Codex service repair lock {path}: {error}") from error
    handle = os.fdopen(descriptor, "r+")
    deadline = time.monotonic() + timeout
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise CodexRuntimeError(f"unsafe Codex service repair lock: {path}")
        if info.st_mode & 0o077:
            raise CodexRuntimeError(
                f"Codex service repair lock exposes group/other permissions: {path}"
            )
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CodexRuntimeError(
                        "timed out waiting for Codex service repair lock"
                    )
                time.sleep(0.1)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def codex_setup_state_lock(timeout: float = 10.0):
    """Serialize installed preflight reads with setup marker/plist mutations."""

    if not INSTALL_PREFIX:
        yield
        return
    path = codex_reload_marker_path().parent / "harness-setup.lock"
    runtime_info = _secure_path_info(path.parent, kind="directory")
    if runtime_info is None:
        raise CodexRuntimeError(
            f"installed Codex setup runtime directory is missing: {path.parent}"
        )
    if path.exists() or path.is_symlink():
        _secure_path_info(path, kind="file")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise CodexRuntimeError(
            f"cannot open Codex setup state lock {path}: {error}"
        ) from error
    deadline = time.monotonic() + timeout
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise CodexRuntimeError(f"unsafe Codex setup state lock: {path}")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise CodexRuntimeError(
                        f"timed out waiting for Codex setup state lock: {path}"
                    )
                time.sleep(0.05)
        yield
    except OSError as error:
        raise CodexRuntimeError(
            f"cannot lock Codex setup state at {path}: {error}"
        ) from error
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _wait_for_daemon(socket_path: Path, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_detail = "daemon did not become ready"
    while time.monotonic() < deadline:
        ok, last_detail = probe_handshake(socket_path, timeout=0.5)
        if ok:
            require_supported_daemon(socket_path)
            return
        time.sleep(0.2)
    raise CodexRuntimeError(f"app-server reload failed: {last_detail}")


def _wait_for_wake_bridge_takeover(
    socket_path: Path,
    *,
    timeout: float = WAKE_BRIDGE_TAKEOVER_TIMEOUT_SECONDS,
    poll_interval: float = WAKE_BRIDGE_TAKEOVER_POLL_SECONDS,
) -> None:
    """Wait for the repaired bridge's fresh, PID-matched heartbeat."""

    deadline = time.monotonic() + timeout
    last_detail = "wake bridge did not become ready"
    while True:
        ok, last_detail = wake_bridge_health(socket_path)
        if ok:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))
    raise CodexRuntimeError(f"wake bridge repair failed: {last_detail}")


def _restart_wake_bridge(socket_path: Path) -> None:
    require_supported_daemon(socket_path)
    install_launch_agent(WAKE_LABEL, wake_plist(socket_path), reload=True)
    kickstart(WAKE_LABEL, force=False)
    _wait_for_wake_bridge_takeover(socket_path)


def _reload_service_pair(socket_path: Path, timeout: float) -> None:
    install_launch_agent(
        APP_SERVER_LABEL,
        app_server_plist(socket_path),
        reload=True,
    )
    kickstart(APP_SERVER_LABEL, force=False)
    _wait_for_daemon(socket_path, timeout)
    _restart_wake_bridge(socket_path)


def codex_launch_preflight(
    socket_path: Path = DEFAULT_SOCKET,
    *,
    confirm_reload: Callable[[CodexServiceStatus], bool] | None = None,
    ready_action: Callable[[], None] | None = None,
    timeout: float = 10.0,
) -> CodexServiceStatus:
    """Make services ready and publish the client seat under the host lock.

    ``ready_action`` is intentionally executed only after a final in-lock READY
    observation.  The Codex launcher uses it to claim its seat, closing the
    scan-to-reload race: once the lock is released, every later reload scan can
    see that lease.
    """

    reload_approved = False
    for _attempt in range(6):
        # Avoid observing setup's marker/plist/manifest transaction halfway
        # through. This first snapshot is advisory (for prompting); the final
        # decision is repeated under both locks below.
        with codex_setup_state_lock(timeout):
            status = inspect_codex_services(socket_path)
        if status.state in {
            SERVICE_UNSUPPORTED,
            SERVICE_UNSAFE,
            SERVICE_SETUP_REQUIRED,
            SERVICE_RELOAD_DEFERRED_BUSY,
        }:
            raise CodexRuntimeError(
                f"Codex service preflight {status.state}: {status.detail}"
            )
        if status.state == SERVICE_RELOAD_REQUIRED_IDLE and not reload_approved:
            if confirm_reload is None or not confirm_reload(status):
                raise CodexRuntimeError(
                    "Codex service reload is required but was not approved: "
                    f"{status.detail}"
                )
            reload_approved = True

        with codex_service_repair_lock(timeout):
            with codex_setup_state_lock(timeout):
                # Another launcher may have repaired or setup may have changed
                # the managed plist while we waited. Every mutation decision is
                # based on this in-lock snapshot, never the advisory one above.
                current = inspect_codex_services(socket_path)
                if current.state == SERVICE_READY:
                    if ready_action is not None:
                        ready_action()
                    return current
                if current.state == SERVICE_COLD:
                    reload_payload = None
                    if INSTALL_PREFIX:
                        candidate = app_server_plist(
                            socket_path,
                            ensure_runtime=False,
                        )
                        if codex_reload_required(candidate):
                            reload_payload = candidate
                    if reload_payload is None:
                        ensure_daemon(socket_path, timeout=timeout)
                    else:
                        # A marker means setup wrote a specific new plist. Use
                        # the coordinated loader even for a nominally cold job
                        # so the exact marked definition is what becomes live.
                        _reload_service_pair(socket_path, timeout)
                        clear_codex_reload_marker(reload_payload)
                    continue
                if current.state == SERVICE_BRIDGE_DOWN:
                    _restart_wake_bridge(socket_path)
                    continue
                if (
                    current.state == SERVICE_RELOAD_REQUIRED_IDLE
                    and reload_approved
                ):
                    reload_payload = (
                        app_server_plist(socket_path, ensure_runtime=False)
                        if INSTALL_PREFIX
                        else None
                    )
                    _reload_service_pair(socket_path, timeout)
                    if reload_payload is not None:
                        clear_codex_reload_marker(reload_payload)
                    continue
                if current.state == SERVICE_RELOAD_REQUIRED_IDLE:
                    continue
                raise CodexRuntimeError(
                    f"Codex service preflight {current.state}: {current.detail}"
                )
    raise CodexRuntimeError("Codex service preflight did not converge")


class WebSocketUnix:
    """Small RFC 6455 client for the app-server's local Unix socket."""

    MAX_MESSAGE = 16 * 1024 * 1024

    def __init__(self, path: Path, timeout: float = 3.0):
        self.path = Path(path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(self.path))
        self._upgrade()

    def _upgrade(self) -> None:
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CodexRuntimeError("app-server closed during WebSocket upgrade")
            response.extend(chunk)
            if len(response) > 65536:
                raise CodexRuntimeError("oversized WebSocket upgrade response")
        header, _, extra = bytes(response).partition(b"\r\n\r\n")
        if extra:
            raise CodexRuntimeError("unexpected frame bytes in WebSocket upgrade response")
        lines = header.decode("latin1").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            raise CodexRuntimeError(f"WebSocket upgrade failed: {lines[0] if lines else ''}")
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise CodexRuntimeError("invalid WebSocket Sec-WebSocket-Accept")

    def close(self) -> None:
        try:
            self._send_frame(b"", opcode=0x8)
        except OSError:
            pass
        self.sock.close()

    def fileno(self) -> int:
        return self.sock.fileno()

    def _send_frame(self, payload: bytes, *, opcode: int) -> None:
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_json(self, payload: dict) -> None:
        self._send_frame(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            opcode=0x1,
        )

    def _read_exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = self.sock.recv(size - len(chunks))
            if not chunk:
                raise CodexRuntimeError("app-server WebSocket closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        if length > self.MAX_MESSAGE:
            raise CodexRuntimeError(f"WebSocket message exceeds {self.MAX_MESSAGE} bytes")
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length)
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def recv_json(self, timeout: float | None = None) -> dict:
        previous_timeout = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            parts = []
            message_opcode = None
            while True:
                fin, opcode, payload = self._read_frame()
                if opcode == 0x8:
                    raise CodexRuntimeError("app-server closed WebSocket")
                if opcode == 0x9:
                    self._send_frame(payload, opcode=0xA)
                    continue
                if opcode == 0xA:
                    continue
                if opcode in (0x1, 0x2):
                    message_opcode = opcode
                    parts = [payload]
                elif opcode == 0x0 and message_opcode is not None:
                    parts.append(payload)
                else:
                    raise CodexRuntimeError(f"unexpected WebSocket opcode {opcode}")
                if not fin:
                    if sum(len(part) for part in parts) > self.MAX_MESSAGE:
                        raise CodexRuntimeError(
                            f"WebSocket message exceeds {self.MAX_MESSAGE} bytes"
                        )
                    continue
                if message_opcode != 0x1:
                    raise CodexRuntimeError("expected a WebSocket text message")
                try:
                    message = json.loads(b"".join(parts).decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise CodexRuntimeError(
                        f"invalid app-server JSON message: {error}"
                    ) from error
                if not isinstance(message, dict):
                    raise CodexRuntimeError(
                        "invalid app-server JSON message: expected an object"
                    )
                return message
        finally:
            self.sock.settimeout(previous_timeout)


class AppServerClient:
    def __init__(self, path: Path = DEFAULT_SOCKET, timeout: float = 3.0):
        self.path = Path(path)
        self.transport = WebSocketUnix(self.path, timeout=timeout)
        self.timeout = timeout
        self.next_id = 1
        self.notifications: list[dict] = []
        self._initialize()

    def _initialize(self) -> None:
        self.initialize_result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "roundtable_rt_codex_wake",
                    "title": "Roundtable Codex wake bridge",
                    "version": "1.3.0",
                },
                # excludeTurns and bounded turn-history paging use the
                # experimental API; supported releases are validated
                # explicitly above rather than accepted by an open range.
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized")

    def close(self) -> None:
        self.transport.close()

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        self.transport.send_json(payload)

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self.next_id
        self.next_id += 1
        payload = {"method": method, "id": request_id, "params": params or {}}
        self.transport.send_json(payload)
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {method}")
            message = self.transport.recv_json(remaining)
            if message.get("id") == request_id and (
                "result" in message or "error" in message
            ):
                if "error" in message:
                    error = message["error"]
                    if isinstance(error, dict):
                        detail = f"({error.get('code')}): {error.get('message')}"
                    else:
                        detail = repr(error)
                    raise RpcError(f"{method} failed {detail}")
                result = message.get("result")
                if result is None:
                    return {}
                if not isinstance(result, dict):
                    raise CodexRuntimeError(
                        f"invalid {method} result: expected an object"
                    )
                return result
            self.notifications.append(message)

    def drain_notifications(self, timeout: float = 0.0) -> list[dict]:
        messages = self.notifications
        self.notifications = []
        wait = timeout
        while True:
            readable, _, _ = select.select([self.transport], [], [], wait)
            if not readable:
                return messages
            messages.append(self.transport.recv_json(self.timeout))
            wait = 0.0


def probe_handshake_detailed(
    socket_path: Path = DEFAULT_SOCKET, timeout: float = 1.0
) -> tuple[bool, str, Exception | None]:
    client = None
    try:
        client = AppServerClient(socket_path, timeout=timeout)
        return True, "initialize/initialized succeeded", None
    except Exception as error:
        return False, str(error), error
    finally:
        if client is not None:
            client.close()


def probe_handshake(socket_path: Path = DEFAULT_SOCKET, timeout: float = 1.0) -> tuple[bool, str]:
    ok, detail, _error = probe_handshake_detailed(socket_path, timeout)
    return ok, detail


def repairable_probe_failure(socket_path: Path, error: Exception | None) -> bool:
    if isinstance(error, PermissionError):
        return False
    if not socket_path.exists():
        return True
    return isinstance(
        error,
        (
            FileNotFoundError,
            ConnectionRefusedError,
            ConnectionResetError,
            ConnectionAbortedError,
        ),
    )


def require_default_socket(socket_path: Path) -> None:
    """Reject endpoints that would reuse the global LaunchAgent label unsafely."""
    if socket_path != DEFAULT_SOCKET:
        raise UnsupportedVersion(
            f"non-default app-server socket is not version-validated: {socket_path}"
        )


def ensure_daemon(socket_path: Path = DEFAULT_SOCKET, timeout: float = 10.0) -> None:
    """Handshake the daemon, single-flight install/kickstart it if unavailable."""
    # This must precede every probe and launchd operation. The app-server uses
    # one global label, so healing a custom endpoint would otherwise boot out
    # and rewrite the healthy default daemon first.
    require_default_socket(socket_path)
    ok, detail, error = probe_handshake_detailed(
        socket_path, timeout=min(1.0, timeout)
    )
    if ok:
        return
    if not repairable_probe_failure(socket_path, error):
        raise CodexRuntimeError(
            f"refusing daemon restart after non-liveness probe failure: {detail}"
        )

    runtime_dir = ensure_private_runtime_dir()
    lock = runtime_dir / "codex-app-server-start.lock"
    deadline = time.monotonic() + timeout
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    handle = os.fdopen(descriptor, "r+")
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            ok, wait_detail, wait_error = probe_handshake_detailed(
                socket_path, timeout=0.5
            )
            if ok:
                handle.close()
                return
            if not repairable_probe_failure(socket_path, wait_error):
                handle.close()
                raise CodexRuntimeError(
                    "refusing daemon restart while another caller holds the lock "
                    f"after non-liveness probe failure: {wait_detail}"
                )
            if time.monotonic() >= deadline:
                handle.close()
                raise CodexRuntimeError(
                    "timed out waiting for app-server single-flight lock"
                )
            time.sleep(0.2)

    try:
        ok, detail, error = probe_handshake_detailed(socket_path, timeout=0.5)
        if ok:
            return
        if not repairable_probe_failure(socket_path, error):
            raise CodexRuntimeError(
                f"refusing daemon restart after non-liveness probe failure: {detail}"
            )
        install_launch_agent(
            APP_SERVER_LABEL, app_server_plist(socket_path), reload=True
        )
        kickstart(APP_SERVER_LABEL, force=False)
        last_error = "daemon did not become ready"
        while time.monotonic() < deadline:
            ok, last_error = probe_handshake(socket_path, timeout=0.5)
            if ok:
                return
            time.sleep(0.2)
        raise CodexRuntimeError(f"app-server self-heal failed: {last_error}")
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def parse_codex_version(text: str) -> tuple[int, int, int] | None:
    for token in text.strip().split():
        parts = token.split(".")
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            return tuple(int(part) for part in parts)  # type: ignore[return-value]
    return None


def codex_version() -> tuple[tuple[int, int, int] | None, str]:
    path = codex_bin()
    proc = subprocess.run(
        [str(path), "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env={**os.environ, "PATH": _env_path()},
    )
    output = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        return None, output
    return parse_codex_version(output), output


def daemon_version(socket_path: Path = DEFAULT_SOCKET) -> tuple[dict | None, str]:
    """Ask the installed CLI to inspect the running app-server daemon."""
    proc = subprocess.run(
        [str(codex_bin()), "app-server", "daemon", "version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": _env_path(),
            "RT_CODEX_SOCKET": str(socket_path),
            "CODEX_HOME": str(CODEX_HOME),
        },
    )
    output = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        return None, output
    start = output.find("{")
    if start < 0:
        return None, output
    try:
        value = json.loads(output[start:])
    except json.JSONDecodeError:
        return None, output
    if not isinstance(value, dict):
        return None, output
    return value, output


@contextmanager
def authenticated_socket_peer(
    socket_path: Path,
    *,
    timeout: float = 1.0,
) -> Iterator[int]:
    """Hold a Darwin Unix-socket connection while yielding its peer PID."""

    if sys.platform != "darwin":
        raise CodexRuntimeError("Unix socket peer PID validation requires macOS")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        # Darwin exposes LOCAL_PEERPID as SOL_LOCAL(0), option 2.  Python does
        # not currently publish symbolic constants for either value.
        raw = client.getsockopt(0, 2, struct.calcsize("i"))
    except OSError as error:
        client.close()
        raise CodexRuntimeError(
            f"cannot authenticate Unix socket peer {socket_path}: {error}"
        ) from error
    if len(raw) != struct.calcsize("i"):
        client.close()
        raise CodexRuntimeError(
            f"Unix socket peer PID has invalid size {len(raw)}: {socket_path}"
        )
    peer_pid = struct.unpack("i", raw)[0]
    if peer_pid <= 0:
        client.close()
        raise CodexRuntimeError(
            f"Unix socket peer PID is not positive: {socket_path} pid={peer_pid}"
        )
    try:
        yield peer_pid
    finally:
        client.close()


def socket_peer_pid(socket_path: Path, *, timeout: float = 1.0) -> int:
    """Return a point-in-time kernel-authenticated Darwin Unix-socket peer PID."""

    with authenticated_socket_peer(socket_path, timeout=timeout) as peer_pid:
        return peer_pid


def _process_parent_and_uid(pid: int) -> tuple[int, int]:
    proc = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "ppid=", "-o", "uid="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    parts = proc.stdout.split()
    if proc.returncode != 0 or len(parts) != 2:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        raise CodexRuntimeError(f"cannot inspect process {pid}: {detail}")
    try:
        parent_pid, uid = (int(value) for value in parts)
    except ValueError as error:
        raise CodexRuntimeError(
            f"process {pid} has invalid parent/uid data: {proc.stdout.strip()!r}"
        ) from error
    if parent_pid < 0 or uid < 0:
        raise CodexRuntimeError(
            f"process {pid} has negative parent/uid data: {parent_pid}/{uid}"
        )
    return parent_pid, uid


def require_pid_lineage(peer_pid: int, launchd_pid: int) -> None:
    """Require a same-user socket peer to descend from one launchd job PID."""

    current = peer_pid
    seen: set[int] = set()
    for _depth in range(128):
        if current in seen:
            raise CodexRuntimeError(f"process parent cycle while inspecting pid {current}")
        seen.add(current)
        parent, uid = _process_parent_and_uid(current)
        if uid != os.getuid():
            raise CodexRuntimeError(
                f"app-server process uid {uid} != {os.getuid()}: pid={current}"
            )
        if current == launchd_pid:
            return
        if parent <= 0:
            break
        current = parent
    raise CodexRuntimeError(
        "Unix socket peer is not owned by the Roundtable LaunchAgent process tree: "
        f"peer-pid={peer_pid} launchd-pid={launchd_pid}"
    )


def require_roundtable_daemon_owner(socket_path: Path) -> tuple[int, int]:
    """Tie the live socket peer to the exact loaded Roundtable LaunchAgent."""

    _validate_service_paths(socket_path)
    expected = app_server_plist(socket_path, ensure_runtime=False)
    before = launchd_job_snapshot(APP_SERVER_LABEL)
    with authenticated_socket_peer(socket_path) as peer_pid:
        require_pid_lineage(peer_pid, before.pid)
        after = launchd_job_snapshot(APP_SERVER_LABEL)
    if after != before:
        raise CodexRuntimeError(
            "Roundtable app-server LaunchAgent changed during socket ownership validation"
        )

    expected_path = Path(
        os.path.normpath(str(launch_agent_path(APP_SERVER_LABEL).expanduser()))
    )
    expected_args = tuple(str(value) for value in expected["ProgramArguments"])
    expected_program = Path(os.path.normpath(expected_args[0]))
    expected_working_directory = Path(
        os.path.normpath(str(Path(expected["WorkingDirectory"]).expanduser()))
    )
    live_environment = dict(before.environment)
    mismatches: list[str] = []
    if before.path != expected_path:
        mismatches.append(f"plist-path={before.path} expected={expected_path}")
    if before.program != expected_program:
        mismatches.append(f"program={before.program} expected={expected_program}")
    if before.arguments != expected_args:
        mismatches.append("ProgramArguments differ from the current managed plist")
    if before.working_directory != expected_working_directory:
        mismatches.append(
            f"working-directory={before.working_directory} "
            f"expected={expected_working_directory}"
        )
    for name, value in expected["EnvironmentVariables"].items():
        if live_environment.get(name) != str(value):
            mismatches.append(
                f"environment {name}={live_environment.get(name)!r} expected={str(value)!r}"
            )
    unexpected_environment = sorted(
        set(live_environment)
        - set(expected["EnvironmentVariables"])
        - LAUNCHD_APP_SERVER_ENV_ALLOWLIST
    )
    if unexpected_environment:
        mismatches.append(
            "unexpected explicit environment keys="
            + ",".join(unexpected_environment)
        )
    if mismatches:
        raise CodexDaemonReloadRequired(
            "loaded Roundtable app-server definition is stale: " + "; ".join(mismatches)
        )
    return before.pid, peer_pid


def require_daemon_identity(
    daemon: dict,
    socket_path: Path,
    cli_version: tuple[int, int, int],
) -> CodexDaemonIdentity:
    """Validate protocol shape, versions, and Roundtable lifecycle ownership.

    The gate-exercised Codex 0.144.6 was source-verified to report
    ``managedCodexPath`` as a fixed standalone update slot, not as the
    executable serving the socket.  The actual identity proof therefore comes
    from the Roundtable launchd job and the kernel-reported Unix-socket peer
    PID; every release must satisfy these same schema and launchd/peer-PID
    checks at runtime or it fails closed here.
    """

    if daemon.get("status") != "running":
        raise CodexRuntimeError(f"app-server daemon is not running: {daemon.get('status')!r}")
    reported_socket = daemon.get("socketPath")
    if not isinstance(reported_socket, str) or reported_socket != str(socket_path):
        raise CodexRuntimeError(
            f"daemon socket mismatch: {reported_socket!r} != {str(socket_path)!r}"
        )
    if "backend" in daemon:
        backend = daemon.get("backend")
        if backend == "pid":
            raise CodexRuntimeError(
                "app-server is owned by the Codex pid backend daemon, not the "
                "Roundtable LaunchAgent"
            )
        raise CodexRuntimeError(f"daemon backend is invalid or unsupported: {backend!r}")

    managed_value = daemon.get("managedCodexPath")
    if (
        not isinstance(managed_value, str)
        or not managed_value
        or any(character in managed_value for character in "\0\r\n")
    ):
        raise CodexRuntimeError("daemon managedCodexPath is missing or invalid")
    managed_codex = Path(managed_value).expanduser()
    if not managed_codex.is_absolute():
        raise CodexRuntimeError(
            f"daemon managedCodexPath is not absolute: {managed_value!r}"
        )
    managed_codex = Path(os.path.normpath(str(managed_codex)))
    if "managedCodexVersion" not in daemon:
        raise CodexRuntimeError("daemon managedCodexVersion field is missing")
    managed_version = daemon["managedCodexVersion"]
    if managed_version is not None and (
        not isinstance(managed_version, str) or not managed_version.strip()
    ):
        raise CodexRuntimeError("daemon managedCodexVersion is invalid")

    selected_codex = codex_bin().expanduser()
    if not selected_codex.is_absolute():
        raise CodexRuntimeError(f"selected Codex path is not absolute: {selected_codex}")
    selected_codex = Path(os.path.normpath(str(selected_codex)))
    distribution = "standalone" if selected_codex == managed_codex else "external"
    rendered_cli = ".".join(str(part) for part in cli_version)
    daemon_cli_version = daemon.get("cliVersion")
    app_server_version = daemon.get("appServerVersion")
    if not isinstance(daemon_cli_version, str) or not daemon_cli_version:
        raise CodexRuntimeError("daemon cliVersion is missing or invalid")
    if not isinstance(app_server_version, str) or not app_server_version:
        raise CodexRuntimeError("daemon appServerVersion is missing or invalid")

    # Establish lifecycle/process ownership before classifying any mismatch as
    # safely reloadable.  A responsive foreign socket must remain unsafe even
    # when its versions happen to look stale in an otherwise familiar way.
    launchd_pid, peer_pid = require_roundtable_daemon_owner(socket_path)

    if daemon_cli_version != rendered_cli:
        raise CodexDaemonReloadRequired(
            "selected CLI/daemon CLI version mismatch: "
            f"{rendered_cli} != {daemon_cli_version}"
        )
    if app_server_version != rendered_cli:
        raise CodexDaemonReloadRequired(
            f"CLI/app-server version mismatch: {rendered_cli} != {app_server_version}"
        )

    if distribution == "standalone":
        if managed_version != rendered_cli:
            raise CodexDaemonReloadRequired(
                "selected standalone/managed Codex version mismatch: "
                f"{rendered_cli} != {managed_version}"
            )
    return CodexDaemonIdentity(
        selected_codex=selected_codex,
        distribution=distribution,
        managed_codex_hint=managed_codex,
        managed_codex_version=managed_version,
        launchd_pid=launchd_pid,
        peer_pid=peer_pid,
    )


def minimum_release_text() -> str:
    return ".".join(str(part) for part in MINIMUM_CODEX_RELEASE)


def require_supported_version() -> tuple[int, int, int]:
    """Return the selected CLI release, failing closed below the floor.

    Version acceptance is a floor plus parse check only; a release at or above
    the floor is proven by the live protocol probe run against its daemon, not
    by a per-version allowlist.
    """
    version, output = codex_version()
    if version is None:
        raise CodexRuntimeError(f"could not parse Codex version: {output}")
    if version < MINIMUM_CODEX_RELEASE:
        rendered = ".".join(str(part) for part in version)
        raise UnsupportedVersion(
            f"Codex {rendered} is below the minimum supported app-server "
            f"release {minimum_release_text()}"
        )
    return version


def require_validated_version() -> tuple[int, int, int]:
    # Compatibility alias for older pneu_packaging artifacts that call
    # this name through _codex_context.  It now applies the same floor+parse
    # check as require_supported_version instead of an exact-set allowlist, so
    # a mixed-version upgrade accepts any release at or above the floor.
    return require_supported_version()


def codex_protocol_probe(
    socket_path: Path = DEFAULT_SOCKET, timeout: float = 3.0
) -> tuple[bool, str]:
    """Exercise the wake bridge's read-only app-server surface once.

    The probe covers the initialize/initialized handshake plus the exact
    read-only bridge methods ``thread/loaded/list`` and ``hooks/list`` with
    their expected result shapes.  It deliberately does not touch
    ``thread/read``, ``thread/resume``, ``thread/turns/list``, or
    ``turn/start``: those are thread-bound or side-effectful and stay covered
    by the bridge's per-wake fail-closed paths.  A passing probe is protocol
    evidence for launch, not a validated support claim.
    """
    client = None
    method = "initialize"
    try:
        client = AppServerClient(socket_path, timeout=timeout)
        method = "thread/loaded/list"
        page = client.request(method, {})
        data = page.get("data")
        if not isinstance(data, list) or not all(
            isinstance(item, str) for item in data
        ):
            raise CodexRuntimeError("data is not a list of thread IDs")
        cursor = page.get("nextCursor")
        if cursor is not None and not isinstance(cursor, str):
            raise CodexRuntimeError(f"nextCursor is invalid: {cursor!r}")
        method = "hooks/list"
        result = client.request(method, {"cwds": [str(Path.home())]})
        if not isinstance(result.get("data"), list):
            raise CodexRuntimeError("data is not a list of hook entries")
    except Exception as error:
        return False, f"{method} probe failed: {error}"
    finally:
        if client is not None:
            client.close()
    return True, "read-only protocol probe passed"


def require_supported_daemon(socket_path: Path = DEFAULT_SOCKET) -> dict:
    """Fail closed when CLI and default-socket app-server versions diverge."""
    require_default_socket(socket_path)
    cli = require_supported_version()
    daemon, detail = daemon_version(socket_path)
    if not daemon or daemon.get("status") != "running":
        raise CodexRuntimeError(f"could not validate app-server version: {detail}")
    require_daemon_identity(daemon, socket_path, cli)
    ok, probe_detail = codex_protocol_probe(socket_path)
    if not ok:
        rendered = ".".join(str(part) for part in cli)
        raise UnsupportedVersion(
            f"Codex {rendered} failed the app-server protocol probe: "
            f"{probe_detail}"
        )
    return daemon


def pid_is_running(pid_path: Path, expected_fragment: str) -> tuple[bool, str]:
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        return False, f"missing or invalid pid file {pid_path}"
    try:
        os.kill(pid, 0)
    except OSError as error:
        return False, f"pid {pid} is not alive: {error}"
    proc = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or expected_fragment not in proc.stdout:
        return False, f"pid {pid} does not match {expected_fragment}"
    return True, f"pid {pid}"
