"""Host-local runtime state and fenced harness-seat leases.

Project mailboxes are durable and stay in ``<project>/.roundtable``.  This
module owns only process and adapter state that is meaningful on one host.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LEASE_SCHEMA = "roundtable.session-lease.v1"
PROJECT_SCHEMA = "roundtable.runtime-project.v1"
CODEX_LAUNCH_INTENT_SCHEMA = "roundtable.codex-launch-intent.v1"
CODEX_LAUNCH_INTENT_NAME = "codex-launch-intent.json"
REPLY_EXPECTATIONS_SCHEMA = "roundtable.reply-expectations.v1"
REPLY_EXPECTATIONS_NAME = "reply-expectations.json"
SEAT_SURFACE_SCHEMA = "roundtable.seat-surface.v1"
SEAT_CAPABILITY_SCHEMA = "roundtable.seat-capability.v1"
SEAT_CAPABILITY_NAME = "capability.json"
# Surface capability is an explicit address, never an environment snapshot.
# Only these keys may be persisted: no HOME, PATH, tokens, or full environment.
SEAT_CAPABILITY_SURFACE_FIELDS = {
    "herdr": ("pane", frozenset({"workspace", "tab", "session", "endpoint"})),
    "tmux": ("target", frozenset({"session", "endpoint"})),
}
SEAT_CAPABILITY_VALUE_MAX = 512
REPLY_DURATION_RE = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[smh])$")
DEFAULT_HEARTBEAT_TTL = 30.0
# Watchers renew more frequently than the health TTL.  Keeping the renewal
# interval at one third of the TTL leaves room for scheduler and filesystem
# jitter while preserving the invariant that a dead watcher is stale within
# one TTL.
DEFAULT_HEARTBEAT_RENEWAL_INTERVAL = DEFAULT_HEARTBEAT_TTL / 3.0
DEFAULT_CODEX_LAUNCH_THREAD_WINDOW = 300.0
BIND_REQUEST_LOCK_NAME = ".codex-bind-requests.lock"
UNCHANGED = object()


class RuntimeStateError(RuntimeError):
    """Runtime metadata is unsafe, malformed, or cannot be verified."""


class FenceRejected(RuntimeStateError):
    """A stale session or watcher attempted to mutate the current lease."""


class SeatOccupied(RuntimeStateError):
    def __init__(self, inspection: "SeatInspection"):
        super().__init__(inspection.detail)
        self.inspection = inspection


class SeatAmbiguous(RuntimeStateError):
    def __init__(self, inspection: "SeatInspection"):
        super().__init__(inspection.detail)
        self.inspection = inspection


@dataclass(frozen=True)
class SeatPaths:
    runtime_root: Path
    project_dir: Path
    project_meta: Path
    claim_lock: Path
    agents_dir: Path
    agent_dir: Path
    state_lock: Path
    lease: Path
    surface: Path
    capability: Path


@dataclass(frozen=True)
class LeaseToken:
    project_root: Path
    project_hash: str
    agent_id: str
    harness: str
    session_id: str
    revision: str
    owner_pid: int
    owner_start: str
    record: dict[str, Any]

    @property
    def watcher_pid(self) -> int | None:
        value = (self.record.get("wake") or {}).get("watcherPid")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def native_session_id(self) -> str | None:
        value = (self.record.get("wake") or {}).get("nativeSessionId")
        return value if isinstance(value, str) and value else None

    @property
    def empty_beats(self) -> int:
        value = (self.record.get("wake") or {}).get("emptyBeats", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @property
    def last_wake_messages(self) -> tuple[str, ...]:
        value = (self.record.get("wake") or {}).get("lastWakeMessages", [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            return ()
        return tuple(value)

    @property
    def wake_attempts(self) -> int:
        value = (self.record.get("wake") or {}).get("wakeAttempts", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @property
    def activity_at(self) -> str | None:
        value = self.record.get("activityAt")
        return value if isinstance(value, str) and value else None

    @property
    def activity_revision(self) -> int:
        value = self.record.get("activityRevision", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0


@dataclass(frozen=True)
class SeatInspection:
    status: str
    detail: str
    token: LeaseToken | None = None
    heartbeat_age: float | None = None
    wake_healthy: bool = False

    @property
    def adapter_healthy(self) -> bool:
        return self.wake_healthy

    @property
    def lease(self) -> LeaseToken | None:
        return self.token

    @property
    def record(self) -> dict[str, Any] | None:
        return self.token.record if self.token is not None else None


@dataclass(frozen=True)
class RuntimeReclaim:
    """Result of a guarded attempt to retire one project's runtime directory."""

    path: Path
    removed: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplyExpectation:
    """One durable sender-side expectation for a quiet peer acknowledgement."""

    msg_id: str
    peer: str
    sent_at: str
    deadline: str
    duration: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def parse_reply_duration(raw: Any) -> tuple[str, int]:
    """Parse one positive integer reply-alarm duration."""

    if not isinstance(raw, str):
        raise RuntimeStateError(
            "reply deadline must be a positive integer duration such as 90s, 30m, or 2h"
        )
    rendered = raw.strip().lower()
    match = REPLY_DURATION_RE.fullmatch(rendered)
    if match is None:
        raise RuntimeStateError(
            "reply deadline must be a positive integer duration such as 90s, 30m, or 2h"
        )
    amount = int(match.group("amount"))
    unit = match.group("unit")
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return rendered, amount * multiplier


def _absolute_runtime_path(value: Path | str, label: str) -> Path:
    selected = Path(value).expanduser()
    if not selected.is_absolute():
        raise RuntimeStateError(
            f"{label} must resolve to an absolute path, got {str(value)!r}"
        )
    # Normalize lexical aliases without following a leaf symlink; the runtime
    # directory validator must still be able to reject that symlink.
    return Path(os.path.normpath(str(selected)))


def runtime_root() -> Path:
    generic = os.environ.get("RT_RUNTIME_DIR")
    legacy = os.environ.get("RT_CODEX_RUNTIME_DIR")
    generic_path = (
        _absolute_runtime_path(generic, "RT_RUNTIME_DIR") if generic else None
    )
    legacy_path = (
        _absolute_runtime_path(legacy, "RT_CODEX_RUNTIME_DIR") if legacy else None
    )
    if (
        generic_path is not None
        and legacy_path is not None
        and generic_path != legacy_path
    ):
        raise RuntimeStateError(
            "RT_RUNTIME_DIR and RT_CODEX_RUNTIME_DIR select different runtime "
            f"roots: {generic_path} != {legacy_path}"
        )
    if generic_path is not None or legacy_path is not None:
        return generic_path or legacy_path
    # Mirror _rtlib._default_state_root: a host with a real pre-rename state
    # root keeps it (the symlink-hardened checks refuse a linked root), so
    # legacy installs stay on ~/.roundtable and fresh hosts use ~/.pneu.
    legacy = Path.home() / ".roundtable"
    preferred = Path.home() / ".pneu"
    if (preferred / "projects.yaml").exists():
        return preferred / ".runtime"
    if (
        legacy.is_dir()
        and not legacy.is_symlink()
        and (legacy / "projects.yaml").exists()
    ):
        return legacy / ".runtime"
    return preferred / ".runtime"


def canonical_project(project: Path | str) -> Path:
    try:
        # Runtime identity is meaningful only for an existing project.  Use
        # strict resolution so symlink loops fail closed on Python 3.13+ as
        # they did on earlier supported interpreters.
        return Path(project).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise RuntimeStateError(f"cannot resolve project root {project}: {error}") from error


def project_hash(project: Path | str) -> str:
    root = canonical_project(project)
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _agent_key(agent_id: str) -> str:
    if not isinstance(agent_id, str) or not agent_id or "\0" in agent_id:
        raise RuntimeStateError("agent_id must be a non-empty string without NUL")
    return hashlib.sha256(agent_id.encode("utf-8")).hexdigest()


def _validate_harness(harness: str) -> str:
    if not isinstance(harness, str) or not harness or "\0" in harness:
        raise RuntimeStateError("harness must be a non-empty string without NUL")
    return harness


def seat_paths(
    project: Path | str,
    agent_id: str,
    *,
    root: Path | None = None,
) -> SeatPaths:
    canonical = canonical_project(project)
    digest = project_hash(canonical)
    base = (
        _absolute_runtime_path(root, "runtime root override")
        if root is not None
        else runtime_root()
    )
    project_dir = base / "projects" / digest
    agents_dir = project_dir / "agents"
    agent_dir = agents_dir / _agent_key(agent_id)
    return SeatPaths(
        runtime_root=base,
        project_dir=project_dir,
        project_meta=project_dir / "project.json",
        claim_lock=project_dir / "claim.lock",
        agents_dir=agents_dir,
        agent_dir=agent_dir,
        state_lock=agent_dir / "state.lock",
        lease=agent_dir / "lease.json",
        surface=agent_dir / "surface.json",
        capability=agent_dir / SEAT_CAPABILITY_NAME,
    )


