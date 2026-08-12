"""Per-project Claude Remote Control host ownership and diagnostics."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from _rtlib import (
    ProjectRegistryError,
    active_project_entries,
    load_agents_doc,
    load_project_registry_strict,
    projects_registry_path,
    resolve_project_mailbox_checked,
)


SCHEMA = "roundtable.rc-host.v1"
REGISTRATION_ENV = "RT_RC_HOST_PROJECT_UUID"
LABEL_PREFIX = "com.roundtable.rc-host"
CREATE_TIMEOUT_SECONDS = 120
REMOVE_TIMEOUT_SECONDS = 120
MAX_STATE_BYTES = 256 * 1024
PID_RE = re.compile(r"(?m)^\s*pid\s*=\s*(\d+)\s*$")
AGENT_ID_RE = re.compile(r"^[a-z0-9#_-]+$")


class RCHostError(RuntimeError):
    """A per-project Remote Control host operation cannot proceed safely."""


@dataclass(frozen=True)
class RCHostStatus:
    project: Path
    project_uuid: str
    label: str
    enabled: bool
    configured: bool
    loaded: bool
    pid: int | None
    process_alive: bool
    last_registration: dict[str, Any] | None
    detail: str

    @property
    def healthy(self) -> bool:
        return self.enabled and self.configured and self.loaded and self.process_alive


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _state_root() -> Path:
    return projects_registry_path().parent


def rc_hosts_dir() -> Path:
    return _state_root() / "rc-hosts"


def launch_agents_dir(home: Path | None = None) -> Path:
    configured = os.environ.get("RT_LAUNCH_AGENTS_DIR")
    if configured:
        return Path(configured).expanduser().absolute()
    return (home or Path.home()).expanduser().absolute() / "Library" / "LaunchAgents"


def _launchctl() -> str:
    configured = os.environ.get("RT_LAUNCHCTL", "/bin/launchctl")
    if os.path.isabs(configured):
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    else:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    raise RCHostError(f"launchctl is not executable: {configured}")


def _launch_domain() -> str:
    return f"gui/{os.getuid()}"


def _canonical_project(project: Path | str) -> tuple[Path, str, dict[str, Any]]:
    try:
        mailbox = resolve_project_mailbox_checked(Path(project))
        entries, _warnings = load_project_registry_strict()
    except ProjectRegistryError as error:
        raise RCHostError(f"project is not an active registered pneu project: {error}") from error
    matches = [
        entry
        for entry in active_project_entries(entries, available_only=True)
        if entry.get("uuid") == mailbox.project_uuid
        and Path(entry.get("root", "")).expanduser().resolve() == mailbox.project_root
    ]
    if len(matches) != 1:
        raise RCHostError(
            f"cannot identify one active registry row for {mailbox.project_root}"
        )
    return mailbox.project_root, mailbox.project_uuid, matches[0]


def state_path(project_uuid: str) -> Path:
    if not isinstance(project_uuid, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        project_uuid,
    ):
        raise RCHostError(f"invalid project UUID for rc-host state: {project_uuid!r}")
    return rc_hosts_dir() / f"{project_uuid}.json"


def label_for(project_uuid: str) -> str:
    state_path(project_uuid)
    return f"{LABEL_PREFIX}.{project_uuid.replace('-', '')}"


def _path_info(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RCHostError(f"cannot inspect {path}: {error}") from error


def _validate_regular(path: Path, *, missing_ok: bool = False) -> os.stat_result | None:
    info = _path_info(path)
    if info is None:
        if missing_ok:
            return None
        raise RCHostError(f"required file is missing: {path}")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RCHostError(f"refusing non-regular or symlinked file: {path}")
    if info.st_uid != os.getuid():
        raise RCHostError(f"file is not owned by the current user: {path}")
    return info


def _ensure_directory(path: Path, *, mode: int = 0o700) -> bool:
    info = _path_info(path)
    if info is not None:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RCHostError(f"refusing non-directory or symlinked path: {path}")
        if info.st_uid != os.getuid():
            raise RCHostError(f"directory is not owned by the current user: {path}")
        return False
    parent = path.parent
    if parent != path and not parent.exists():
        _ensure_directory(parent, mode=mode)
    path.mkdir(mode=mode)
    return True


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
    _validate_regular(path, missing_ok=True)
    _ensure_directory(path.parent, mode=0o700 if path.parent == rc_hosts_dir() else 0o755)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise RCHostError(f"cannot atomically write {path}: {error}") from error


def _read_json(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    info = _validate_regular(path, missing_ok=missing_ok)
    if info is None:
        return None
    if info.st_size > MAX_STATE_BYTES:
        raise RCHostError(f"JSON file is too large: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RCHostError(f"cannot parse JSON file {path}: {error}") from error
    if not isinstance(value, dict):
        raise RCHostError(f"JSON file is not an object: {path}")
    return value


@contextmanager
def state_guard():
    directory = rc_hosts_dir()
    _ensure_directory(directory)
    lock = directory / ".lock"
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _configured_claude_ids(project: Path) -> list[str]:
    document = load_agents_doc(project, "rt-rc-host")
    agents = document.get("agents") or {}
    if not isinstance(agents, dict):
        raise RCHostError(f"agents is not a mapping in {project / '.roundtable/agents.yaml'}")
    result: list[str] = []
    for base, config in agents.items():
        if not isinstance(base, str) or not isinstance(config, dict):
            continue
        if config.get("harness") not in {"claude", "claude-code"}:
            continue
        instances = config.get("instances")
        if not isinstance(instances, list) or not instances:
            instances = [{"id": base}]
        for instance in instances:
            value = instance.get("id") if isinstance(instance, dict) else instance
            if not isinstance(value, str) or not AGENT_ID_RE.fullmatch(value):
                raise RCHostError(f"invalid configured Claude instance id: {value!r}")
            if value not in result:
                result.append(value)
    return result


def claude_seat(project: Path) -> str:
    ids = _configured_claude_ids(project)
    if len(ids) != 1:
        rendered = ", ".join(ids) or "none"
        raise RCHostError(
            "rc-host requires exactly one configured Claude seat; "
            f"configured: {rendered}"
        )
    return ids[0]


def workspace_trusted(project: Path, *, home: Path | None = None) -> bool:
    path = (home or Path.home()).expanduser().absolute() / ".claude.json"
    value = _read_json(path, missing_ok=True)
    if value is None:
        return False
    projects = value.get("projects")
    if not isinstance(projects, dict):
        return False
    # Claude Code honors a trust acceptance recorded on any ancestor
    # directory, so an exact-path probe alone reports false negatives for
    # projects that never received their own dialog.
    candidate = Path(project).expanduser().absolute()
    for directory in (candidate, *candidate.parents):
        record = projects.get(str(directory))
        if isinstance(record, dict) and record.get("hasTrustDialogAccepted") is True:
            return True
    return False


def trust_remedy(project: Path) -> str:
    return f"run `cd {project} && claude` once and accept the workspace trust dialog"


def _require_git_repository(project: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise RCHostError(f"cannot inspect Git repository for worktree spawn: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or "not inside a Git repository"
        raise RCHostError(f"phone worktree spawn requires Git: {detail}")


def managed_command_path(name: str) -> Path:
    configured = os.environ.get("ROUNDTABLE_INSTALL_PREFIX", "").strip()
    if configured:
        candidate = Path(configured).expanduser().absolute() / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    candidate = Path(__file__).resolve().with_name(name)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return candidate
    raise RCHostError(f"managed command is unavailable: {candidate}")


def _claude_binary() -> Path:
    from _rtlauncher import harness_bin

    try:
        return harness_bin("claude")
    except Exception as error:
        raise RCHostError(str(error)) from error


def hook_groups(command: Path) -> dict[str, dict[str, Any]]:
    base = {"type": "command", "command": str(command)}
    return {
        "WorktreeCreate": {
            "hooks": [{**base, "args": ["hook-create"], "timeout": CREATE_TIMEOUT_SECONDS}]
        },
        "WorktreeRemove": {
            "hooks": [{**base, "args": ["hook-remove"], "timeout": REMOVE_TIMEOUT_SECONDS}]
        },
    }


def _settings_path(project: Path) -> Path:
    return project / ".claude" / "settings.local.json"


def _tracked_settings(project: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "ls-files", "--error-unmatch", ".claude/settings.local.json"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError as error:
        raise RCHostError(f"cannot inspect whether local settings are tracked: {error}") from error
    return result.returncode == 0


def _prepare_settings_enable(
    project: Path,
    command: Path,
    prior: dict[str, Any] | None,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    path = _settings_path(project)
    if _tracked_settings(project):
        raise RCHostError(
            f"refusing tracked project-local settings: {path}; make it untracked first"
        )
    path_info = _validate_regular(path, missing_ok=True)
    before = _read_json(path, missing_ok=True)
    created = before is None
    current = {} if before is None else before
    after = copy.deepcopy(current)
    hooks_added = "hooks" not in after
    if "hooks" not in after:
        after["hooks"] = {}
    hooks = after.get("hooks")
    if not isinstance(hooks, dict):
        raise RCHostError(f"Claude hooks must be an object: {path}")
    expected = hook_groups(command)
    events: list[dict[str, Any]] = []
    prior_settings = prior.get("settings") if isinstance(prior, dict) else None
    for event, group in expected.items():
        event_added = event not in hooks
        if event not in hooks:
            hooks[event] = []
        groups = hooks.get(event)
        if not isinstance(groups, list):
            raise RCHostError(f"Claude hook event {event} must be a list: {path}")
        count = groups.count(group)
        if prior is None:
            if count:
                raise RCHostError(
                    f"unowned pneu rc-host {event} hook already exists in {path}"
                )
            groups.append(copy.deepcopy(group))
        elif count != 1:
            raise RCHostError(
                f"managed pneu rc-host {event} hook drifted in {path}"
            )
        recorded_event = None
        if isinstance(prior_settings, dict):
            recorded_event = next(
                (
                    item
                    for item in prior_settings.get("events", [])
                    if isinstance(item, dict) and item.get("event") == event
                ),
                None,
            )
        if isinstance(recorded_event, dict) and recorded_event.get("group") != group:
            raise RCHostError(
                f"managed pneu rc-host {event} ownership drifted in {path}"
            )
        events.append(
            {
                "event": event,
                "containerAdded": (
                    bool(recorded_event.get("containerAdded"))
                    if isinstance(recorded_event, dict)
                    else event_added
                ),
                "group": copy.deepcopy(group),
            }
        )
    ownership = {
        "path": str(path),
        "created": (
            bool(prior_settings.get("created"))
            if isinstance(prior_settings, dict)
            else created
        ),
        "hooksContainerAdded": (
            bool(prior_settings.get("hooksContainerAdded"))
            if isinstance(prior_settings, dict)
            else hooks_added
        ),
        "events": events,
        "mode": (
            int(prior_settings.get("mode"))
            if isinstance(prior_settings, dict)
            and isinstance(prior_settings.get("mode"), int)
            else (stat.S_IMODE(path_info.st_mode) if path_info is not None else 0o600)
        ),
    }
    return current, _json_bytes(after), ownership


def _prepare_settings_disable(
    state: dict[str, Any],
) -> tuple[Path, bytes | None, int]:
    settings = state.get("settings")
    if not isinstance(settings, dict):
        raise RCHostError("rc-host state has invalid settings ownership")
    path = Path(str(settings.get("path", "")))
    current = _read_json(path)
    after = copy.deepcopy(current)
    hooks = after.get("hooks")
    if not isinstance(hooks, dict):
        raise RCHostError(f"managed Claude hooks are missing from {path}")
    for item in settings.get("events", []):
        if not isinstance(item, dict):
            raise RCHostError("rc-host settings event ownership is invalid")
        event = item.get("event")
        group = item.get("group")
        groups = hooks.get(event)
        if not isinstance(event, str) or not isinstance(groups, list) or groups.count(group) != 1:
            raise RCHostError(f"managed pneu rc-host {event!r} hook drifted in {path}")
        groups.remove(group)
        if item.get("containerAdded") is True and not groups:
            hooks.pop(event)
    if settings.get("hooksContainerAdded") is True and not hooks:
        after.pop("hooks")
    if settings.get("created") is True and not after:
        return path, None, int(settings.get("mode", 0o600))
    return path, _json_bytes(after), int(settings.get("mode", 0o600))


def _plist_payload(
    project: Path,
    project_uuid: str,
    project_name: str,
    seat: str,
    claude: Path,
    command: Path,
    label: str,
    *,
    home: Path,
) -> dict[str, Any]:
    host_dir = rc_hosts_dir() / project_uuid
    prefix = os.environ.get("ROUNDTABLE_INSTALL_PREFIX", "").strip()
    # launchd starts the host with the bare system PATH, and every
    # phone-spawned session inherits it, so name-resolve the pneu commands,
    # the harness itself, and Homebrew tools explicitly.
    path_entries = [str(home / ".local" / "bin")]
    if prefix:
        path_entries.append(str(Path(prefix) / "bin"))
    path_entries.extend(
        ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    )
    environment = {
        "HOME": str(home),
        "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
        "RT_PROJECTS_FILE": str(projects_registry_path()),
        "RT_RC_HOST_PROJECT_UUID": project_uuid,
    }
    runtime = os.environ.get("RT_RUNTIME_DIR") or os.environ.get("RT_CODEX_RUNTIME_DIR")
    if runtime:
        environment["RT_RUNTIME_DIR"] = runtime
        environment["RT_CODEX_RUNTIME_DIR"] = runtime
    if prefix:
        environment["ROUNDTABLE_INSTALL_PREFIX"] = prefix
    session_name = f"{seat}@{project_name}"
    return {
        "Label": label,
        "ProgramArguments": [
            str(claude),
            "remote-control",
            "--spawn",
            "worktree",
            "--name",
            session_name,
            "--remote-control-session-name-prefix",
            session_name,
            "--create-session-in-dir",
        ],
        "WorkingDirectory": str(project),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "EnvironmentVariables": environment,
        "StandardOutPath": str(host_dir / "stdout.log"),
        "StandardErrorPath": str(host_dir / "stderr.log"),
        "AssociatedBundleIdentifiers": ["com.pneu.rc-host"],
    }


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _state_record(
    *,
    project: Path,
    project_uuid: str,
    entry: dict[str, Any],
    seat: str,
    command: Path,
    claude: Path,
    label: str,
    plist_path: Path,
    plist_payload: bytes,
    settings: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "projectRoot": str(project),
        "projectUuid": project_uuid,
        "projectName": entry.get("name"),
        "projectGroup": entry.get("group"),
        "seat": seat,
        "command": str(command),
        "claude": str(claude),
        "label": label,
        "settings": settings,
        "plist": {"path": str(plist_path), "sha256": _sha256(plist_payload)},
        "enabledAt": utc_now(),
        "lastRegistration": None,
    }


def _validate_state(value: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    if value.get("schema") != SCHEMA:
        raise RCHostError(f"invalid rc-host state schema at {path or '<memory>'}")
    for name in (
        "projectRoot",
        "projectUuid",
        "projectName",
        "projectGroup",
        "seat",
        "command",
        "claude",
        "label",
        "enabledAt",
    ):
        if not isinstance(value.get(name), str) or not value.get(name):
            raise RCHostError(f"invalid rc-host state field {name} at {path or '<memory>'}")
    if value.get("label") != label_for(value["projectUuid"]):
        raise RCHostError(f"rc-host label does not match project UUID at {path or '<memory>'}")
    if not isinstance(value.get("settings"), dict) or not isinstance(value.get("plist"), dict):
        raise RCHostError(f"invalid rc-host ownership state at {path or '<memory>'}")
    return value


def load_state_for_project(project: Path | str) -> dict[str, Any] | None:
    root, project_uuid, _entry = _canonical_project(project)
    value = _read_json(state_path(project_uuid), missing_ok=True)
    if value is None:
        return None
    _validate_state(value, state_path(project_uuid))
    if value.get("projectRoot") != str(root):
        raise RCHostError("rc-host state project path does not match the registry")
    return value


def iter_states() -> list[dict[str, Any]]:
    directory = rc_hosts_dir()
    info = _path_info(directory)
    if info is None:
        return []
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RCHostError(f"rc-host state directory is unsafe: {directory}")
    result = []
    for path in sorted(directory.glob("*.json")):
        value = _read_json(path)
        assert value is not None
        result.append(_validate_state(value, path))
    return result


def _run_launchctl(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [_launchctl(), *arguments],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise RCHostError(f"cannot run launchctl: {error}") from error


def _launchctl_inspect(label: str) -> tuple[bool, str, int | None]:
    result = _run_launchctl(["print", f"{_launch_domain()}/{label}"])
    if result.returncode == 113:
        return False, result.stderr.strip(), None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RCHostError(f"cannot inspect LaunchAgent {label}: {detail}")
    match = PID_RE.search(result.stdout)
    return True, result.stdout, int(match.group(1)) if match else None


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _configuration_matches(state: dict[str, Any]) -> tuple[bool, str]:
    try:
        project = Path(state["projectRoot"])
        command = Path(state["command"])
        _prepare_settings_enable(project, command, state)
        plist = state["plist"]
        plist_path = Path(plist.get("path", ""))
        _validate_regular(plist_path)
        payload = plist_path.read_bytes()
        if _sha256(payload) != plist.get("sha256"):
            return False, f"LaunchAgent plist drifted at {plist_path}"
        parsed = plistlib.loads(payload)
        if parsed.get("Label") != state["label"]:
            return False, f"LaunchAgent label drifted at {plist_path}"
    except (OSError, ValueError, plistlib.InvalidFileException, RCHostError) as error:
        return False, str(error)
    return True, "project hooks and LaunchAgent plist match owned state"


def status(project: Path | str) -> RCHostStatus:
    root, project_uuid, _entry = _canonical_project(project)
    label = label_for(project_uuid)
    path = state_path(project_uuid)
    value = _read_json(path, missing_ok=True)
    if value is None:
        plist = launch_agents_dir() / f"{label}.plist"
        detail = "disabled"
        if _path_info(plist) is not None:
            detail = f"disabled but an unowned/orphaned plist exists at {plist}"
        return RCHostStatus(root, project_uuid, label, False, False, False, None, False, None, detail)
    state = _validate_state(value, path)
    if state["projectRoot"] != str(root):
        raise RCHostError("rc-host state project path does not match the registry")
    configured, config_detail = _configuration_matches(state)
    try:
        loaded, _launch_output, pid = _launchctl_inspect(label)
        alive = _pid_alive(pid)
        launch_detail = (
            f"loaded pid={pid if pid is not None else 'unreported'} process={'alive' if alive else 'not-running'}"
            if loaded
            else "not loaded"
        )
    except RCHostError as error:
        loaded, pid, alive = False, None, False
        launch_detail = str(error)
    detail = f"{config_detail}; {launch_detail}"
    registration = state.get("lastRegistration")
    return RCHostStatus(
        root,
        project_uuid,
        label,
        True,
        configured,
        loaded,
        pid,
        alive,
        registration if isinstance(registration, dict) else None,
        detail,
    )


def enable(project: Path | str, *, home: Path | None = None) -> RCHostStatus:
    root, project_uuid, entry = _canonical_project(project)
    selected_home = (home or Path.home()).expanduser().absolute()
    if not workspace_trusted(root, home=selected_home):
        raise RCHostError(f"workspace trust is required; {trust_remedy(root)}")
    _require_git_repository(root)
    seat = claude_seat(root)
    command = managed_command_path("rt-rc-host")
    claude = _claude_binary()
    label = label_for(project_uuid)
    plist_path = launch_agents_dir(selected_home) / f"{label}.plist"
    with state_guard():
        path = state_path(project_uuid)
        prior = _read_json(path, missing_ok=True)
        if prior is not None:
            state = _validate_state(prior, path)
            configured, detail = _configuration_matches(state)
            if not configured:
                raise RCHostError(f"refusing to overwrite drifted rc-host state: {detail}")
            loaded, _output, _pid = _launchctl_inspect(label)
            if not loaded:
                result = _run_launchctl(["bootstrap", _launch_domain(), str(plist_path)])
                if result.returncode != 0:
                    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                    raise RCHostError(f"cannot load LaunchAgent {label}: {detail}")
            return status(root)

        loaded, _output, _pid = _launchctl_inspect(label)
        if loaded or _path_info(plist_path) is not None:
            raise RCHostError(
                f"refusing unowned existing LaunchAgent state for {label}; inspect {plist_path}"
            )
        before_settings, settings_payload, settings_ownership = _prepare_settings_enable(
            root, command, None
        )
        plist_value = _plist_payload(
            root,
            project_uuid,
            str(entry.get("name")),
            seat,
            claude,
            command,
            label,
            home=selected_home,
        )
        plist_payload = plistlib.dumps(plist_value, fmt=plistlib.FMT_XML, sort_keys=True)
        state = _state_record(
            project=root,
            project_uuid=project_uuid,
            entry=entry,
            seat=seat,
            command=command,
            claude=claude,
            label=label,
            plist_path=plist_path,
            plist_payload=plist_payload,
            settings=settings_ownership,
        )
        settings_path = _settings_path(root)
        settings_existed = _path_info(settings_path) is not None
        settings_before_payload = (
            settings_path.read_bytes() if settings_existed else None
        )
        settings_mode = int(settings_ownership["mode"])
        created_directories: list[Path] = []
        for directory in (settings_path.parent, plist_path.parent, rc_hosts_dir() / project_uuid):
            if _ensure_directory(directory, mode=0o700 if directory != settings_path.parent else 0o755):
                created_directories.append(directory)
        try:
            _atomic_write(settings_path, settings_payload, settings_mode)
            _atomic_write(plist_path, plist_payload, 0o600)
            _atomic_write(path, _json_bytes(state), 0o600)
            result = _run_launchctl(["bootstrap", _launch_domain(), str(plist_path)])
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                raise RCHostError(f"cannot load LaunchAgent {label}: {detail}")
        except Exception:
            if settings_existed:
                assert settings_before_payload is not None
                _atomic_write(settings_path, settings_before_payload, settings_mode)
            else:
                settings_path.unlink(missing_ok=True)
            plist_path.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            for directory in reversed(created_directories):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise
    return status(root)


def disable(project: Path | str) -> RCHostStatus:
    root, project_uuid, _entry = _canonical_project(project)
    label = label_for(project_uuid)
    with state_guard():
        path = state_path(project_uuid)
        value = _read_json(path, missing_ok=True)
        if value is None:
            plist_path = launch_agents_dir() / f"{label}.plist"
            if _path_info(plist_path) is not None:
                raise RCHostError(
                    f"refusing to remove unowned/orphaned plist: {plist_path}"
                )
            return status(root)
        state = _validate_state(value, path)
        if state["projectRoot"] != str(root):
            raise RCHostError("rc-host state project path does not match the registry")
        configured, detail = _configuration_matches(state)
        if not configured:
            raise RCHostError(f"refusing to remove drifted rc-host state: {detail}")
        settings_path, settings_payload, settings_mode = _prepare_settings_disable(state)
        settings_before = settings_path.read_bytes()
        settings_before_mode = stat.S_IMODE(settings_path.stat().st_mode)
        plist_path = Path(state["plist"]["path"])
        plist_before = plist_path.read_bytes()
        loaded, _output, _pid = _launchctl_inspect(label)
        if loaded:
            result = _run_launchctl(["bootout", f"{_launch_domain()}/{label}"])
            if result.returncode != 0:
                detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
                raise RCHostError(f"cannot unload LaunchAgent {label}: {detail}")
        try:
            if settings_payload is None:
                settings_path.unlink()
            else:
                _atomic_write(settings_path, settings_payload, settings_mode)
            plist_path.unlink()
            path.unlink()
        except Exception:
            _atomic_write(settings_path, settings_before, settings_before_mode)
            _atomic_write(plist_path, plist_before, 0o600)
            _atomic_write(path, _json_bytes(state), 0o600)
            if loaded:
                _run_launchctl(["bootstrap", _launch_domain(), str(plist_path)])
            raise
    return status(root)


def require_enabled(project: Path | str) -> dict[str, Any]:
    root, _project_uuid, entry = _canonical_project(project)
    state = load_state_for_project(root)
    if state is None:
        anchor_uuid = os.environ.get(REGISTRATION_ENV, "").strip()
        if anchor_uuid:
            candidate_path = state_path(anchor_uuid)
            candidate = _read_json(candidate_path, missing_ok=True)
            if candidate is not None:
                candidate = _validate_state(candidate, candidate_path)
                if candidate.get("projectGroup") != entry.get("group"):
                    raise RCHostError(
                        "enabled phone host belongs to a different repository group"
                    )
                state = candidate
    if state is None:
        raise RCHostError(
            f"the Claude phone connection is not enabled for {root}; "
            "run `pneu rc-host enable` from the project root"
        )
    configured, detail = _configuration_matches(state)
    if not configured:
        raise RCHostError(f"Claude phone connection configuration is unsafe: {detail}")
    return state


def record_registration(
    session_project: Path,
    *,
    agent: str,
    session_id: str,
) -> None:
    anchor_uuid = os.environ.get(REGISTRATION_ENV, "").strip()
    if not anchor_uuid:
        return
    session_root, _session_uuid, session_entry = _canonical_project(session_project)
    path = state_path(anchor_uuid)
    with state_guard():
        state = _read_json(path, missing_ok=True)
        if state is None:
            return
        state = _validate_state(state, path)
        if state.get("projectGroup") != session_entry.get("group"):
            raise RCHostError(
                "refusing rc-host registration from a project outside the enabled repository group"
            )
        updated = copy.deepcopy(state)
        updated["lastRegistration"] = {
            "at": utc_now(),
            "projectRoot": str(session_root),
            "agent": agent,
            "sessionId": session_id,
        }
        _atomic_write(path, _json_bytes(updated), 0o600)


def status_from_state(state: dict[str, Any]) -> RCHostStatus:
    state = _validate_state(state)
    return status(Path(state["projectRoot"]))