def _path_info(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeStateError(f"cannot inspect runtime path {path}: {error}") from error


def _ensure_private_dir(path: Path) -> None:
    info = _path_info(path)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise RuntimeStateError(f"runtime directory is a symlink: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = path.lstat()
    except OSError as error:
        raise RuntimeStateError(f"cannot create runtime directory {path}: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeStateError(f"runtime path is not a directory: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeStateError(
            f"runtime directory owner uid {info.st_uid} != {os.getuid()}: {path}"
        )
    try:
        os.chmod(path, 0o700)
    except OSError as error:
        raise RuntimeStateError(f"cannot protect runtime directory {path}: {error}") from error


def _validate_read_path(path: Path, *, directory: bool) -> None:
    info = _path_info(path)
    if info is None:
        return
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeStateError(f"runtime path is a symlink: {path}")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise RuntimeStateError(f"runtime path is not a directory: {path}")
    if not directory and not stat.S_ISREG(info.st_mode):
        raise RuntimeStateError(f"runtime path is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise RuntimeStateError(
            f"runtime path owner uid {info.st_uid} != {os.getuid()}: {path}"
        )
    if info.st_mode & 0o077:
        raise RuntimeStateError(f"runtime path exposes group/other permissions: {path}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path: Path, *, shared: bool = False):
    _ensure_private_dir(path.parent)
    info = _path_info(path)
    if info is not None and stat.S_ISLNK(info.st_mode):
        raise RuntimeStateError(f"runtime lock is a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = None
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeStateError(f"runtime lock is not a regular file: {path}")
        if opened.st_uid != os.getuid():
            raise RuntimeStateError(
                f"runtime lock owner uid {opened.st_uid} != {os.getuid()}: {path}"
            )
        os.fchmod(descriptor, 0o600)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise RuntimeStateError(f"cannot open runtime lock {path}: {error}") from error
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    handle = os.fdopen(descriptor, "r+")
    try:
        fcntl.flock(
            handle.fileno(),
            fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
        )
        yield handle
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@contextmanager
def bind_request_guard(root: Path | str | None = None):
    """Serialize Codex bind-request publication with conditional consumption.

    The hook and wake bridge are separate processes.  They must share this
    lock so a request cannot be replaced between the bridge's inode check and
    unlink.  The lock lives outside the request queue and is never removed.
    """
    selected = runtime_root() if root is None else _absolute_runtime_path(
        root,
        "bind request runtime root",
    )
    with _locked(selected / BIND_REQUEST_LOCK_NAME):
        yield


@contextmanager
def _existing_shared_lock(path: Path):
    """Take a read lock without creating or chmodding runtime state."""

    _validate_read_path(path, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise RuntimeStateError(f"cannot open runtime lock {path}: {error}") from error
    handle = os.fdopen(descriptor, "r")
    try:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or opened.st_mode & 0o077
        ):
            raise RuntimeStateError(f"runtime lock changed while opening: {path}")
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        yield handle
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _read_json(path: Path) -> dict[str, Any] | None:
    _validate_read_path(path, directory=False)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeStateError(f"cannot read runtime JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeStateError(f"runtime JSON is not an object: {path}")
    return value


def _write_project_meta(paths: SeatPaths, project: Path) -> None:
    expected = {
        "schema": PROJECT_SCHEMA,
        "projectRoot": str(project),
        "projectHash": project_hash(project),
    }
    current = _read_json(paths.project_meta)
    if current is None:
        _atomic_json(paths.project_meta, expected)
        return
    if current != expected:
        raise RuntimeStateError(
            f"runtime project metadata mismatch at {paths.project_meta}"
        )


def _validate_project_meta(paths: SeatPaths, project: Path) -> None:
    current = _read_json(paths.project_meta)
    if current is None:
        if paths.project_dir.exists():
            raise RuntimeStateError(
                f"runtime project metadata is missing: {paths.project_meta}"
            )
        return
    expected = {
        "schema": PROJECT_SCHEMA,
        "projectRoot": str(project),
        "projectHash": project_hash(project),
    }
    if current != expected:
        raise RuntimeStateError(
            f"runtime project metadata mismatch at {paths.project_meta}"
        )


def process_start_fingerprint(pid: int) -> str | None:
    """Return a stable process-birth fingerprint, or ``None`` if unavailable."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    proc_stat = Path("/proc") / str(pid) / "stat"
    try:
        payload = proc_stat.read_text()
        # comm can contain spaces and parentheses. Everything after the last
        # ')' starts at field 3; starttime is field 22.
        suffix = payload.rsplit(")", 1)[1].strip().split()
        if len(suffix) >= 20:
            return f"proc:{suffix[19]}"
    except (OSError, IndexError):
        pass
    environment = dict(os.environ)
    # BSD ps renders lstart according to the caller's locale. The launcher,
    # hooks, doctor, and launchd bridge can legitimately have different locale
    # environments, so force one representation before persisting it.
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    try:
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            env=environment,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else None


def _pid_state(pid: int) -> str:
    try:
        os.kill(pid, 0)
        return "live"
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "ambiguous"
    except (OSError, ValueError, OverflowError):
        return "dead"


def _owner_liveness(record: dict[str, Any]) -> tuple[str, str]:
    pid = record.get("ownerPid")
    expected = record.get("ownerStart")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(expected, str)
        or not expected
    ):
        return "ambiguous", "owner PID or process-start fingerprint is invalid"
    state = _pid_state(pid)
    if state == "dead":
        return "stale", f"owner pid {pid} is not running"
    if state == "ambiguous":
        return "ambiguous", f"owner pid {pid} cannot be inspected"
    observed = process_start_fingerprint(pid)
    if observed is None:
        return "ambiguous", f"owner pid {pid} process start is unavailable"
    if observed != expected:
        return "stale", f"owner pid {pid} was reused"
    return "active", f"owner pid {pid} is running"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _reply_expectations_path(paths: SeatPaths) -> Path:
    return paths.agent_dir / REPLY_EXPECTATIONS_NAME


def _validate_reply_expectation(
    value: Any,
    *,
    path: Path,
    index: int,
) -> ReplyExpectation:
    if not isinstance(value, dict):
        raise RuntimeStateError(
            f"reply expectation {index} in {path} is not an object"
        )
    msg_id = value.get("msg_id")
    peer = value.get("peer")
    sent_at = value.get("sent_at")
    deadline = value.get("deadline")
    duration = value.get("duration")
    if (
        not isinstance(msg_id, str)
        or not msg_id
        or "\x00" in msg_id
        or not isinstance(peer, str)
        or not peer
        or "\x00" in peer
        or not isinstance(sent_at, str)
        or not isinstance(deadline, str)
        or not isinstance(duration, str)
    ):
        raise RuntimeStateError(
            f"reply expectation {index} in {path} has invalid fields"
        )
    normalized_duration, _seconds = parse_reply_duration(duration)
    sent = _parse_time(sent_at)
    due = _parse_time(deadline)
    if sent is None or due is None or due <= sent:
        raise RuntimeStateError(
            f"reply expectation {index} in {path} has invalid timestamps"
        )
    return ReplyExpectation(
        msg_id=msg_id,
        peer=peer,
        sent_at=sent_at,
        deadline=deadline,
        duration=normalized_duration,
    )


def _read_reply_expectations(paths: SeatPaths) -> list[ReplyExpectation]:
    path = _reply_expectations_path(paths)
    payload = _read_json(path)
    if payload is None:
        return []
    if payload.get("schema") != REPLY_EXPECTATIONS_SCHEMA:
        raise RuntimeStateError(
            f"invalid reply expectation schema at {path}: {payload.get('schema')!r}"
        )
    values = payload.get("expectations")
    if not isinstance(values, list):
        raise RuntimeStateError(f"reply expectations is not a list: {path}")
    result = [
        _validate_reply_expectation(value, path=path, index=index)
        for index, value in enumerate(values)
    ]
    ids = [item.msg_id for item in result]
    if len(set(ids)) != len(ids):
        raise RuntimeStateError(f"reply expectations contain duplicate msg_id: {path}")
    return result


def _reply_expectation_record(item: ReplyExpectation) -> dict[str, str]:
    return {
        "msg_id": item.msg_id,
        "peer": item.peer,
        "sent_at": item.sent_at,
        "deadline": item.deadline,
        "duration": item.duration,
    }


def _write_reply_expectations(paths: SeatPaths, values: list[ReplyExpectation]) -> None:
    path = _reply_expectations_path(paths)
    if not values:
        info = _path_info(path)
        if info is None:
            return
        _validate_read_path(path, directory=False)
        try:
            path.unlink()
        except OSError as error:
            raise RuntimeStateError(
                f"cannot clear reply expectations at {path}: {error}"
            ) from error
        return
    _atomic_json(
        path,
        {
            "schema": REPLY_EXPECTATIONS_SCHEMA,
            "expectations": [_reply_expectation_record(item) for item in values],
        },
    )


def _validate_record(
    record: dict[str, Any],
    project: Path,
    agent_id: str | None = None,
) -> None:
    if record.get("schema") != LEASE_SCHEMA:
        raise RuntimeStateError(
            f"invalid lease schema {record.get('schema')!r}, expected {LEASE_SCHEMA!r}"
        )
    if record.get("projectRoot") != str(project):
        raise RuntimeStateError(
            f"lease project {record.get('projectRoot')!r} != {str(project)!r}"
        )
    if record.get("projectHash") != project_hash(project):
        raise RuntimeStateError("lease project hash does not match canonical root")
    if agent_id is not None and record.get("agentId") != agent_id:
        raise RuntimeStateError(
            f"lease agent {record.get('agentId')!r} != {agent_id!r}"
        )
    for name in ("agentId", "harness", "sessionId", "revision", "claimedAt"):
        if not isinstance(record.get(name), str) or not record.get(name):
            raise RuntimeStateError(f"lease field {name} is missing or invalid")
    owner_pid = record.get("ownerPid")
    if (
        not isinstance(owner_pid, int)
        or isinstance(owner_pid, bool)
        or owner_pid <= 0
        or owner_pid > 2**31 - 1
    ):
        raise RuntimeStateError("lease field ownerPid is missing or invalid")
    if not isinstance(record.get("ownerStart"), str) or not record.get("ownerStart"):
        raise RuntimeStateError("lease field ownerStart is missing or invalid")
    activity_revision = record.get("activityRevision", 0)
    if (
        not isinstance(activity_revision, int)
        or isinstance(activity_revision, bool)
        or activity_revision < 0
    ):
        raise RuntimeStateError("lease field activityRevision is invalid")
    activity_at = record.get("activityAt")
    if activity_at is not None and (
        not isinstance(activity_at, str) or not activity_at
    ):
        raise RuntimeStateError("lease field activityAt is invalid")
    wake = record.get("wake", {})
    if not isinstance(wake, dict):
        raise RuntimeStateError("lease wake state is not an object")
    last_wake_messages = wake.get("lastWakeMessages", [])
    if not isinstance(last_wake_messages, list) or any(
        not isinstance(item, str) or not item for item in last_wake_messages
    ):
        raise RuntimeStateError("lease wake.lastWakeMessages is invalid")
    wake_attempts = wake.get("wakeAttempts", 0)
    if (
        not isinstance(wake_attempts, int)
        or isinstance(wake_attempts, bool)
        or wake_attempts < 0
    ):
        raise RuntimeStateError("lease wake.wakeAttempts is invalid")


def _token(record: dict[str, Any]) -> LeaseToken:
    return LeaseToken(
        project_root=Path(record["projectRoot"]),
        project_hash=record["projectHash"],
        agent_id=record["agentId"],
        harness=record["harness"],
        session_id=record["sessionId"],
        revision=str(record["revision"]),
        owner_pid=record["ownerPid"],
        owner_start=record["ownerStart"],
        record=json.loads(json.dumps(record)),
    )


def _inspection_from_record(
    record: dict[str, Any],
    project: Path,
    *,
    heartbeat_ttl: float,
) -> SeatInspection:
    _validate_record(record, project)
    token = _token(record)
    liveness, detail = _owner_liveness(record)
    if liveness == "stale":
        return SeatInspection("stale", detail, token)
    if liveness == "ambiguous":
        return SeatInspection("ambiguous", detail, token)

    wake = record.get("wake") or {}
    heartbeat = _parse_time(wake.get("heartbeatAt"))
    if heartbeat is None:
        return SeatInspection(
            "active_unhealthy",
            f"{detail}; wake adapter has no heartbeat",
            token,
        )
    age = max(0.0, (datetime.now(timezone.utc) - heartbeat).total_seconds())
    if age > heartbeat_ttl:
        return SeatInspection(
            "active_unhealthy",
            f"{detail}; wake heartbeat is stale ({age:.1f}s > {heartbeat_ttl:.1f}s)",
            token,
            heartbeat_age=age,
        )
    watcher_pid = wake.get("watcherPid")
    if watcher_pid is not None:
        if (
            not isinstance(watcher_pid, int)
            or isinstance(watcher_pid, bool)
            or watcher_pid <= 0
        ):
            return SeatInspection(
                "active_unhealthy",
                f"{detail}; wake watcher PID is invalid",
                token,
                heartbeat_age=age,
            )
        watcher_state = _pid_state(watcher_pid)
        if watcher_state != "live":
            return SeatInspection(
                "active_unhealthy",
                f"{detail}; wake watcher pid {watcher_pid} is not verifiably live",
                token,
                heartbeat_age=age,
            )
    return SeatInspection(
        "active_healthy",
        f"{detail}; wake heartbeat age={age:.1f}s",
        token,
        heartbeat_age=age,
        wake_healthy=True,
    )


def inspect_seat(
    project: Path | str,
    agent_id: str,
    heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL,
) -> SeatInspection:
    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    try:
        _validate_read_path(paths.runtime_root, directory=True)
        _validate_read_path(paths.project_dir, directory=True)
        _validate_read_path(paths.agents_dir, directory=True)
        _validate_read_path(paths.agent_dir, directory=True)
        if _path_info(paths.lease) is None:
            if paths.project_dir.exists():
                _validate_project_meta(paths, canonical)
            return SeatInspection("vacant", "no current seat lease")
        _validate_project_meta(paths, canonical)
        record = _read_json(paths.lease)
        if record is None:
            return SeatInspection("vacant", "no current seat lease")
        _validate_record(record, canonical, agent_id)
        return _inspection_from_record(
            record,
            canonical,
            heartbeat_ttl=float(heartbeat_ttl),
        )
    except RuntimeStateError as error:
        return SeatInspection("ambiguous", str(error))


def _read_agent_records(paths: SeatPaths, project: Path) -> list[dict[str, Any]]:
    if not paths.agents_dir.exists():
        return []
    _validate_read_path(paths.agents_dir, directory=True)
    records = []
    try:
        directories = sorted(paths.agents_dir.iterdir())
    except OSError as error:
        raise RuntimeStateError(
            f"cannot list runtime agents in {paths.agents_dir}: {error}"
        ) from error
    for directory in directories:
        _validate_read_path(directory, directory=True)
        lease_path = directory / "lease.json"
        if _path_info(lease_path) is None:
            continue
        record = _read_json(lease_path)
        if record is None:
            continue
        _validate_record(record, project)
        records.append(record)
    return records


def harness_lease_records(
    project: Path | str,
    harness: str,
    *,
    claim_lock_held: bool = False,
) -> list[dict[str, Any]]:
    """Return validated lease records for one harness without creating state.

    ``claim_lock_held`` is for callers performing a larger guarded ownership
    transition. It avoids reopening the same project lock while preserving the
    normal shared-lock behavior for diagnostic callers.
    """
    canonical = canonical_project(project)
    selected_harness = _validate_harness(harness)
    paths = seat_paths(canonical, f"__inspect-{selected_harness}__")
    _validate_read_path(paths.runtime_root, directory=True)
    if _path_info(paths.project_dir) is None:
        return []
    _validate_read_path(paths.project_dir, directory=True)
    _validate_project_meta(paths, canonical)
    if _path_info(paths.agents_dir) is None:
        return []
    _validate_read_path(paths.agents_dir, directory=True)
    if claim_lock_held:
        records = _read_agent_records(paths, canonical)
    else:
        with _locked(paths.claim_lock, shared=True):
            records = _read_agent_records(paths, canonical)
    return [
        json.loads(json.dumps(record))
        for record in records
        if record.get("harness") == selected_harness
    ]


def inspect_host_harness_seats(
    harness: str,
    heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL,
) -> list[SeatInspection]:
    """Inspect every host-local seat for one harness without trusting a registry.

    The project registry is user-facing discovery state and can be incomplete.
    Service maintenance therefore walks the private runtime root itself.  Any
    malformed, symlinked, foreign-owned, or overexposed runtime component raises
    ``RuntimeStateError`` so callers can fail closed before disrupting a shared
    harness service.
    """

    selected_harness = _validate_harness(harness)
    root = runtime_root()
    _validate_read_path(root, directory=True)
    if _path_info(root) is None:
        return []

    projects_dir = root / "projects"
    _validate_read_path(projects_dir, directory=True)
    if _path_info(projects_dir) is None:
        return []
    try:
        project_directories = sorted(projects_dir.iterdir())
    except OSError as error:
        raise RuntimeStateError(
            f"cannot list runtime projects in {projects_dir}: {error}"
        ) from error

    inspections: list[SeatInspection] = []
    for project_dir in project_directories:
        _validate_read_path(project_dir, directory=True)
        meta_path = project_dir / "project.json"
        meta = _read_json(meta_path)
        if meta is None:
            raise RuntimeStateError(
                f"runtime project metadata is missing: {meta_path}"
            )
        project_root = meta.get("projectRoot")
        digest = meta.get("projectHash")
        if (
            meta.get("schema") != PROJECT_SCHEMA
            or not isinstance(project_root, str)
            or not project_root
            or not isinstance(digest, str)
            or not digest
        ):
            raise RuntimeStateError(
                f"runtime project metadata is invalid: {meta_path}"
            )
        if project_dir.name != digest:
            raise RuntimeStateError(
                f"runtime project metadata mismatch at {meta_path}"
            )
        canonical, _canonical_digest = _canonical_runtime_project(
            project_dir,
            project_root,
            digest,
        )

        paths = seat_paths(canonical, f"__inspect-{selected_harness}__", root=root)
        if paths.project_dir != project_dir:
            raise RuntimeStateError(
                f"runtime project directory mismatch: {project_dir}"
            )
        _validate_project_meta(paths, canonical)
        if _path_info(paths.agents_dir) is None:
            continue
        _validate_read_path(paths.agents_dir, directory=True)
        if _path_info(paths.claim_lock) is None:
            raise RuntimeStateError(
                f"runtime project claim lock is missing: {paths.claim_lock}"
            )
        with _existing_shared_lock(paths.claim_lock):
            records = _read_agent_records(paths, canonical)
        for record in records:
            if record.get("harness") == selected_harness:
                inspections.append(
                    _inspection_from_record(
                        record,
                        canonical,
                        heartbeat_ttl=float(heartbeat_ttl),
                    )
                )
    return inspections


def _validate_runtime_tree(path: Path) -> None:
    """Validate one private runtime tree before a narrowly-scoped removal."""

    _validate_read_path(path, directory=True)
    try:
        children = sorted(path.iterdir())
    except OSError as error:
        raise RuntimeStateError(f"cannot list runtime path {path}: {error}") from error
    for child in children:
        info = _path_info(child)
        if info is None:
            raise RuntimeStateError(f"runtime path disappeared while inspecting: {child}")
        if stat.S_ISDIR(info.st_mode):
            _validate_runtime_tree(child)
        else:
            _validate_read_path(child, directory=False)


def reclaim_project_runtime(project: Path | str) -> RuntimeReclaim:
    """Remove one retired project's runtime directory when every lease is stale.

    The caller must perform the higher-level project/worktree safety checks.
    This function only owns the exact hash-derived runtime directory.  A live
    or ambiguous owner is an advisory retention result; malformed or unsafe
    runtime state raises so callers fail closed instead of guessing.
    """

    canonical = canonical_project(project)
    paths = seat_paths(canonical, "__runtime-reclaim__")
    _validate_read_path(paths.runtime_root, directory=True)
    if _path_info(paths.runtime_root) is None:
        return RuntimeReclaim(paths.project_dir, False)
    projects_dir = paths.runtime_root / "projects"
    _validate_read_path(projects_dir, directory=True)
    if _path_info(projects_dir) is None or _path_info(paths.project_dir) is None:
        return RuntimeReclaim(paths.project_dir, False)

    _validate_runtime_tree(paths.project_dir)
    _validate_project_meta(paths, canonical)
    _validate_read_path(paths.claim_lock, directory=False)

    # Claim/reclaim serialization prevents a new launcher from publishing a
    # replacement lease while the exact runtime directory is being retired.
    with _locked(paths.claim_lock):
        _validate_runtime_tree(paths.project_dir)
        _validate_project_meta(paths, canonical)
        records = _read_agent_records(paths, canonical)
        blockers = []
        for record in records:
            liveness, detail = _owner_liveness(record)
            if liveness != "stale":
                blockers.append(
                    f"agent={record.get('agentId', '<invalid>')} "
                    f"harness={record.get('harness', '<invalid>')} {detail}"
                )
        if blockers:
            return RuntimeReclaim(
                paths.project_dir,
                False,
                tuple(sorted(blockers)),
            )
        try:
            shutil.rmtree(paths.project_dir)
        except OSError as error:
            raise RuntimeStateError(
                f"cannot reclaim retired runtime directory {paths.project_dir}: {error}"
            ) from error
    return RuntimeReclaim(paths.project_dir, True)


def _runtime_project_meta(path: Path) -> tuple[str, str]:
    meta_path = path / "project.json"
    meta = _read_json(meta_path)
    if meta is None:
        raise RuntimeStateError(f"runtime project metadata is missing: {meta_path}")
    project_root = meta.get("projectRoot")
    digest = meta.get("projectHash")
    if (
        meta.get("schema") != PROJECT_SCHEMA
        or not isinstance(project_root, str)
        or not project_root
        or not Path(project_root).is_absolute()
        or "\0" in project_root
        or not isinstance(digest, str)
        or not digest
    ):
        raise RuntimeStateError(f"runtime project metadata is invalid: {meta_path}")
    expected_digest = hashlib.sha256(project_root.encode("utf-8")).hexdigest()
    if digest != expected_digest:
        raise RuntimeStateError(f"runtime project metadata hash mismatch: {meta_path}")
    return project_root, digest


def _runtime_residue_cleanup(project_dir: Path, project_root: str) -> str:
    projects_dir = project_dir.parent
    return (
        f"after closing any seat launched from {project_root} and verifying no "
        "live or ambiguous owner remains, move this exact directory out of "
        f"{projects_dir} into a backup: {project_dir}; then rerun rt-doctor"
    )


def _canonical_runtime_project(
    project_dir: Path,
    project_root: str,
    digest: str,
) -> tuple[Path, str]:
    canonical = canonical_project(project_root)
    canonical_digest = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()
    if str(canonical) != project_root or canonical_digest != digest:
        raise RuntimeStateError(
            f"stale migrated-project runtime residue at {project_dir}: recorded "
            f"projectRoot {project_root} now canonicalizes to {canonical}, so "
            f"recorded projectHash {digest} does not match canonical project "
            f"hash {canonical_digest}; "
            f"{_runtime_residue_cleanup(project_dir, project_root)}"
        )
    return canonical, canonical_digest


def orphaned_runtime_projects(
    registry_entries: list[dict[str, Any]],
) -> list[dict[str, str | Path]]:
    """List missing-project runtime dirs eligible for a doctor advisory.

    This is deliberately report-only.  It does not inspect or mutate leases and
    never removes anything; callers decide how to display the advisory.
    """

    root = runtime_root()
    _validate_read_path(root, directory=True)
    if _path_info(root) is None:
        return []
    projects_dir = root / "projects"
    _validate_read_path(projects_dir, directory=True)
    if _path_info(projects_dir) is None:
        return []
    try:
        project_dirs = sorted(projects_dir.iterdir())
    except OSError as error:
        raise RuntimeStateError(
            f"cannot list runtime projects in {projects_dir}: {error}"
        ) from error

    entries_by_root = {
        str(entry.get("root")): entry
        for entry in registry_entries
        if isinstance(entry, dict) and entry.get("root") is not None
    }
    result: list[dict[str, str | Path]] = []
    for project_dir in project_dirs:
        _validate_runtime_tree(project_dir)
        project_root, digest = _runtime_project_meta(project_dir)
        if project_dir.name != digest:
            raise RuntimeStateError(
                f"runtime project directory name does not match metadata: {project_dir}"
            )
        candidate = Path(project_root)
        root_missing = False
        canonical: Path | None = None
        canonical_drift = False
        try:
            os.stat(candidate)
        except FileNotFoundError:
            root_missing = True
        except OSError as error:
            raise RuntimeStateError(
                f"cannot inspect runtime project root {candidate}: {error}"
            ) from error
        else:
            canonical = canonical_project(candidate)
            canonical_digest = hashlib.sha256(
                str(canonical).encode("utf-8")
            ).hexdigest()
            canonical_drift = (
                str(canonical) != project_root or canonical_digest != digest
            )
        entry = entries_by_root.get(project_root)
        if entry is None:
            registry_state = "absent"
        elif entry.get("status") == "tombstoned":
            registry_state = "tombstoned"
        else:
            registry_state = "active"
        reasons = []
        if root_missing and registry_state in {"absent", "tombstoned"}:
            reasons.append("recorded project root is missing")
        if registry_state == "tombstoned":
            reasons.append("registry entry is tombstoned")
        if canonical_drift:
            reasons.append(
                f"recorded projectRoot canonicalizes to {canonical}, whose "
                "canonical project hash differs from the recorded runtime hash"
            )
        if not reasons:
            continue
        result.append(
            {
                "runtime_dir": project_dir,
                "project_root": candidate,
                "registry_state": registry_state,
                "canonical_project_root": canonical,
                "reason": "; ".join(reasons),
                "cleanup": _runtime_residue_cleanup(project_dir, project_root),
            }
        )
    return result


def claim(
    project: Path | str,
    agent_id: str,
    harness: str,
    *,
    owner_pid: int | None = None,
    session_id: str | None = None,
) -> LeaseToken:
    canonical = canonical_project(project)
    _agent_key(agent_id)
    _validate_harness(harness)
    pid = owner_pid if owner_pid is not None else os.getpid()
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id
    ):
        raise RuntimeStateError("session_id must be a non-empty string when provided")
    owner_start = process_start_fingerprint(pid)
    if _pid_state(pid) != "live" or owner_start is None:
        raise RuntimeStateError(
            f"cannot establish owner process identity for pid {pid}"
        )
    paths = seat_paths(canonical, agent_id)
    _ensure_private_dir(paths.runtime_root)
    _ensure_private_dir(paths.runtime_root / "projects")
    _ensure_private_dir(paths.project_dir)
    _ensure_private_dir(paths.agents_dir)
    _ensure_private_dir(paths.agent_dir)
    with _locked(paths.claim_lock):
        _write_project_meta(paths, canonical)
        for record in _read_agent_records(paths, canonical):
            inspection = _inspection_from_record(
                record,
                canonical,
                heartbeat_ttl=DEFAULT_HEARTBEAT_TTL,
            )
            same_agent = record["agentId"] == agent_id
            same_harness = record["harness"] == harness
            if not (same_agent or same_harness):
                continue
            if inspection.status in {"active_healthy", "active_unhealthy"}:
                raise SeatOccupied(inspection)
            if inspection.status == "ambiguous":
                raise SeatAmbiguous(inspection)

        with _locked(paths.state_lock):
            # Re-read the target after taking its mutation lock. Project claim
            # serialization prevents another launcher, while this lock fences
            # concurrent hook/heartbeat writes from the old lease.
            existing = _read_json(paths.lease)
            if existing is not None:
                _validate_record(existing, canonical, agent_id)
                inspection = _inspection_from_record(
                    existing,
                    canonical,
                    heartbeat_ttl=DEFAULT_HEARTBEAT_TTL,
                )
                if inspection.status in {"active_healthy", "active_unhealthy"}:
                    raise SeatOccupied(inspection)
                if inspection.status == "ambiguous":
                    raise SeatAmbiguous(inspection)
            record = {
                "schema": LEASE_SCHEMA,
                "projectRoot": str(canonical),
                "projectHash": project_hash(canonical),
                "agentId": agent_id,
                "harness": harness,
                "sessionId": session_id or uuid.uuid4().hex,
                "revision": uuid.uuid4().hex,
                "ownerPid": pid,
                "ownerStart": owner_start,
                "claimedAt": utc_now(),
                "activityAt": None,
                "activityRevision": 0,
                "wake": {},
            }
            _atomic_json(paths.lease, record)
            return _token(record)


def record_seat_surface(
    project: Path | str,
    agent_id: str,
    harness: str,
    surface: dict[str, str],
    *,
    session_id: Any,
    revision: Any,
) -> Path:
    """Record one advisory terminal location beside a seat's fenced state.

    Surface placement is not ownership or liveness evidence. The worktree-open
    caller records it only after the terminal backend accepts the command and
    the harness launcher has claimed an active seat lease.
    """

    canonical = canonical_project(project)
    _agent_key(agent_id)
    selected_harness = _validate_harness(harness)
    if not isinstance(surface, dict):
        raise RuntimeStateError("seat surface must be an object")
    kind = surface.get("kind")
    if kind == "herdr":
        reference_name = "pane"
    elif kind == "tmux":
        reference_name = "target"
    else:
        raise RuntimeStateError(
            f"seat surface kind must be 'herdr' or 'tmux', got {kind!r}"
        )
    reference = surface.get(reference_name)
    if not isinstance(reference, str) or not reference or "\0" in reference:
        raise RuntimeStateError(
            f"seat surface {kind}.{reference_name} must be a non-empty string "
            "without NUL"
        )
    if set(surface) != {"kind", reference_name}:
        raise RuntimeStateError(
            f"seat surface {kind} contains unsupported fields"
        )

    paths = seat_paths(canonical, agent_id)
    _ensure_private_dir(paths.runtime_root)
    _ensure_private_dir(paths.runtime_root / "projects")
    _ensure_private_dir(paths.project_dir)
    _ensure_private_dir(paths.agents_dir)
    _ensure_private_dir(paths.agent_dir)
    with _locked(paths.claim_lock):
        _write_project_meta(paths, canonical)
        with _locked(paths.state_lock):
            lease = _load_fenced_record(
                paths,
                canonical,
                agent_id,
                session_id,
                revision,
            )
            if lease.get("harness") != selected_harness:
                raise FenceRejected(
                    f"seat harness changed for {agent_id!r} in {canonical}"
                )
            payload = {
                "schema": SEAT_SURFACE_SCHEMA,
                "projectRoot": str(canonical),
                "projectHash": project_hash(canonical),
                "agentId": agent_id,
                "harness": selected_harness,
                "recordedAt": utc_now(),
                "surface": dict(surface),
            }
            _atomic_json(paths.surface, payload)
    return paths.surface


def _capability_value(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or len(value) > SEAT_CAPABILITY_VALUE_MAX
    ):
        raise RuntimeStateError(
            f"seat capability {label} must be a non-empty string without NUL "
            f"of at most {SEAT_CAPABILITY_VALUE_MAX} characters"
        )
    return value


def validate_capability_surface(surface: Any) -> dict[str, str]:
    """Validate one minimal, explicitly addressable surface capability.

    The daemon never fabricates ``HERDR_ENV``; a surface consumer addresses the
    recorded pane or endpoint explicitly.  Only the allowlisted identifiers
    below may be stored, so no environment, ``HOME``, ``PATH``, or token can
    reach this record.
    """

    if not isinstance(surface, dict):
        raise RuntimeStateError("seat capability surface must be an object")
    kind = surface.get("kind")
    if kind not in SEAT_CAPABILITY_SURFACE_FIELDS:
        expected = " | ".join(sorted(SEAT_CAPABILITY_SURFACE_FIELDS))
        raise RuntimeStateError(
            f"seat capability surface kind must be one of {expected}, got {kind!r}"
        )
    required, optional = SEAT_CAPABILITY_SURFACE_FIELDS[kind]
    unsupported = set(surface) - {"kind", required} - set(optional)
    if unsupported:
        rendered = ", ".join(sorted(unsupported))
        raise RuntimeStateError(
            f"seat capability surface {kind} contains unsupported fields: {rendered}"
        )
    validated = {
        "kind": kind,
        required: _capability_value(surface.get(required), f"{kind}.{required}"),
    }
    for name in sorted(optional):
        if name in surface:
            validated[name] = _capability_value(surface[name], f"{kind}.{name}")
    return validated


def _validate_seat_capability(
    payload: Any,
    project: Path,
    agent_id: str,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeStateError("seat capability record is not an object")
    if payload.get("schema") != SEAT_CAPABILITY_SCHEMA:
        raise RuntimeStateError("seat capability schema is invalid")
    if payload.get("projectRoot") != str(project):
        raise RuntimeStateError("seat capability project root does not match")
    if payload.get("projectHash") != project_hash(project):
        raise RuntimeStateError("seat capability project hash does not match")
    if payload.get("agentId") != agent_id:
        raise RuntimeStateError("seat capability agent identity does not match")
    for name in ("harness", "roundtableSessionId", "leaseRevision"):
        _capability_value(payload.get(name), name)
    for name in ("threadId", "bindingRevision"):
        value = payload.get(name)
        if value is not None:
            _capability_value(value, name)
    surface = payload.get("surface")
    if surface is not None:
        payload["surface"] = validate_capability_surface(surface)
    return payload


def read_seat_capability(
    project: Path | str,
    agent_id: str,
) -> dict[str, Any] | None:
    """Read one seat-capability record without validating the live fence."""

    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    if _path_info(paths.capability) is None:
        return None
    with _locked(paths.state_lock, shared=True):
        payload = _read_json(paths.capability)
    if payload is None:
        raise RuntimeStateError(
            f"seat capability record is unreadable for {agent_id!r} in {canonical}"
        )
    return _validate_seat_capability(payload, canonical, agent_id)


def record_seat_capability(
    project: Path | str,
    agent_id: str,
    harness: str,
    *,
    session_id: Any,
    revision: Any,
    surface: Any = UNCHANGED,
    thread_id: Any = UNCHANGED,
    binding_revision: Any = UNCHANGED,
    claim_lock_held: bool = False,
) -> Path:
    """Associate a seat's native thread and surface capability with its lease.

    The record is private, keyed to the seat's runtime directory, and carries
    the whole association tuple (``threadId`` + ``bindingRevision`` +
    ``roundtableSessionId`` + ``leaseRevision``).  ``surface.json`` remains the
    advisory navigation artifact; this record is the one a capability resolver
    revalidates against the current binding and lease at every use.

    ``claim_lock_held`` is for callers already inside ``seat_shared_guard``:
    that guard holds the project claim lock shared, so re-acquiring it
    exclusively here would deadlock the same process against itself.
    """

    canonical = canonical_project(project)
    _agent_key(agent_id)
    selected_harness = _validate_harness(harness)
    paths = seat_paths(canonical, agent_id)
    _ensure_private_dir(paths.runtime_root)
    _ensure_private_dir(paths.runtime_root / "projects")
    _ensure_private_dir(paths.project_dir)
    _ensure_private_dir(paths.agents_dir)
    _ensure_private_dir(paths.agent_dir)
    with nullcontext() if claim_lock_held else _locked(paths.claim_lock):
        if not claim_lock_held:
            _write_project_meta(paths, canonical)
        with _locked(paths.state_lock):
            lease = _load_fenced_record(
                paths,
                canonical,
                agent_id,
                session_id,
                revision,
            )
            if lease.get("harness") != selected_harness:
                raise FenceRejected(
                    f"seat harness changed for {agent_id!r} in {canonical}"
                )
            existing = _read_json(paths.capability)
            previous: dict[str, Any] = {}
            if existing is not None:
                try:
                    candidate = _validate_seat_capability(
                        existing,
                        canonical,
                        agent_id,
                    )
                except RuntimeStateError:
                    candidate = None
                # A record written under an older lease generation carries no
                # authority for this one; start clean instead of inheriting a
                # thread or pane the new seat never claimed.
                if candidate is not None and (
                    candidate.get("roundtableSessionId") == lease.get("sessionId")
                    and str(candidate.get("leaseRevision"))
                    == str(lease.get("revision"))
                ):
                    previous = candidate
            payload = {
                "schema": SEAT_CAPABILITY_SCHEMA,
                "projectRoot": str(canonical),
                "projectHash": project_hash(canonical),
                "agentId": agent_id,
                "harness": selected_harness,
                "roundtableSessionId": lease["sessionId"],
                "leaseRevision": str(lease["revision"]),
                "threadId": previous.get("threadId"),
                "bindingRevision": previous.get("bindingRevision"),
                "surface": previous.get("surface"),
                "recordedAt": utc_now(),
            }
            if thread_id is not UNCHANGED:
                payload["threadId"] = (
                    None
                    if thread_id is None
                    else _capability_value(thread_id, "threadId")
                )
            if binding_revision is not UNCHANGED:
                payload["bindingRevision"] = (
                    None
                    if binding_revision is None
                    else _capability_value(binding_revision, "bindingRevision")
                )
            if surface is not UNCHANGED:
                payload["surface"] = (
                    None if surface is None else validate_capability_surface(surface)
                )
            _atomic_json(paths.capability, payload)
    return paths.capability


def clear_seat_capability(
    project: Path | str,
    agent_id: str,
    *,
    claim_lock_held: bool = False,
) -> bool:
    """Remove a seat-capability record whose lease is gone or superseded."""

    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    with nullcontext() if claim_lock_held else _locked(paths.claim_lock):
        with _locked(paths.state_lock):
            if _path_info(paths.capability) is None:
                return False
            _validate_read_path(paths.capability, directory=False)
            try:
                paths.capability.unlink()
            except FileNotFoundError:
                return False
            return True


def arm_codex_launch_intent(token: LeaseToken) -> Path:
    """Publish the fenced Codex lease that the next root SessionStart may use.

    Remote Codex hooks execute in the long-lived app-server environment, not
    in the launcher client's tool-shell environment.  The launcher therefore
    records its freshly claimed lease in private host runtime state.  A hook
    can later map its documented ``cwd`` and native session id to this intent
    without trusting inherited ``RT_SESSION_ID`` variables.
    """

    if token.harness != "codex":
        raise RuntimeStateError(
            f"cannot arm Codex launch intent for harness {token.harness!r}"
        )
    canonical = canonical_project(token.project_root)
    paths = seat_paths(canonical, token.agent_id)
    intent_path = paths.project_dir / CODEX_LAUNCH_INTENT_NAME
    with _locked(paths.claim_lock):
        with _locked(paths.state_lock, shared=True):
            current = _token(
                _load_fenced_record(
                    paths,
                    canonical,
                    token.agent_id,
                    token.session_id,
                    token.revision,
                )
            )
        payload = {
            "schema": CODEX_LAUNCH_INTENT_SCHEMA,
            "projectRoot": str(canonical),
            "projectHash": project_hash(canonical),
            "agentId": current.agent_id,
            "roundtableSessionId": current.session_id,
            "leaseRevision": current.revision,
            "armedAt": utc_now(),
            "activeNativeSessionId": None,
            "lastSessionStartAt": None,
        }
        _atomic_json(intent_path, payload)
    return intent_path


def _validate_codex_launch_intent(
    payload: dict[str, Any],
    project: Path,
) -> tuple[str, str, str, str | None, datetime]:
    if payload.get("schema") != CODEX_LAUNCH_INTENT_SCHEMA:
        raise RuntimeStateError("Codex launch intent schema is invalid")
    if payload.get("projectRoot") != str(project):
        raise RuntimeStateError("Codex launch intent project root does not match")
    if payload.get("projectHash") != project_hash(project):
        raise RuntimeStateError("Codex launch intent project hash does not match")
    values = []
    for name in ("agentId", "roundtableSessionId", "leaseRevision"):
        value = payload.get(name)
        if not isinstance(value, str) or not value or "\0" in value:
            raise RuntimeStateError(f"Codex launch intent field {name} is invalid")
        values.append(value)
    active = payload.get("activeNativeSessionId")
    if active is not None and (
        not isinstance(active, str) or not active or "\0" in active
    ):
        raise RuntimeStateError(
            "Codex launch intent field activeNativeSessionId is invalid"
        )
    armed_at = _parse_time(payload.get("armedAt"))
    if armed_at is None:
        raise RuntimeStateError("Codex launch intent field armedAt is invalid")
    return values[0], values[1], values[2], active, armed_at


def _codex_thread_created_at(
    native_session_id: str,
) -> tuple[str, datetime | None]:
    """Classify a Codex thread ID and decode a UUIDv7 creation time."""

    try:
        parsed = uuid.UUID(native_session_id)
    except (ValueError, AttributeError):
        return "not_uuid", None
    if parsed.version != 7:
        return "not_v7", None
    milliseconds = parsed.int >> 80
    try:
        return (
            "v7",
            datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc),
        )
    except (OSError, OverflowError, ValueError):
        return "v7_invalid_time", None


def resolve_codex_launch_intent(
    project: Path | str,
    native_session_id: str,
    source: str,
    *,
    initial_thread_window: float = DEFAULT_CODEX_LAUNCH_THREAD_WINDOW,
    rejection: dict[str, Any] | None = None,
) -> LeaseToken | None:
    """Resolve and advance the current Codex launch intent, or fail closed.

    Codex dispatches the first root ``startup``/``resume`` event on the first
    turn, which may happen long after launch.  Codex-generated fresh thread IDs
    are UUIDv7, so a ``startup`` creation time must fall inside the launch
    window; the hook's arrival time is only a compatibility fallback for
    non-UUID test or legacy IDs.  A historical ``resume`` may predate that
    window and is accepted under the exact current live lease fence.  Once
    claimed, later events must name the same native session, except ``clear``
    which is the documented lifecycle event allowed to advance the native
    thread for the same fenced Roundtable lease.  Project claim locking makes
    two competing SessionStart hooks linearizable.

    ``rejection`` optionally receives a stable reason for an expected no-op.
    Malformed or ambiguous state still raises instead of being reduced to a
    reason code.
    """

    def _reject(reason: str) -> None:
        if rejection is not None:
            rejection.clear()
            rejection["reason"] = reason
        return None

    if (
        not isinstance(native_session_id, str)
        or not native_session_id
        or "\0" in native_session_id
    ):
        raise RuntimeStateError("native Codex session ID is invalid")
    if source not in {"startup", "resume", "clear"}:
        raise RuntimeStateError(f"unsupported Codex SessionStart source: {source!r}")
    try:
        thread_window = float(initial_thread_window)
    except (TypeError, ValueError) as error:
        raise RuntimeStateError(
            "Codex launch thread window is invalid"
        ) from error
    if not math.isfinite(thread_window) or thread_window <= 0:
        raise RuntimeStateError(
            "Codex launch thread window must be positive and finite"
        )

    canonical = canonical_project(project)
    lookup = seat_paths(canonical, "__codex-launch-intent__")
    intent_path = lookup.project_dir / CODEX_LAUNCH_INTENT_NAME
    if _path_info(intent_path) is None:
        return _reject("intent_missing")
    _validate_read_path(lookup.runtime_root, directory=True)
    _validate_read_path(lookup.project_dir, directory=True)
    _validate_project_meta(lookup, canonical)
    with _locked(lookup.claim_lock):
        payload = _read_json(intent_path)
        if payload is None:
            return _reject("intent_missing")
        agent_id, session_id, revision, active, armed_at = (
            _validate_codex_launch_intent(payload, canonical)
        )
        paths = seat_paths(canonical, agent_id)
        if paths.project_dir != lookup.project_dir:
            raise RuntimeStateError("Codex launch intent runtime path mismatch")
        try:
            with _locked(paths.state_lock, shared=True):
                current = _token(
                    _load_fenced_record(
                        paths,
                        canonical,
                        agent_id,
                        session_id,
                        revision,
                    )
                )
        except FenceRejected as error:
            # A launcher that died before exec leaves a harmless stale intent.
            # An unrelated native Codex session in the same cwd remains a no-op.
            if rejection is not None:
                rejection.clear()
                rejection.update(
                    reason="lease_fence_rejected",
                    detail=str(error),
                )
            return None
        if current.harness != "codex":
            raise RuntimeStateError(
                f"Codex launch intent points to harness {current.harness!r}"
            )

        if active is None:
            if source not in {"startup", "resume"}:
                return _reject("unclaimed_source")
            now = datetime.now(timezone.utc)
            intent_age = (now - armed_at).total_seconds()
            if intent_age < -5.0:
                return _reject("intent_armed_in_future")
            thread_id_kind, thread_created_at = _codex_thread_created_at(
                native_session_id
            )
            if thread_id_kind == "v7_invalid_time":
                return _reject("native_session_time_invalid")
            if thread_id_kind == "not_uuid":
                if intent_age > thread_window:
                    return _reject("native_session_time_unavailable")
            elif thread_id_kind == "not_v7":
                if source == "startup":
                    return _reject("startup_session_not_v7")
            elif thread_created_at is not None:
                launch_delta = (thread_created_at - armed_at).total_seconds()
                if source == "startup" and (
                    launch_delta < -5.0 or launch_delta > thread_window
                ):
                    return _reject("native_session_outside_launch_window")
                if source == "resume" and launch_delta > thread_window:
                    return _reject("native_session_outside_launch_window")
        elif source != "clear" and active != native_session_id:
            # A nested or unrelated root thread cannot steal an established
            # launch intent merely because it shares the project cwd.
            return _reject("active_native_session_mismatch")

        if active != native_session_id:
            payload["activeNativeSessionId"] = native_session_id
        payload["lastSessionStartAt"] = utc_now()
        _atomic_json(intent_path, payload)
        return current


def release_codex_launch_intent_thread(
    project: Path | str,
    native_session_id: str,
) -> bool:
    """Un-claim a launch intent taken by a thread that can never be a seat.

    A ``/btw`` side child runs its own SessionStart in the parent's cwd, so it
    can reach an unclaimed intent first.  Once the bridge proves that thread is
    ephemeral or forked, holding the claim would strand the launch: the real
    root thread would be refused as a mismatch forever.  Only the exact named
    thread is released, and only by a caller that has proven it unbindable.
    """

    if (
        not isinstance(native_session_id, str)
        or not native_session_id
        or "\0" in native_session_id
    ):
        raise RuntimeStateError("native Codex session ID is invalid")
    canonical = canonical_project(project)
    lookup = seat_paths(canonical, "__codex-launch-intent__")
    intent_path = lookup.project_dir / CODEX_LAUNCH_INTENT_NAME
    if _path_info(intent_path) is None:
        return False
    with _locked(lookup.claim_lock):
        payload = _read_json(intent_path)
        if payload is None:
            return False
        _agent_id, _session, _revision, active, _armed_at = (
            _validate_codex_launch_intent(payload, canonical)
        )
        if active != native_session_id:
            return False
        payload["activeNativeSessionId"] = None
        payload["releasedAt"] = utc_now()
        _atomic_json(intent_path, payload)
        return True


def clear_codex_launch_intent_if_stale(
    project: Path | str,
    *,
    lock_held: bool = False,
) -> bool:
    """Remove a Codex launch intent only after proving its lease is stale.

    The handoff command uses this while holding ``seat_claim_guard``.  An
    intent tied to a live or ambiguous seat is never removed; a missing lease
    is stale, and a stale lease is removable only when its complete fence
    matches the intent.  This keeps cleanup from becoming an alternate way to
    steal a live launch claim.
    """

    canonical = canonical_project(project)
    lookup = seat_paths(canonical, "__codex-launch-intent__")
    intent_path = lookup.project_dir / CODEX_LAUNCH_INTENT_NAME

    def clear() -> bool:
        if _path_info(intent_path) is None:
            return False
        payload = _read_json(intent_path)
        if payload is None:
            return False
        agent_id, session_id, revision, _active, _armed_at = (
            _validate_codex_launch_intent(payload, canonical)
        )
        inspection = inspect_seat(canonical, agent_id)
        if inspection.status in {"active_healthy", "active_unhealthy"}:
            raise RuntimeStateError(
                f"Codex launch intent still belongs to an active seat for {canonical}"
            )
        if inspection.status == "ambiguous":
            raise RuntimeStateError(
                f"Codex launch intent seat state is ambiguous for {canonical}: "
                f"{inspection.detail}"
            )
        if inspection.status == "stale":
            token = inspection.token
            if (
                token is None
                or token.agent_id != agent_id
                or token.session_id != session_id
                or str(token.revision) != str(revision)
            ):
                raise RuntimeStateError(
                    "Codex launch intent does not match the stale seat fence"
                )
        elif inspection.status != "vacant":
            raise RuntimeStateError(
                f"cannot classify Codex launch intent state for {canonical}: "
                f"{inspection.status}"
            )
        _validate_read_path(intent_path, directory=False)
        intent_path.unlink()
        return True

    if lock_held:
        return clear()
    with seat_claim_guard(canonical):
        return clear()


def _normalize_fence(session_id: Any, revision: Any) -> tuple[str, str]:
    if not isinstance(session_id, str) or not session_id:
        raise FenceRejected("session ID is missing")
    rendered_revision = str(revision) if revision is not None else ""
    if not rendered_revision:
        raise FenceRejected("lease revision is missing")
    return session_id, rendered_revision


def _load_fenced_record(
    paths: SeatPaths,
    project: Path,
    agent_id: str,
    session_id: Any,
    revision: Any,
) -> dict[str, Any]:
    expected_session, expected_revision = _normalize_fence(session_id, revision)
    record = _read_json(paths.lease)
    if record is None:
        raise FenceRejected(f"no current lease for {agent_id!r} in {project}")
    _validate_record(record, project, agent_id)
    if (
        record.get("sessionId") != expected_session
        or str(record.get("revision")) != expected_revision
    ):
        raise FenceRejected(
            f"seat lease changed for {agent_id!r} in {project}"
        )
    liveness, detail = _owner_liveness(record)
    if liveness == "stale":
        raise FenceRejected(detail)
    if liveness == "ambiguous":
        raise RuntimeStateError(detail)
    return record


def load_validated_lease(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
) -> LeaseToken:
    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    _validate_project_meta(paths, canonical)
    with _locked(paths.state_lock, shared=True):
        return _token(
            _load_fenced_record(
                paths,
                canonical,
                agent_id,
                session_id,
                revision,
            )
        )


def list_reply_expectations(
    project: Path | str,
    agent_id: str,
) -> tuple[ReplyExpectation, ...]:
    """Read one seat's durable reply expectations without mutating them."""

    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    _validate_read_path(paths.agent_dir, directory=True)
    if _path_info(paths.agent_dir) is None:
        return ()
    with _locked(paths.state_lock, shared=True):
        return tuple(_read_reply_expectations(paths))


def has_reply_expectations(project: Path | str, agent_id: str) -> bool:
    """Lock-free probe for pending reply alarms on one seat.

    ``_write_reply_expectations`` unlinks the file whenever the pending set
    becomes empty, so a missing file is authoritative absence.  Any other
    stat outcome errs toward True: the fenced reconcile path stays the only
    deciding read, and this probe may never suppress a real alarm.
    """

    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    try:
        return _reply_expectations_path(paths).lstat().st_size > 0
    except FileNotFoundError:
        return False
    except OSError:
        return True


def add_reply_expectation(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
    *,
    msg_id: str,
    peer: str,
    duration: str,
    sent_at: str | None = None,
) -> ReplyExpectation:
    """Persist one sender-side alarm under the current fenced seat."""

    canonical = canonical_project(project)
    normalized_duration, seconds = parse_reply_duration(duration)
    if (
        not isinstance(msg_id, str)
        or not msg_id
        or "\x00" in msg_id
        or not isinstance(peer, str)
        or not peer
        or "\x00" in peer
    ):
        raise RuntimeStateError("reply expectation msg_id and peer must be non-empty strings")
    selected_sent_at = sent_at or utc_now()
    sent = _parse_time(selected_sent_at)
    if sent is None:
        raise RuntimeStateError("reply expectation sent_at is invalid")
    deadline = (
        sent + timedelta(seconds=seconds)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    item = ReplyExpectation(
        msg_id=msg_id,
        peer=peer,
        sent_at=selected_sent_at,
        deadline=deadline,
        duration=normalized_duration,
    )
    paths = seat_paths(canonical, agent_id)
    with _locked(paths.state_lock):
        _load_fenced_record(paths, canonical, agent_id, session_id, revision)
        current = _read_reply_expectations(paths)
        if any(existing.msg_id == msg_id for existing in current):
            raise RuntimeStateError(
                f"reply expectation already exists for msg_id {msg_id}"
            )
        _write_reply_expectations(paths, [*current, item])
    return item


def reconcile_reply_expectations(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
    acknowledged_msg_ids: set[str] | frozenset[str] = frozenset(),
    *,
    now: datetime | None = None,
) -> tuple[tuple[ReplyExpectation, ...], tuple[ReplyExpectation, ...]]:
    """Clear acknowledged alarms and atomically consume newly overdue alarms.

    The returned pairs are ``(cleared, fired)``. Fired entries are removed in
    the same fenced write that marks them consumed, so a watcher restart can
    never emit the same alarm twice.
    """

    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    acknowledged = set(acknowledged_msg_ids)
    with _locked(paths.state_lock):
        _load_fenced_record(paths, canonical, agent_id, session_id, revision)
        current = _read_reply_expectations(paths)
        cleared: list[ReplyExpectation] = []
        fired: list[ReplyExpectation] = []
        remaining: list[ReplyExpectation] = []
        for item in current:
            if item.msg_id in acknowledged:
                cleared.append(item)
                continue
            deadline = _parse_time(item.deadline)
            if deadline is not None and deadline <= current_time:
                fired.append(item)
                continue
            remaining.append(item)
        if len(remaining) != len(current):
            _write_reply_expectations(paths, remaining)
    return tuple(cleared), tuple(fired)


@contextmanager
def seat_shared_guard(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
):
    """Hold claim/reclaim serialization while using a fenced active seat.

    Claim and release take the project claim lock exclusively. Bind and wake
    operations hold it shared from their final lease validation through their
    routing-critical side effect, so a replacement lease can linearize only
    before or after that operation.
    """
    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    with _locked(paths.claim_lock, shared=True):
        yield load_validated_lease(
            canonical,
            agent_id,
            session_id,
            revision,
        )


@contextmanager
def seat_claim_guard(project: Path | str, agent_id: str = "codex"):
    """Hold the project claim lock for a guarded seat handoff.

    A handoff must inspect stale state and remove the binding/launch intent as
    one ownership transition. Claimers and fenced routing operations already
    use this lock, so keeping it exclusive prevents a fresh seat from being
    published between the safety check and the cleanup.
    """

    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    _ensure_private_dir(paths.runtime_root)
    _ensure_private_dir(paths.runtime_root / "projects")
    _ensure_private_dir(paths.project_dir)
    _ensure_private_dir(paths.agents_dir)
    with _locked(paths.claim_lock):
        _write_project_meta(paths, canonical)
        yield


@contextmanager
def legacy_harness_guard(
    project: Path | str,
    harness: str,
):
    """Fence a legacy side effect that has no session lease.

    Legacy compatibility is valid only while the project has no lease record
    for this harness.  The shared project lock is held through the caller's
    routing-critical side effect, so a unified launcher claim can linearize
    only before or after it.
    """
    canonical = canonical_project(project)
    selected_harness = _validate_harness(harness)
    paths = seat_paths(canonical, f"__legacy-{selected_harness}__")
    _ensure_private_dir(paths.runtime_root)
    _ensure_private_dir(paths.runtime_root / "projects")
    _ensure_private_dir(paths.project_dir)
    _ensure_private_dir(paths.agents_dir)

    # Initialize/validate project metadata under the same exclusive lock used
    # by claim().  Reacquiring it shared below is safe: a claim that wins the
    # gap publishes its lease before our guarded record scan.
    with _locked(paths.claim_lock):
        _write_project_meta(paths, canonical)

    with _locked(paths.claim_lock, shared=True):
        _validate_project_meta(paths, canonical)
        records = _read_agent_records(paths, canonical)
        conflicts = [
            record
            for record in records
            if record.get("harness") == selected_harness
        ]
        if conflicts:
            agents = ", ".join(
                sorted({str(record.get("agentId")) for record in conflicts})
            )
            raise RuntimeStateError(
                f"host-local {selected_harness} seat state already exists "
                f"for {canonical} (agents: {agents})"
            )
        yield


def _watcher_can_be_replaced(wake: dict[str, Any]) -> bool:
    pid = wake.get("watcherPid")
    heartbeat = _parse_time(wake.get("heartbeatAt"))
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return True
    if _pid_state(pid) != "live":
        return True
    if heartbeat is None:
        return True
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    return age > DEFAULT_HEARTBEAT_TTL


def watcher_is_live(
    token: LeaseToken,
    *,
    heartbeat_ttl: float = DEFAULT_HEARTBEAT_TTL,
) -> bool:
    """Return whether the token's fenced wake watcher is live and current."""
    wake = token.record.get("wake") or {}
    pid = wake.get("watcherPid")
    heartbeat = _parse_time(wake.get("heartbeatAt"))
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or heartbeat is None
        or _pid_state(pid) != "live"
    ):
        return False
    age = (datetime.now(timezone.utc) - heartbeat).total_seconds()
    return age <= float(heartbeat_ttl)


def update_wake(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
    *,
    watcher_pid: int | None | object = UNCHANGED,
    native_session_id: str | None | object = UNCHANGED,
    empty_beats: int | object = UNCHANGED,
    last_wake_messages: list[str] | tuple[str, ...] | object = UNCHANGED,
    wake_attempts: int | object = UNCHANGED,
    expected_watcher_pid: int | None = None,
) -> LeaseToken:
    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    with _locked(paths.state_lock):
        record = _load_fenced_record(
            paths, canonical, agent_id, session_id, revision
        )
        wake = dict(record.get("wake") or {})
        current_watcher = wake.get("watcherPid")
        if expected_watcher_pid is not None and current_watcher != expected_watcher_pid:
            raise FenceRejected(
                f"wake watcher changed for {agent_id!r} in {canonical}"
            )
        if watcher_pid is not UNCHANGED:
            if (
                watcher_pid is not None
                and (
                    not isinstance(watcher_pid, int)
                    or isinstance(watcher_pid, bool)
                    or watcher_pid <= 0
                )
            ):
                raise RuntimeStateError("watcher_pid must be a positive integer or None")
            if (
                watcher_pid is not None
                and current_watcher not in {None, watcher_pid}
                and not _watcher_can_be_replaced(wake)
            ):
                raise FenceRejected(
                    f"another live watcher pid {current_watcher} owns the wake slot"
                )
            if watcher_pid is None:
                wake.pop("watcherPid", None)
            else:
                wake["watcherPid"] = watcher_pid
        if native_session_id is not UNCHANGED:
            if native_session_id is None:
                wake.pop("nativeSessionId", None)
            elif isinstance(native_session_id, str) and native_session_id:
                wake["nativeSessionId"] = native_session_id
            else:
                raise RuntimeStateError(
                    "native_session_id must be a non-empty string or None"
                )
        if empty_beats is not UNCHANGED:
            if (
                not isinstance(empty_beats, int)
                or isinstance(empty_beats, bool)
                or empty_beats < 0
            ):
                raise RuntimeStateError("empty_beats must be a non-negative integer")
            wake["emptyBeats"] = empty_beats
        if last_wake_messages is not UNCHANGED:
            if not isinstance(last_wake_messages, (list, tuple)) or any(
                not isinstance(item, str) or not item
                for item in last_wake_messages
            ):
                raise RuntimeStateError(
                    "last_wake_messages must contain non-empty strings"
                )
            wake["lastWakeMessages"] = list(last_wake_messages)
        if wake_attempts is not UNCHANGED:
            if (
                not isinstance(wake_attempts, int)
                or isinstance(wake_attempts, bool)
                or wake_attempts < 0
            ):
                raise RuntimeStateError(
                    "wake_attempts must be a non-negative integer"
                )
            wake["wakeAttempts"] = wake_attempts
        wake["heartbeatAt"] = utc_now()
        record["wake"] = wake
        _atomic_json(paths.lease, record)
        return _token(record)


def clear_wake(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
    *,
    expected_watcher_pid: int | None = None,
) -> LeaseToken:
    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    with _locked(paths.state_lock):
        record = _load_fenced_record(
            paths, canonical, agent_id, session_id, revision
        )
        wake = dict(record.get("wake") or {})
        current_watcher = wake.get("watcherPid")
        if expected_watcher_pid is not None and current_watcher != expected_watcher_pid:
            raise FenceRejected(
                f"wake watcher changed for {agent_id!r} in {canonical}"
            )
        wake.pop("watcherPid", None)
        wake.pop("heartbeatAt", None)
        record["wake"] = wake
        _atomic_json(paths.lease, record)
        return _token(record)


def update_activity(
    project: Path | str,
    agent_id: str,
    session_id: Any,
    revision: Any,
) -> LeaseToken:
    canonical = canonical_project(project)
    paths = seat_paths(canonical, agent_id)
    with _locked(paths.state_lock):
        record = _load_fenced_record(
            paths, canonical, agent_id, session_id, revision
        )
        current = record.get("activityRevision", 0)
        if not isinstance(current, int) or isinstance(current, bool) or current < 0:
            raise RuntimeStateError("lease activity revision is invalid")
        record["activityRevision"] = current + 1
        record["activityAt"] = utc_now()
        _atomic_json(paths.lease, record)
        return _token(record)


def release(token: LeaseToken) -> bool:
    paths = seat_paths(token.project_root, token.agent_id)
    with _locked(paths.claim_lock):
        with _locked(paths.state_lock):
            try:
                _load_fenced_record(
                    paths,
                    token.project_root,
                    token.agent_id,
                    token.session_id,
                    token.revision,
                )
            except FenceRejected:
                return False
            try:
                paths.lease.unlink()
            except FileNotFoundError:
                return False
            # Capability is a property of the released lease generation, so it
            # can never outlive it.  Unlink inline: both guards are already
            # held here, and re-entering them would deadlock this process.
            if _path_info(paths.capability) is not None:
                try:
                    _validate_read_path(paths.capability, directory=False)
                    paths.capability.unlink()
                except (FileNotFoundError, RuntimeStateError):
                    pass
            return True
