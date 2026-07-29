"""
Shared library for roundtable rt-* tools.
Portable version — no hardcoded paths or agent names.
"""
import fcntl
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except Exception:
    yaml = None


LIFECYCLE_ORDINAL = {"pending": 0, "injected": 1, "submitted": 2, "accepted": 3, "acked": 4}
LEGACY_PROJECTS_SCHEMA = "roundtable.projects.v1"
PROJECTS_SCHEMA = "roundtable.projects.v2"
PROJECT_ID_SCHEMA = "roundtable.project.v1"
PROJECT_LAYOUTS = frozenset({"local", "central"})
PROJECT_STATUSES = frozenset({"active", "tombstoned"})
ORPHAN_WARNING_PREFIX = "orphan: "
LAYOUT_LOCK_TIMEOUT_SECONDS = 10.0
LAYOUT_LOCK_POLL_SECONDS = 0.05
GIT_ROUTING_ENVIRONMENT = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_EXEC_PATH",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_TEMPLATE_DIR",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
    }
)


@dataclass(frozen=True)
class ProjectMailbox:
    """One registry-pinned authoritative mailbox layout."""

    project_uuid: str
    project_root: Path
    layout: str
    state_dir: Path
    mail_root: Path
    inbox_dir: Path
    messages_dir: Path
    locks_dir: Path
    layout_lock: Path


class ProjectRegistryError(ValueError):
    """A registry or project identity cannot be used safely."""


class ProjectLayoutLockTimeout(ProjectRegistryError):
    """A healthy layout transition did not quiesce before the caller's bound."""


def _project_identity_for_layout_lock(project):
    """Read the stable UUID before consulting its mutable layout pointer."""

    raw_root = Path(project).expanduser()
    if raw_root.is_symlink():
        raise ProjectRegistryError(
            f"project root is a symbolic link: {raw_root}"
        )
    root = raw_root.resolve()
    if root in {Path.home().resolve(), Path(root.anchor)}:
        raise ProjectRegistryError(
            f"not a project (missing {project_config_path(root)}): {root}"
        )
    root_fd, state_fd = _open_project_guard(root)
    try:
        project_uuid = _read_project_identity_at(
            root, state_fd, required=True
        )
        _verify_project_guard(root, root_fd, state_fd)
        return root, project_uuid
    finally:
        _close_project_guard(root_fd, state_fd)


def _verify_layout_lock_entry(lock_dir_fd, lock_path, descriptor):
    """Prove the held lock file is still the persistent directory entry."""

    held = os.fstat(descriptor)
    try:
        visible = os.stat(
            lock_path.name,
            dir_fd=lock_dir_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ProjectRegistryError(
            f"layout lock changed during acquisition: {lock_path}: {error}"
        ) from error
    if (
        held.st_nlink == 0
        or visible.st_dev != held.st_dev
        or visible.st_ino != held.st_ino
    ):
        raise ProjectRegistryError(
            f"layout lock changed during acquisition: {lock_path}"
        )
    if (
        not stat.S_ISREG(held.st_mode)
        or held.st_uid != os.getuid()
        or stat.S_IMODE(held.st_mode) != 0o600
        or held.st_nlink != 1
        or not stat.S_ISREG(visible.st_mode)
        or visible.st_uid != held.st_uid
    ):
        raise ProjectRegistryError(
            f"invalid layout lock ownership/type/mode: {lock_path}"
        )


def _verify_layout_lock_directory(parent_fd, lock_dir_fd, lock_dir):
    """Revalidate the complete lock namespace after any contended wait."""

    _validate_owned_directory_fd(
        lock_dir_fd, lock_dir, "layout lock directory"
    )
    held = os.fstat(lock_dir_fd)
    if stat.S_IMODE(held.st_mode) != 0o700:
        raise ProjectRegistryError(
            f"layout lock directory mode must be 0700: {lock_dir}; "
            f"run chmod 700 {lock_dir}"
        )
    try:
        visible = os.stat(
            lock_dir.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ProjectRegistryError(
            f"layout lock directory changed during acquisition: "
            f"{lock_dir}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(visible.st_mode)
        or visible.st_uid != held.st_uid
        or visible.st_dev != held.st_dev
        or visible.st_ino != held.st_ino
    ):
        raise ProjectRegistryError(
            f"layout lock directory changed during acquisition: {lock_dir}"
        )


def _open_layout_lock(registry, project_uuid):
    """Open one persistent UUID lock through held, validated directories."""

    parent_fd = _open_registry_parent(registry)
    lock_dir_fd = None
    descriptor = None
    lock_dir = registry.parent / "layout-locks"
    lock_path = lock_dir / f"{project_uuid}.lock"
    directory_created = False
    try:
        try:
            os.mkdir(lock_dir.name, 0o700, dir_fd=parent_fd)
            directory_created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise ProjectRegistryError(
                f"cannot create layout lock directory {lock_dir}: {error}"
            ) from error
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            lock_dir_fd = os.open(
                lock_dir.name,
                directory_flags,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise ProjectRegistryError(
                f"cannot open layout lock directory {lock_dir}: {error}"
            ) from error
        if directory_created:
            os.fchmod(lock_dir_fd, 0o700)
        _verify_layout_lock_directory(
            parent_fd, lock_dir_fd, lock_dir
        )
        _verify_open_directory_path(
            parent_fd, registry.parent, "registry parent"
        )
        if directory_created:
            os.fsync(parent_fd)

        flags = (
            os.O_RDWR
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        file_created = False
        try:
            descriptor = os.open(
                lock_path.name,
                flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=lock_dir_fd,
            )
            file_created = True
            os.fchmod(descriptor, 0o600)
        except FileExistsError:
            try:
                descriptor = os.open(
                    lock_path.name,
                    flags,
                    dir_fd=lock_dir_fd,
                )
            except OSError as error:
                raise ProjectRegistryError(
                    f"cannot open layout lock {lock_path}: {error}"
                ) from error
        except OSError as error:
            raise ProjectRegistryError(
                f"cannot create layout lock {lock_path}: {error}"
            ) from error
        _verify_layout_lock_entry(lock_dir_fd, lock_path, descriptor)
        if file_created:
            os.fsync(lock_dir_fd)
        return parent_fd, lock_dir_fd, descriptor, lock_path
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if lock_dir_fd is not None:
            os.close(lock_dir_fd)
        os.close(parent_fd)
        raise


class _ProjectMailboxLock:
    """Context manager for one UUID-pinned shared or exclusive layout lease."""

    def __init__(
        self,
        project,
        *,
        registry_path=None,
        exclusive=False,
        timeout=LAYOUT_LOCK_TIMEOUT_SECONDS,
    ):
        if not isinstance(exclusive, bool):
            raise ProjectRegistryError(
                "layout lock exclusive flag must be a boolean"
            )
        if timeout is not None:
            if isinstance(timeout, bool) or not isinstance(
                timeout, (int, float)
            ):
                raise ProjectRegistryError(
                    "layout lock timeout must be a finite non-negative "
                    "number or None"
                )
            try:
                timeout = float(timeout)
            except OverflowError:
                raise ProjectRegistryError(
                    "layout lock timeout must be a finite non-negative "
                    "number or None"
                ) from None
            if not math.isfinite(timeout) or timeout < 0:
                raise ProjectRegistryError(
                    "layout lock timeout must be a finite non-negative "
                    "number or None"
                )
        self.project = project
        self.registry = _registry_path(registry_path)
        self.exclusive = exclusive
        self.timeout = timeout
        self._parent_fd = None
        self._lock_dir_fd = None
        self._descriptor = None
        self._lock_path = None
        self._entered = False

    def _close(self):
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                # Closing the descriptor is the authoritative release. Do not
                # strand the held directory descriptors if an explicit unlock
                # is interrupted or rejected.
                pass
            finally:
                os.close(descriptor)
        if self._lock_dir_fd is not None:
            os.close(self._lock_dir_fd)
            self._lock_dir_fd = None
        if self._parent_fd is not None:
            os.close(self._parent_fd)
            self._parent_fd = None

    def __enter__(self):
        if self._entered:
            raise ProjectRegistryError("layout lock context cannot be reused")
        self._entered = True
        root, project_uuid = _project_identity_for_layout_lock(self.project)
        (
            self._parent_fd,
            self._lock_dir_fd,
            self._descriptor,
            self._lock_path,
        ) = _open_layout_lock(self.registry, project_uuid)
        operation = (
            fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        ) | fcntl.LOCK_NB
        deadline = (
            None
            if self.timeout is None
            else time.monotonic() + float(self.timeout)
        )
        try:
            while True:
                try:
                    fcntl.flock(self._descriptor, operation)
                    break
                except BlockingIOError:
                    if deadline is not None and time.monotonic() >= deadline:
                        mode = "exclusive" if self.exclusive else "shared"
                        raise ProjectLayoutLockTimeout(
                            f"timed out waiting for {mode} layout lock "
                            f"{self._lock_path}"
                        ) from None
                    remaining = (
                        LAYOUT_LOCK_POLL_SECONDS
                        if deadline is None
                        else max(0.0, deadline - time.monotonic())
                    )
                    time.sleep(min(LAYOUT_LOCK_POLL_SECONDS, remaining))
                except InterruptedError:
                    continue
            _verify_layout_lock_entry(
                self._lock_dir_fd,
                self._lock_path,
                self._descriptor,
            )
            _verify_layout_lock_directory(
                self._parent_fd,
                self._lock_dir_fd,
                self._lock_path.parent,
            )
            _validate_owned_directory_fd(
                self._parent_fd,
                self.registry.parent,
                "registry parent",
            )
            _verify_open_directory_path(
                self._parent_fd,
                self.registry.parent,
                "registry parent",
            )
            mailbox = resolve_project_mailbox_checked(
                root, registry_path=self.registry
            )
            if mailbox.project_uuid != project_uuid:
                raise ProjectRegistryError(
                    f"project identity changed while acquiring layout lock: "
                    f"{project_uuid} became {mailbox.project_uuid}"
                )
            if mailbox.layout_lock != self._lock_path:
                raise ProjectRegistryError(
                    f"mailbox resolver returned unexpected layout lock: "
                    f"{mailbox.layout_lock} != {self._lock_path}"
                )
            return mailbox
        except Exception:
            self._close()
            raise

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self._close()
        return False


class _CliProjectMailboxLock:
    """Translate only lock-entry failures to the normal CLI diagnostic."""

    def __init__(self, guard, tool):
        self.guard = guard
        self.tool = tool

    def __enter__(self):
        try:
            return self.guard.__enter__()
        except ProjectRegistryError as error:
            raise SystemExit(
                f"{self.tool}: mailbox access failed: {error}"
            ) from None

    def __exit__(self, exc_type, exc_value, traceback):
        return self.guard.__exit__(exc_type, exc_value, traceback)


def locked_project_mailbox_checked(
    project,
    registry_path=None,
    *,
    exclusive=False,
    timeout=LAYOUT_LOCK_TIMEOUT_SECONDS,
):
    """Return a typed UUID layout-lock context for library callers."""

    return _ProjectMailboxLock(
        project,
        registry_path=registry_path,
        exclusive=exclusive,
        timeout=timeout,
    )


def locked_project_mailbox(
    project,
    registry_path=None,
    *,
    exclusive=False,
    timeout=LAYOUT_LOCK_TIMEOUT_SECONDS,
    tool="roundtable",
):
    """Return a CLI-facing UUID layout-lock context."""

    return _CliProjectMailboxLock(
        locked_project_mailbox_checked(
            project,
            registry_path=registry_path,
            exclusive=exclusive,
            timeout=timeout,
        ),
        tool,
    )


def _orphan_warning(label, root):
    return (
        f"{ORPHAN_WARNING_PREFIX}{label} missing "
        f"{project_config_path(root)}"
    )


def _structural_registry_warnings(warnings):
    return [
        warning
        for warning in warnings
        if not warning.startswith(ORPHAN_WARNING_PREFIX)
    ]


def require_fenced_seat(project, tool):
    """Validate the exact launcher lease before a pre-approved mail action."""

    from _rtruntime import RuntimeStateError, inspect_seat, load_validated_lease

    canonical = Path(project).expanduser().resolve()
    values = {
        "RT_PROJECT_ROOT": os.environ.get("RT_PROJECT_ROOT", "").strip(),
        "RT_FROM": os.environ.get("RT_FROM", "").strip().lower(),
        "RT_SESSION_ID": os.environ.get("RT_SESSION_ID", "").strip(),
        "RT_LEASE_REVISION": os.environ.get("RT_LEASE_REVISION", "").strip(),
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(
            f"{tool}: --fenced requires a Roundtable-launched seat; missing "
            + ", ".join(missing)
        )
    try:
        configured_root = Path(values["RT_PROJECT_ROOT"]).expanduser().resolve()
    except OSError as error:
        raise SystemExit(
            f"{tool}: invalid RT_PROJECT_ROOT for --fenced: {error}"
        ) from None
    if configured_root != canonical:
        raise SystemExit(
            f"{tool}: fenced project {configured_root} does not match {canonical}"
        )
    agent = values["RT_FROM"]
    try:
        token = load_validated_lease(
            canonical,
            agent,
            values["RT_SESSION_ID"],
            values["RT_LEASE_REVISION"],
        )
        inspection = inspect_seat(canonical, agent)
    except (OSError, RuntimeStateError) as error:
        raise SystemExit(f"{tool}: fenced seat validation failed: {error}") from None
    if not inspection.status.startswith("active_") or inspection.token is None:
        raise SystemExit(
            f"{tool}: fenced seat is not active: {inspection.status} "
            f"({inspection.detail})"
        )
    if (
        inspection.token.session_id != token.session_id
        or inspection.token.revision != token.revision
    ):
        raise SystemExit(f"{tool}: fenced seat changed during validation")
    return agent


def _absolute_registry_path(value):
    """Return an absolute spelling without following the registry symlink."""

    expanded = Path(value).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def projects_registry_path():
    override = os.environ.get("RT_PROJECTS_FILE")
    if override:
        return _absolute_registry_path(override)
    return _absolute_registry_path(
        Path.home() / ".roundtable" / "projects.yaml"
    )


def _registry_path(path=None):
    return _absolute_registry_path(
        projects_registry_path() if path is None else path
    )


def _empty_projects_doc():
    return {"schema": PROJECTS_SCHEMA, "projects": []}


def _validate_owned_directory_fd(descriptor, path, label):
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ProjectRegistryError(
            f"{label} has invalid type or ownership: {path}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProjectRegistryError(
            f"{label} is group/other writable: {path}"
        )


def _read_owned_regular_bytes_at(
    parent_fd,
    path,
    label,
    *,
    missing_ok=False,
):
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ProjectRegistryError(f"{label} is missing: {path}")
    except OSError as error:
        raise ProjectRegistryError(f"cannot open {label} {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ProjectRegistryError(
                f"{label} has invalid type or ownership: {path}"
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ProjectRegistryError(
                f"{label} is group/other writable: {path}"
            )
    except Exception:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read()


def _read_owned_regular_bytes(path, label, *, missing_ok=False):
    """Read one owned regular file through an owned, non-symlink parent."""

    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_fd = os.open(path.parent, parent_flags)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ProjectRegistryError(f"{label} parent is missing: {path.parent}")
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot open {label} parent {path.parent}: {error}"
        ) from error
    try:
        _validate_owned_directory_fd(parent_fd, path.parent, f"{label} parent")
        return _read_owned_regular_bytes_at(
            parent_fd, path, label, missing_ok=missing_ok
        )
    finally:
        os.close(parent_fd)


def _read_projects_snapshot(path, *, parent_fd=None):
    payload = (
        _read_owned_regular_bytes_at(
            parent_fd, path, "project registry", missing_ok=True
        )
        if parent_fd is not None
        else _read_owned_regular_bytes(
            path, "project registry", missing_ok=True
        )
    )
    if payload is None:
        return _empty_projects_doc(), None
    if yaml is None:
        raise ValueError("PyYAML is required to read projects.yaml")
    try:
        text = payload.decode("utf-8")
        loaded = yaml.safe_load(text)
    except Exception as error:
        # PyYAML's exception hierarchy is optional when yaml import failed, so
        # keep this parser boundary self-contained.
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} is not a mapping")
    schema = loaded.get("schema")
    if schema not in {LEGACY_PROJECTS_SCHEMA, PROJECTS_SCHEMA}:
        raise ValueError(
            f"{path} schema is {schema!r}, expected {PROJECTS_SCHEMA!r} "
            f"or legacy {LEGACY_PROJECTS_SCHEMA!r}"
        )
    if not isinstance(loaded.get("projects"), list):
        raise ValueError(f"{path} projects is not a list")
    return loaded, payload


def _read_projects_doc(path):
    document, _payload = _read_projects_snapshot(path)
    return document


def _validate_registry_source(path):
    """Reject substituted registry files without creating read-side state."""

    try:
        parent_info = path.parent.lstat()
    except FileNotFoundError:
        parent_info = None
    if parent_info is not None:
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(
            parent_info.st_mode
        ):
            raise ProjectRegistryError(
                f"registry parent is not a real directory: {path.parent}"
            )
        if parent_info.st_uid != os.getuid():
            raise ProjectRegistryError(
                f"registry parent is owned by uid {parent_info.st_uid}, "
                f"expected {os.getuid()}: {path.parent}"
            )
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ProjectRegistryError(
            f"registry is not a regular file: {path}"
        )
    if info.st_uid != os.getuid():
        raise ProjectRegistryError(
            f"registry is owned by uid {info.st_uid}, expected {os.getuid()}: "
            f"{path}"
        )


def _absolute_project_path(root):
    """Freeze a project spelling without following a later symlink swap."""

    return Path(os.path.abspath(os.fspath(Path(root).expanduser())))


def project_identity_path(root):
    return _absolute_project_path(root) / ".roundtable" / "project.json"


def _open_project_guard(root):
    """Hold the project root and state directory without following either."""

    root = _absolute_project_path(root)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        root_fd = os.open(root, flags)
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot open project root {root}: {error}"
        ) from error
    state_fd = None
    succeeded = False
    try:
        _validate_owned_directory_fd(root_fd, root, "project root")
        state_dir = root / ".roundtable"
        state_fd = os.open(".roundtable", flags, dir_fd=root_fd)
        _validate_owned_directory_fd(
            state_fd, state_dir, "project state"
        )
        _read_owned_regular_bytes_at(
            state_fd,
            state_dir / "agents.yaml",
            "project agent configuration",
        )
        _verify_project_guard(root, root_fd, state_fd)
        succeeded = True
        return root_fd, state_fd
    except ProjectRegistryError:
        raise
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot open project state for {root}: {error}"
        ) from error
    finally:
        if not succeeded:
            if state_fd is not None:
                os.close(state_fd)
            os.close(root_fd)


def _close_project_guard(root_fd, state_fd):
    os.close(state_fd)
    os.close(root_fd)


def _verify_project_guard(root, root_fd, state_fd):
    """Verify both held inodes are still visible at the canonical root."""

    root = Path(root)
    _verify_open_directory_path(root_fd, root, "project root")
    held_state = os.fstat(state_fd)
    try:
        visible_state = os.stat(
            ".roundtable",
            dir_fd=root_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise ProjectRegistryError(
            f"project state changed during update: "
            f"{root / '.roundtable'}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(visible_state.st_mode)
        or visible_state.st_uid != os.getuid()
        or visible_state.st_dev != held_state.st_dev
        or visible_state.st_ino != held_state.st_ino
    ):
        raise ProjectRegistryError(
            f"project state changed during update: {root / '.roundtable'}"
        )


def _same_existing_inode(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return False


def legacy_registry_backup_path(path=None):
    registry = _registry_path(path)
    return registry.with_name(f"{registry.name}.v1.backup")


def _canonical_uuid(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ProjectRegistryError(f"{label} has no UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise ProjectRegistryError(f"{label} has invalid UUID {value!r}") from error
    canonical = str(parsed)
    if value != canonical:
        raise ProjectRegistryError(
            f"{label} UUID is not canonical lowercase form: {value!r}"
        )
    return canonical


def _canonical_registered_path(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ProjectRegistryError(f"{label} has no path")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ProjectRegistryError(f"{label} path is not absolute: {value}")
    try:
        resolved = candidate.resolve()
    except OSError as error:
        raise ProjectRegistryError(f"{label} path cannot resolve: {error}") from error
    if candidate != resolved:
        raise ProjectRegistryError(
            f"{label} path is not canonical: {value} (resolves to {resolved})"
        )
    return resolved


def _entry_timestamp(entry, field, label, *, required=True):
    value = entry.get(field)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProjectRegistryError(f"{label} has no valid {field}")
    return value


def _parse_legacy_entries(document, path):
    entries = []
    warnings = [
        f"{path} uses legacy schema {LEGACY_PROJECTS_SCHEMA}; "
        "run rt-projects upgrade before mailbox operations"
    ]
    seen_roots = set()
    for index, raw in enumerate(document.get("projects") or []):
        label = f"{path}: projects[{index}]"
        try:
            if not isinstance(raw, dict):
                raise ProjectRegistryError(f"{label} is not a mapping")
            root = _canonical_registered_path(raw.get("root"), label)
            registered_at = _entry_timestamp(raw, "registered_at", label)
            key = str(root)
            if key in seen_roots:
                raise ProjectRegistryError(f"{label} duplicates {root}")
            seen_roots.add(key)
            available = is_project_root(root)
            if not available:
                warnings.append(_orphan_warning(label, root))
            entries.append(
                {
                    "uuid": None,
                    "path": root,
                    "root": root,
                    "name": root.name,
                    "group": None,
                    "layout": "local",
                    "status": "active",
                    "registered_at": registered_at,
                    "tombstoned_at": None,
                    "available": available,
                    "legacy": True,
                }
            )
        except ProjectRegistryError as error:
            warnings.append(f"{error}; skipped")
    return entries, warnings


def _parse_v2_entries(document, path):
    entries = []
    warnings = []
    seen_uuids = set()
    seen_active_roots = set()
    seen_active_inodes = {}
    for index, raw in enumerate(document.get("projects") or []):
        label = f"{path}: projects[{index}]"
        try:
            if not isinstance(raw, dict):
                raise ProjectRegistryError(f"{label} is not a mapping")
            project_uuid = _canonical_uuid(raw.get("uuid"), label)
            root = _canonical_registered_path(raw.get("path"), label)
            name = raw.get("name")
            if (
                not isinstance(name, str)
                or not name.strip()
                or "\n" in name
                or "\r" in name
            ):
                raise ProjectRegistryError(f"{label} has no valid name")
            declared_group = raw.get("group")
            if not isinstance(declared_group, str) or not declared_group.strip():
                raise ProjectRegistryError(f"{label} has no valid group")
            layout = raw.get("layout")
            if layout not in PROJECT_LAYOUTS:
                raise ProjectRegistryError(
                    f"{label} has unsupported layout {layout!r}"
                )
            status_value = raw.get("status")
            if status_value not in PROJECT_STATUSES:
                raise ProjectRegistryError(
                    f"{label} has unsupported status {status_value!r}"
                )
            registered_at = _entry_timestamp(raw, "registered_at", label)
            tombstoned_at = _entry_timestamp(
                raw,
                "tombstoned_at",
                label,
                required=status_value == "tombstoned",
            )
            if status_value == "active" and raw.get("tombstoned_at") is not None:
                raise ProjectRegistryError(
                    f"{label} active entry carries tombstoned_at"
                )
            if project_uuid in seen_uuids:
                raise ProjectRegistryError(
                    f"{label} duplicates project UUID {project_uuid}"
                )
            seen_uuids.add(project_uuid)
            root_key = str(root)
            if status_value == "active" and root_key in seen_active_roots:
                raise ProjectRegistryError(
                    f"{label} duplicates active project path {root}"
                )
            if status_value == "active":
                seen_active_roots.add(root_key)
            available = is_project_root(root)
            if status_value == "active" and available:
                try:
                    info = os.stat(root, follow_symlinks=False)
                except OSError as error:
                    raise ProjectRegistryError(
                        f"{label} cannot stat live project: {error}"
                    ) from error
                inode_key = (info.st_dev, info.st_ino)
                prior = seen_active_inodes.get(inode_key)
                if prior is not None:
                    raise ProjectRegistryError(
                        f"{label} aliases live project inode already claimed "
                        f"by {prior}"
                    )
                seen_active_inodes[inode_key] = label
            group = declared_group
            group_authoritative = False
            if status_value == "active" and available:
                derived_group, group_authoritative = _derive_project_group(
                    root, project_uuid
                )
                if group_authoritative:
                    group = derived_group
            if status_value == "active" and not available:
                warnings.append(_orphan_warning(label, root))
            entries.append(
                {
                    "uuid": project_uuid,
                    "path": root,
                    # ``root`` remains a compatibility alias for callers while
                    # the serialized v2 field is deliberately named ``path``.
                    "root": root,
                    "name": name,
                    "group": group,
                    "declared_group": declared_group,
                    "group_authoritative": group_authoritative,
                    "layout": layout,
                    "status": status_value,
                    "registered_at": registered_at,
                    "tombstoned_at": tombstoned_at,
                    "available": available,
                    "legacy": False,
                }
            )
        except ProjectRegistryError as error:
            warnings.append(f"{error}; skipped")
    return entries, warnings


def load_project_registry(path=None):
    """Return parsed entries plus isolated diagnostics without mutating disk."""

    path = _registry_path(path)
    try:
        _validate_registry_source(path)
        document = _read_projects_doc(path)
    except (ProjectRegistryError, ValueError) as error:
        return [], [str(error)]
    if document.get("schema") == LEGACY_PROJECTS_SCHEMA:
        return _parse_legacy_entries(document, path)
    return _parse_v2_entries(document, path)


def _strict_entries_from_document(document, path):
    if document.get("schema") != PROJECTS_SCHEMA:
        raise ProjectRegistryError(
            f"{path} uses legacy schema {LEGACY_PROJECTS_SCHEMA}; "
            "run rt-projects upgrade"
        )
    entries, warnings = _parse_v2_entries(document, path)
    structural = _structural_registry_warnings(warnings)
    if structural:
        raise ProjectRegistryError("; ".join(structural))
    return entries, warnings


def _strict_project_registry(path=None):
    path = _registry_path(path)
    try:
        _validate_registry_source(path)
        document = _read_projects_doc(path)
    except (ProjectRegistryError, ValueError) as error:
        raise ProjectRegistryError(str(error)) from error
    entries, warnings = _strict_entries_from_document(document, path)
    return document, entries, warnings


def load_project_registry_strict(path=None):
    """Return a complete v2 snapshot or raise instead of partially parsing."""

    _document, entries, warnings = _strict_project_registry(path)
    return entries, warnings


def active_project_entries(entries, *, available_only=True):
    return [
        entry
        for entry in entries
        if entry.get("status") == "active"
        and not entry.get("legacy")
        and (entry.get("available") or not available_only)
    ]


def validate_project_registry(path=None):
    """Fail before a mutating workflow if the existing registry is malformed."""
    path = _registry_path(path)
    try:
        _strict_project_registry(path)
    except ProjectRegistryError as error:
        raise SystemExit(f"roundtable: invalid project registry: {error}")
    return path


def emit_registry_warnings(warnings, stream=None, tool="roundtable"):
    stream = stream or sys.stderr
    for warning in warnings:
        print(f"{tool}: registry warning: {warning}", file=stream)


def registered_project_roots(path=None, *, warn=True, tool="roundtable"):
    try:
        entries, warnings = load_project_registry_strict(path)
    except ProjectRegistryError as error:
        raise SystemExit(f"{tool}: invalid project registry: {error}") from None
    if warn:
        emit_registry_warnings(warnings, tool=tool)
    return [
        entry["root"]
        for entry in active_project_entries(entries, available_only=True)
    ]


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_registry_parent(path):
    """Create, open, and validate the registry parent for one whole update."""

    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot create registry parent {path.parent}: {error}"
        ) from error
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path.parent, flags)
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot open registry parent {path.parent}: {error}"
        ) from error
    try:
        _validate_owned_directory_fd(
            descriptor, path.parent, "registry parent"
        )
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _write_projects_doc(
    path,
    document,
    *,
    parent_fd,
    expected_payload,
    guard=None,
):
    if yaml is None:
        raise ProjectRegistryError("PyYAML is required to write projects.yaml")
    content = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    tmp_name = f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    descriptor = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            tmp_name, flags, 0o600, dir_fd=parent_fd
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        observed_payload = _read_owned_regular_bytes_at(
            parent_fd,
            path,
            "project registry",
            missing_ok=True,
        )
        if observed_payload != expected_payload:
            raise ProjectRegistryError(
                f"project registry changed during update: {path}"
            )
        if guard is not None:
            guard()
        os.replace(
            tmp_name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    except ProjectRegistryError:
        raise
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot commit project registry {path}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except OSError:
            pass


def _update_project_registry(mutator, path=None, *, guard=None):
    path = _registry_path(path)
    parent_fd = _open_registry_parent(path)
    lock_path = path.with_name(f"{path.name}.lock")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        try:
            descriptor = os.open(
                lock_path.name,
                flags,
                0o600,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise ProjectRegistryError(
                f"cannot open registry lock {lock_path}: {error}"
            ) from error
        with os.fdopen(descriptor, "r+") as lock:
            info = os.fstat(lock.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                raise ProjectRegistryError(
                    f"invalid registry lock ownership/type/mode: {lock_path}"
                )
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            except OSError as error:
                raise ProjectRegistryError(
                    f"cannot lock project registry {path}: {error}"
                ) from error
            try:
                document, source_payload = _read_projects_snapshot(
                    path, parent_fd=parent_fd
                )
            except (ProjectRegistryError, ValueError) as error:
                raise ProjectRegistryError(
                    f"refusing to overwrite invalid registry: {error}"
                ) from error
            changed = mutator(document, source_payload, parent_fd)
            if changed:
                _write_projects_doc(
                    path,
                    document,
                    parent_fd=parent_fd,
                    expected_payload=source_payload,
                    guard=guard,
                )
            else:
                # A prior attempt may have completed its atomic rename and
                # crashed before syncing the directory. An idempotent retry
                # must make that already-visible registry entry durable.
                if guard is not None:
                    guard()
                try:
                    os.fsync(parent_fd)
                except OSError as error:
                    raise ProjectRegistryError(
                        f"cannot sync project registry parent "
                        f"{path.parent}: {error}"
                    ) from error
            if guard is not None:
                guard()
            return changed
    finally:
        os.close(parent_fd)


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _looks_like_git_worktree(root):
    for candidate in (Path(root), *Path(root).parents):
        try:
            (candidate / ".git").lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def _clean_git_environment():
    """Ignore caller routing overrides while preserving ordinary Git config."""

    environment = os.environ.copy()
    for name in GIT_ROUTING_ENVIRONMENT:
        environment.pop(name, None)
    environment.pop("GIT_CONFIG_COUNT", None)
    environment.pop("GIT_CONFIG_PARAMETERS", None)
    for name in tuple(environment):
        if name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    return environment


def _project_identity_git_status(root):
    """Return non-git, tracked, ignored, or unignored for project.json."""

    root = _absolute_project_path(root)
    pathspec = ".roundtable/project.json"
    environment = _clean_git_environment()
    try:
        probe = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        if _looks_like_git_worktree(root):
            raise ProjectRegistryError(
                f"cannot verify Git protection for {project_identity_path(root)}: "
                f"{error}"
            ) from error
        return "non-git"
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        if _looks_like_git_worktree(root):
            raise ProjectRegistryError(
                f"cannot verify Git worktree for {project_identity_path(root)}"
            )
        return "non-git"
    try:
        tracked = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--error-unmatch",
                "--",
                pathspec,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
            env=environment,
        )
        if tracked.returncode == 0:
            return "tracked"
        if tracked.returncode != 1:
            raise ProjectRegistryError(
                f"cannot verify whether {project_identity_path(root)} is tracked"
            )
        ignored = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "check-ignore",
                "-q",
                "--no-index",
                "--",
                pathspec,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProjectRegistryError(
            f"cannot verify Git protection for {project_identity_path(root)}: "
            f"{error}"
        ) from error
    if ignored.returncode == 0:
        return "ignored"
    if ignored.returncode == 1:
        return "unignored"
    raise ProjectRegistryError(
        f"cannot verify ignore status for {project_identity_path(root)}"
    )


def _decode_project_identity(root, marker, raw_payload):
    """Validate one already-secured project identity payload."""

    if raw_payload is None:
        return None
    git_status = _project_identity_git_status(root)
    if git_status == "tracked":
        raise ProjectRegistryError(
            f"project identity is tracked by Git and can be copied across "
            f"worktrees: {marker}"
        )
    if git_status == "unignored":
        raise ProjectRegistryError(
            f"project identity is not effectively ignored by Git: {marker}"
        )
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ProjectRegistryError(
            f"cannot read project identity {marker}: {error}"
        ) from error
    if not isinstance(payload, dict) or payload.get("schema") != PROJECT_ID_SCHEMA:
        raise ProjectRegistryError(f"invalid project identity schema in {marker}")
    return _canonical_uuid(payload.get("uuid"), str(marker))


def _read_project_identity_at(root, state_fd, *, required=False):
    marker = project_identity_path(root)
    raw_payload = _read_owned_regular_bytes_at(
        state_fd,
        marker,
        "project identity",
        missing_ok=True,
    )
    if raw_payload is None and required:
        raise ProjectRegistryError(
            f"project is not registered (missing {marker}); "
            f"run rt-projects add {root}"
        )
    return _decode_project_identity(root, marker, raw_payload)


def _read_project_identity(root, *, required=False):
    marker = project_identity_path(root)
    raw_payload = _read_owned_regular_bytes(
        marker, "project identity", missing_ok=True
    )
    if raw_payload is None and required:
        raise ProjectRegistryError(
            f"project is not registered (missing {marker}); "
            f"run rt-projects add {root}"
        )
    return _decode_project_identity(root, marker, raw_payload)


def _verify_open_directory_path(descriptor, path, label):
    """Ensure the held directory is still the one visible at its path."""

    held = os.fstat(descriptor)
    try:
        visible = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise ProjectRegistryError(
            f"{label} changed during update: {path}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(visible.st_mode)
        or visible.st_uid != os.getuid()
        or visible.st_dev != held.st_dev
        or visible.st_ino != held.st_ino
    ):
        raise ProjectRegistryError(f"{label} changed during update: {path}")


def _ensure_project_identity_ignored(root, *, root_fd, state_fd):
    """Keep the worktree-local identity out of source control."""

    state_dir = project_identity_path(root).parent
    ignore_path = state_dir / ".gitignore"
    git_status = _project_identity_git_status(root)
    if git_status == "tracked":
        raise ProjectRegistryError(
            f"project identity is tracked by Git and cannot be safely "
            f"registered: {project_identity_path(root)}"
        )
    try:
        raw_current = _read_owned_regular_bytes_at(
            state_fd,
            ignore_path,
            "project ignore file",
            missing_ok=True,
        )
        current = (
            ""
            if raw_current is None
            else raw_current.decode("utf-8")
        )
    except UnicodeError as error:
        raise ProjectRegistryError(
            f"cannot read project ignore file {ignore_path}: {error}"
        ) from error
    info = (
        None
        if raw_current is None
        else os.stat(
            ignore_path.name,
            dir_fd=state_fd,
            follow_symlinks=False,
        )
    )
    rules = [
        line.strip()
        for line in current.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    positive_indices = [
        index for index, rule in enumerate(rules) if rule == "project.json"
    ]
    protected_by_file = bool(positive_indices) and not any(
        rule.startswith("!")
        for rule in rules[positive_indices[-1] + 1 :]
    )
    if protected_by_file and git_status in {"non-git", "ignored"}:
        return False

    updated = current
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += "project.json\n"
    tmp_name = f".{ignore_path.name}.tmp.{os.getpid()}.{time.time_ns()}"
    descriptor = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(
            tmp_name,
            flags,
            0o600,
            dir_fd=state_fd,
        )
        if info is not None:
            os.fchmod(descriptor, stat.S_IMODE(info.st_mode))
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        if info is None:
            try:
                os.link(
                    tmp_name,
                    ignore_path.name,
                    src_dir_fd=state_fd,
                    dst_dir_fd=state_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                observed = _read_owned_regular_bytes_at(
                    state_fd,
                    ignore_path,
                    "project ignore file",
                )
                try:
                    observed_text = observed.decode("utf-8")
                except UnicodeError as error:
                    raise ProjectRegistryError(
                        f"cannot read project ignore file {ignore_path}: {error}"
                    ) from error
                if "project.json" not in observed_text.splitlines():
                    raise ProjectRegistryError(
                        f"project ignore file changed during registration: "
                        f"{ignore_path}"
                    )
        else:
            observed = _read_owned_regular_bytes_at(
                state_fd,
                ignore_path,
                "project ignore file",
            )
            if observed != raw_current:
                raise ProjectRegistryError(
                    f"project ignore file changed during registration: {ignore_path}"
                )
            os.replace(
                tmp_name,
                ignore_path.name,
                src_dir_fd=state_fd,
                dst_dir_fd=state_fd,
            )
        os.fsync(state_fd)
        _verify_project_guard(root, root_fd, state_fd)
        verified_status = _project_identity_git_status(root)
        if verified_status not in {"non-git", "ignored"}:
            raise ProjectRegistryError(
                f"project identity is not effectively ignored after updating "
                f"{ignore_path}"
            )
        return True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(tmp_name, dir_fd=state_fd)
        except OSError:
            pass


def _write_project_identity(
    root,
    project_uuid,
    *,
    replace_uuid=None,
    root_fd=None,
    state_fd=None,
):
    root = _absolute_project_path(root)
    project_uuid = _canonical_uuid(project_uuid, "project identity")
    marker = project_identity_path(root)
    owns_guard = root_fd is None and state_fd is None
    if (root_fd is None) != (state_fd is None):
        raise ProjectRegistryError(
            "project identity write requires both root and state guards"
        )
    if owns_guard:
        root_fd, state_fd = _open_project_guard(root)
    state_dir = marker.parent
    try:
        _verify_project_guard(root, root_fd, state_fd)
        _ensure_project_identity_ignored(
            root,
            root_fd=root_fd,
            state_fd=state_fd,
        )
        current = _read_project_identity_at(root, state_fd)
        if current == project_uuid:
            # A prior attempt may have published the marker and crashed before
            # the directory entry became durable. Retrying must close that
            # window before the registry can be committed.
            os.fsync(state_fd)
            _verify_project_guard(root, root_fd, state_fd)
            visible = _read_project_identity_at(
                root, state_fd, required=True
            )
            if visible != project_uuid:
                raise ProjectRegistryError(
                    f"project identity did not commit at {marker}"
                )
            return False
        if current is not None and current != replace_uuid:
            raise ProjectRegistryError(
                f"project identity {marker} already names UUID {current}"
            )
        payload = (
            json.dumps(
                {"schema": PROJECT_ID_SCHEMA, "uuid": project_uuid},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        tmp_name = f".{marker.name}.tmp.{os.getpid()}.{time.time_ns()}"
        descriptor = None
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(
                tmp_name,
                flags,
                0o600,
                dir_fd=state_fd,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = None
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if current is None:
                try:
                    os.link(
                        tmp_name,
                        marker.name,
                        src_dir_fd=state_fd,
                        dst_dir_fd=state_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    observed = _read_project_identity_at(
                        root, state_fd, required=True
                    )
                    if observed != project_uuid:
                        raise ProjectRegistryError(
                            f"project identity changed during registration: {marker}"
                        )
            else:
                observed = _read_project_identity_at(
                    root, state_fd, required=True
                )
                if observed != replace_uuid:
                    raise ProjectRegistryError(
                        f"project identity changed during registration: {marker}"
                    )
                os.replace(
                    tmp_name,
                    marker.name,
                    src_dir_fd=state_fd,
                    dst_dir_fd=state_fd,
                )
            os.fsync(state_fd)
            _verify_project_guard(root, root_fd, state_fd)
            visible = _read_project_identity_at(
                root, state_fd, required=True
            )
            if visible != project_uuid:
                raise ProjectRegistryError(
                    f"project identity did not commit at {marker}"
                )
            return True
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(tmp_name, dir_fd=state_fd)
            except OSError:
                pass
    except ProjectRegistryError:
        raise
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot commit project identity {marker}: {error}"
        ) from error
    finally:
        if owns_guard:
            _close_project_guard(root_fd, state_fd)


def _derive_project_group(root, project_uuid):
    """Return ``(group, authoritative)`` for the current filesystem state."""

    root = _absolute_project_path(root)
    fallback = _canonical_uuid(project_uuid, "project group fallback")
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
            env=_clean_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        if _looks_like_git_worktree(root):
            raise ProjectRegistryError(
                f"cannot derive Git sibling group for {root}: {error}"
            ) from error
        return fallback, True
    if result.returncode == 0:
        value = result.stdout.strip()
        if value:
            common = Path(value)
            if not common.is_absolute():
                common = root / common
            try:
                canonical = common.resolve()
            except OSError:
                canonical = None
            if canonical is not None:
                digest = hashlib.sha256(
                    os.fsencode(str(canonical))
                ).hexdigest()
                return f"git:{digest}", True
    if _looks_like_git_worktree(root):
        raise ProjectRegistryError(
            f"cannot derive Git sibling group for {root}"
        )
    return fallback, True


def derive_project_group(root, project_uuid):
    """Derive the sibling-address group without accepting declared authority."""

    group, _authoritative = _derive_project_group(root, project_uuid)
    return group


def _mint_project_uuid(used):
    for _attempt in range(32):
        candidate = str(uuid.uuid4())
        if candidate not in used:
            return candidate
    raise ProjectRegistryError("could not mint a unique project UUID")


def _v2_entry(
    root,
    project_uuid,
    *,
    registered_at,
    status_value="active",
    layout="local",
    tombstoned_at=None,
):
    root = _absolute_project_path(root)
    if not root.name.strip() or "\n" in root.name or "\r" in root.name:
        raise ProjectRegistryError(
            f"project path has no valid registry name: {root}"
        )
    entry = {
        "uuid": project_uuid,
        "path": str(root),
        "name": root.name,
        "group": derive_project_group(root, project_uuid)
        if status_value == "active" and is_project_root(root)
        else project_uuid,
        "layout": layout,
        "status": status_value,
        "registered_at": registered_at,
    }
    if status_value == "tombstoned":
        entry["tombstoned_at"] = tombstoned_at or _utc_timestamp()
    return entry


def _write_verified_legacy_backup(path, payload, *, parent_fd):
    """Atomically publish the exact v1 snapshot beside the held registry."""

    backup = legacy_registry_backup_path(path)
    existing = _read_owned_regular_bytes_at(
        parent_fd,
        backup,
        "legacy registry backup",
        missing_ok=True,
    )
    if existing is not None:
        if existing != payload:
            raise ProjectRegistryError(
                f"legacy registry backup collision at {backup}"
            )
        return backup
    tmp_name = f".{backup.name}.tmp.{os.getpid()}.{time.time_ns()}"
    descriptor = None
    try:
        descriptor = os.open(
            tmp_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(
                tmp_name,
                backup.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_owned_regular_bytes_at(
                parent_fd,
                backup,
                "legacy registry backup",
            )
            if existing != payload:
                raise ProjectRegistryError(
                    f"legacy registry backup collision at {backup}"
                )
        os.fsync(parent_fd)
    except ProjectRegistryError:
        raise
    except OSError as error:
        raise ProjectRegistryError(
            f"cannot publish legacy registry backup {backup}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except OSError:
            pass
    verified = _read_owned_regular_bytes_at(
        parent_fd,
        backup,
        "legacy registry backup",
    )
    if verified != payload:
        raise ProjectRegistryError(f"legacy registry backup verification failed: {backup}")
    return backup


def upgrade_project_registry(path=None, *, upgraded_at=None):
    """Explicitly and atomically upgrade a root-only v1 registry in scratch/user flow."""

    path = _registry_path(path)
    timestamp = upgraded_at or _utc_timestamp()
    project_guards = []

    def guard_all():
        for root, root_fd, state_fd in project_guards:
            _verify_project_guard(root, root_fd, state_fd)

    def upgrade(document, source_payload, parent_fd):
        if document.get("schema") == PROJECTS_SCHEMA:
            _strict_entries_from_document(document, path)
            return False
        legacy_entries, warnings = _parse_legacy_entries(document, path)
        structural = _structural_registry_warnings(warnings[1:])
        if structural or len(legacy_entries) != len(document.get("projects") or []):
            raise ProjectRegistryError(
                "refusing partial legacy registry upgrade: "
                + "; ".join(structural or warnings[1:])
            )
        if source_payload is None:
            raise ProjectRegistryError(
                f"legacy registry snapshot disappeared: {path}"
            )
        used = set()
        upgraded = []
        marker_plan = []
        for entry in legacy_entries:
            root = entry["root"]
            if entry["available"]:
                root_fd, state_fd = _open_project_guard(root)
                project_guards.append((root, root_fd, state_fd))
                existing_uuid = _read_project_identity_at(root, state_fd)
            else:
                root_fd = state_fd = None
                existing_uuid = None
            project_uuid = existing_uuid or _mint_project_uuid(used)
            if project_uuid in used:
                raise ProjectRegistryError(
                    f"legacy registry contains duplicate project identity {project_uuid}"
                )
            used.add(project_uuid)
            if entry["available"]:
                marker_plan.append(
                    (root, project_uuid, root_fd, state_fd)
                )
                status_value = "active"
            else:
                status_value = "tombstoned"
            upgraded.append(
                _v2_entry(
                    root,
                    project_uuid,
                    registered_at=entry["registered_at"],
                    status_value=status_value,
                    tombstoned_at=timestamp,
                )
            )
        _write_verified_legacy_backup(
            path,
            source_payload,
            parent_fd=parent_fd,
        )
        for root, project_uuid, root_fd, state_fd in marker_plan:
            _write_project_identity(
                root,
                project_uuid,
                root_fd=root_fd,
                state_fd=state_fd,
            )
        document.clear()
        document.update({"schema": PROJECTS_SCHEMA, "projects": upgraded})
        return True

    try:
        return _update_project_registry(
            upgrade,
            path,
            guard=guard_all,
        )
    except ProjectRegistryError as error:
        raise SystemExit(f"roundtable: project registry upgrade failed: {error}") from None
    finally:
        for _root, root_fd, state_fd in reversed(project_guards):
            _close_project_guard(root_fd, state_fd)


def register_project(root, path=None, registered_at=None):
    raw_root = Path(root).expanduser()
    if raw_root.is_symlink():
        raise SystemExit(f"roundtable: refusing symbolic-link project root: {raw_root}")
    root = raw_root.resolve()
    if root in {Path.home().resolve(), Path(root.anchor)}:
        raise SystemExit(
            f"roundtable: not a project (missing {project_config_path(root)}): {root}"
        )
    timestamp = registered_at or _utc_timestamp()
    path = _registry_path(path)
    try:
        root_fd, state_fd = _open_project_guard(root)
    except ProjectRegistryError as error:
        raise SystemExit(
            f"roundtable: project registration failed: {error}"
        ) from None

    def guard():
        _verify_project_guard(root, root_fd, state_fd)

    def add(document, _source_payload, _parent_fd):
        if document.get("schema") != PROJECTS_SCHEMA:
            raise ProjectRegistryError(
                f"{path} uses legacy schema; run rt-projects --registry {path} upgrade"
            )
        entries, _warnings = _strict_entries_from_document(document, path)
        marker_uuid = _read_project_identity_at(root, state_fd)
        by_uuid = {entry["uuid"]: entry for entry in entries}
        active_at_root = [
            entry
            for entry in entries
            if entry["status"] == "active" and entry["root"] == root
        ]
        if len(active_at_root) > 1:
            raise ProjectRegistryError(f"multiple active entries claim {root}")
        if active_at_root:
            existing = active_at_root[0]
            if marker_uuid is None:
                raise ProjectRegistryError(
                    f"active registry entry {existing['uuid']} claims {root}, "
                    f"but {project_identity_path(root)} is missing; refusing "
                    "path-based identity inheritance. Restore the verified "
                    "identity marker, or tombstone this registration before "
                    "adding the replacement project"
                )
            if marker_uuid != existing["uuid"]:
                raise ProjectRegistryError(
                    f"project identity {marker_uuid} conflicts with registry UUID "
                    f"{existing['uuid']} for {root}"
                )
            return False

        replace_uuid = None
        if marker_uuid is not None:
            marker_entry = by_uuid.get(marker_uuid)
            if marker_entry is not None and marker_entry["status"] == "active":
                old_root = marker_entry["root"]
                if (
                    old_root != root
                    and marker_entry["available"]
                    and not _same_existing_inode(old_root, root)
                ):
                    raise ProjectRegistryError(
                        f"project UUID {marker_uuid} is already active at {old_root}; "
                        f"refusing copied identity at {root}"
                    )
                for raw in document["projects"]:
                    if raw.get("uuid") == marker_uuid:
                        raw["path"] = str(root)
                        raw["name"] = root.name
                        raw["group"] = derive_project_group(root, marker_uuid)
                        return True
            if marker_entry is not None and marker_entry["status"] == "tombstoned":
                replace_uuid = marker_uuid
                marker_uuid = None

        used = set(by_uuid)
        project_uuid = marker_uuid or _mint_project_uuid(used)
        _write_project_identity(
            root,
            project_uuid,
            replace_uuid=replace_uuid,
            root_fd=root_fd,
            state_fd=state_fd,
        )
        document["projects"].append(
            _v2_entry(
                root,
                project_uuid,
                registered_at=timestamp,
            )
        )
        return True

    try:
        return _update_project_registry(add, path, guard=guard)
    except ProjectRegistryError as error:
        raise SystemExit(f"roundtable: project registration failed: {error}") from None
    finally:
        _close_project_guard(root_fd, state_fd)


def unregister_project(root, path=None, tombstoned_at=None):
    raw_root = Path(root).expanduser()
    if raw_root.is_symlink():
        raise SystemExit(
            f"roundtable: refusing symbolic-link project root: {raw_root}"
        )
    root = raw_root.resolve()
    timestamp = tombstoned_at or _utc_timestamp()
    path = _registry_path(path)
    root_fd = state_fd = None
    project_uuid = None
    try:
        root_fd, state_fd = _open_project_guard(root)
    except ProjectRegistryError as error:
        # Tombstones are the explicit recovery path for deleted roots and for
        # a replacement project that has not inherited the prior UUID.
        if is_project_root(root):
            raise SystemExit(
                f"roundtable: project removal failed: {error}"
            ) from None
    else:
        try:
            project_uuid = _read_project_identity_at(root, state_fd)
        except ProjectRegistryError as error:
            raise SystemExit(
                f"roundtable: project removal failed: {error}"
            ) from None

    def guard():
        if root_fd is not None:
            _verify_project_guard(root, root_fd, state_fd)

    def tombstone(document, _source_payload, _parent_fd):
        if document.get("schema") != PROJECTS_SCHEMA:
            raise ProjectRegistryError(
                f"{path} uses legacy schema; run rt-projects --registry {path} upgrade"
            )
        entries, _warnings = _strict_entries_from_document(document, path)
        if project_uuid is None:
            matches = [
                entry
                for entry in entries
                if entry["status"] == "active" and entry["root"] == root
            ]
            if not matches:
                return False
            if len(matches) != 1:
                raise ProjectRegistryError(
                    f"multiple active entries claim {root}"
                )
        else:
            matches = [
                entry for entry in entries if entry["uuid"] == project_uuid
            ]
            if len(matches) != 1:
                raise ProjectRegistryError(
                    f"project identity {project_uuid} has "
                    f"{len(matches)} registry entries"
                )
        match = matches[0]
        if match["status"] == "tombstoned":
            return False
        if (
            match["root"] != root
            and match["available"]
            and not _same_existing_inode(match["root"], root)
        ):
            raise ProjectRegistryError(
                f"project identity {project_uuid} is active at "
                f"{match['root']}, not {root}"
            )
        matched_uuid = match["uuid"]
        for raw in document["projects"]:
            if raw.get("uuid") == matched_uuid:
                raw["path"] = str(root)
                raw["name"] = root.name
                raw["status"] = "tombstoned"
                raw["tombstoned_at"] = timestamp
                return True
        raise ProjectRegistryError(
            f"registry entry disappeared for {matched_uuid}"
        )

    try:
        return _update_project_registry(tombstone, path, guard=guard)
    except ProjectRegistryError as error:
        raise SystemExit(f"roundtable: project removal failed: {error}") from None
    finally:
        if root_fd is not None:
            _close_project_guard(root_fd, state_fd)


def _validate_owned_directory(path, label):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ProjectRegistryError(f"{label} is missing: {path}") from None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ProjectRegistryError(
            f"{label} is not a real directory: {path}"
        )
    if info.st_uid != os.getuid():
        raise ProjectRegistryError(
            f"{label} is owned by uid {info.st_uid}, expected {os.getuid()}: "
            f"{path}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ProjectRegistryError(
            f"{label} is group/other writable: {path}"
        )


def mailbox_from_registry_entry(entry, registry_path=None):
    """Resolve paths from one already-parsed immutable registry entry."""

    project_uuid = _canonical_uuid(entry.get("uuid"), "project registry entry")
    root = _absolute_project_path(
        entry.get("root") or entry.get("path")
    )
    layout = entry.get("layout")
    if layout not in PROJECT_LAYOUTS:
        raise ProjectRegistryError(
            f"project {project_uuid} has unsupported layout {layout!r}"
        )
    registry = _registry_path(registry_path)
    state_dir = root / ".roundtable"
    _validate_owned_directory(state_dir, "project state")
    if layout == "local":
        mail_root = state_dir
        inbox_dir = state_dir / "inbox"
    else:
        mail_root = registry.parent / "mail" / project_uuid
        inbox_dir = mail_root / "inbox"
        for directory, label in (
            (registry.parent / "mail", "central mail parent"),
            (mail_root, "central project mail root"),
            (inbox_dir, "central inbox"),
            (mail_root / "messages", "central messages"),
            (mail_root / "locks", "central locks"),
        ):
            _validate_owned_directory(directory, label)
    return ProjectMailbox(
        project_uuid=project_uuid,
        project_root=root,
        layout=layout,
        state_dir=state_dir,
        mail_root=mail_root,
        inbox_dir=inbox_dir,
        messages_dir=mail_root / "messages",
        locks_dir=mail_root / "locks",
        layout_lock=registry.parent / "layout-locks" / f"{project_uuid}.lock",
    )


def _reindex_project_identity(root, project_uuid, path, *, guard=None):
    def reindex(document, _source_payload, _parent_fd):
        if document.get("schema") != PROJECTS_SCHEMA:
            raise ProjectRegistryError(
                f"{path} uses legacy schema; run rt-projects --registry {path} upgrade"
            )
        entries, _warnings = _strict_entries_from_document(document, path)
        matches = [entry for entry in entries if entry["uuid"] == project_uuid]
        if len(matches) != 1:
            raise ProjectRegistryError(
                f"project identity {project_uuid} has {len(matches)} registry entries"
            )
        entry = matches[0]
        if entry["status"] != "active":
            raise ProjectRegistryError(
                f"project identity {project_uuid} is tombstoned"
            )
        moving = entry["root"] != root
        if moving:
            same_location = (
                entry["available"]
                and _same_existing_inode(entry["root"], root)
            )
            if entry["available"] and not same_location:
                raise ProjectRegistryError(
                    f"project identity {project_uuid} is already active at "
                    f"{entry['root']}; refusing copied identity at {root}"
                )
            conflicts = [
                candidate
                for candidate in entries
                if candidate["status"] == "active"
                and candidate["uuid"] != project_uuid
                and (
                    candidate["root"] == root
                    or (
                        candidate["available"]
                        and _same_existing_inode(candidate["root"], root)
                    )
                )
            ]
            if conflicts:
                raise ProjectRegistryError(
                    f"project path {root} is already registered as "
                    f"{conflicts[0]['uuid']}"
                )
        derived_group, group_authoritative = _derive_project_group(
            root, project_uuid
        )
        for raw in document["projects"]:
            if raw.get("uuid") == project_uuid:
                changed = False
                for field, value in (
                    ("path", str(root)),
                    ("name", root.name),
                ):
                    if raw.get(field) != value:
                        raw[field] = value
                        changed = True
                if group_authoritative and raw.get("group") != derived_group:
                    raw["group"] = derived_group
                    changed = True
                return changed
        raise ProjectRegistryError(
            f"project registry entry disappeared for {project_uuid}"
        )

    return _update_project_registry(reindex, path, guard=guard)


def resolve_project_mailbox_checked(project, registry_path=None):
    """Resolve one exact registered project, preserving typed failures."""

    path = _registry_path(registry_path)
    raw_root = Path(project).expanduser()
    if raw_root.is_symlink():
        raise ProjectRegistryError(
            f"project root is a symbolic link: {raw_root}"
        )
    root = raw_root.resolve()
    if root in {Path.home().resolve(), Path(root.anchor)}:
        raise ProjectRegistryError(
            f"not a project (missing {project_config_path(root)}): {root}"
        )
    root_fd, state_fd = _open_project_guard(root)

    def guard():
        _verify_project_guard(root, root_fd, state_fd)

    try:
        project_uuid = _read_project_identity_at(
            root, state_fd, required=True
        )
        _document, entries, warnings = _strict_project_registry(path)
        structural = _structural_registry_warnings(warnings)
        if structural:
            raise ProjectRegistryError("; ".join(structural))
        matches = [entry for entry in entries if entry["uuid"] == project_uuid]
        if len(matches) != 1:
            raise ProjectRegistryError(
                f"project identity {project_uuid} has "
                f"{len(matches)} registry entries"
            )
        entry = matches[0]
        if entry["status"] != "active":
            raise ProjectRegistryError(
                f"project identity {project_uuid} is tombstoned"
            )
        metadata_drift = (
            entry["root"] != root
            or entry["name"] != root.name
            or (
                entry.get("group_authoritative")
                and entry.get("declared_group") != entry.get("group")
            )
        )
        if metadata_drift:
            _reindex_project_identity(
                root,
                project_uuid,
                path,
                guard=guard,
            )
            _document, entries, warnings = _strict_project_registry(path)
            structural = _structural_registry_warnings(warnings)
            if structural:
                raise ProjectRegistryError("; ".join(structural))
            matches = [
                entry for entry in entries if entry["uuid"] == project_uuid
            ]
            if (
                len(matches) != 1
                or matches[0]["root"] != root
                or matches[0]["name"] != root.name
                or (
                    matches[0].get("group_authoritative")
                    and matches[0].get("declared_group")
                    != matches[0].get("group")
                )
            ):
                raise ProjectRegistryError(
                    f"project identity {project_uuid} reconciliation "
                    "did not commit"
                )
            entry = matches[0]
        mailbox = mailbox_from_registry_entry(entry, path)
        guard()
        return mailbox
    finally:
        _close_project_guard(root_fd, state_fd)


def resolve_project_mailbox(project, registry_path=None):
    """CLI-facing mailbox resolver with a concise fail-closed diagnostic."""

    try:
        return resolve_project_mailbox_checked(project, registry_path)
    except ProjectRegistryError as error:
        raise SystemExit(f"roundtable: mailbox resolution failed: {error}") from None


def project_config_path(root):
    return root / ".roundtable" / "agents.yaml"


def is_project_root(root):
    candidate = Path(root).expanduser().resolve()
    home = Path.home().expanduser().resolve()
    if candidate == home or candidate == Path(candidate.anchor):
        return False
    return project_config_path(candidate).is_file()


def fallback_project_root():
    """Fallback project when cwd isn't inside one and no workspace binding matches."""
    default = os.environ.get("RT_FALLBACK_PROJECT", "")
    if default:
        return Path(default).expanduser().resolve()
    return None


def not_project_message(tool):
    return (
        f"{tool}: not in a roundtable project; create .roundtable/agents.yaml, "
        "set ROUNDTABLE_PROJECT_DIR, or set RT_FALLBACK_PROJECT to a fallback project. "
        "Run 'roundtable-init <name>' to create a new project."
    )


def project_for_current_workspace():
    """Resolve the roundtable project bound to the caller's current cmux workspace.

    Matches by stable workspace_id (then ref) across all known project roots.
    Returns the project root Path, or None.
    """
    try:
        identify = json.loads(
            subprocess.check_output(
                ["cmux", "identify", "--json", "--id-format", "both"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
            )
        )
    except Exception:
        return None
    caller = identify.get("caller") or {}
    ws_id, ws_ref = caller.get("workspace_id"), caller.get("workspace_ref")
    if not (ws_id or ws_ref):
        return None

    exact_matches = []
    legacy_matches = []
    for root in registered_project_roots(tool="roundtable"):
        runtime = root / ".roundtable" / "runtime.json"
        try:
            data = json.loads(runtime.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        runtime_ws_id = data.get("workspace_id")
        if runtime_ws_id:
            if ws_id and runtime_ws_id == ws_id:
                exact_matches.append(root)
            continue
        if ws_ref and data.get("workspace_ref") == ws_ref:
            legacy_matches.append(root)

    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        projects = ", ".join(str(root) for root in exact_matches)
        raise SystemExit(
            f"roundtable: multiple projects match caller workspace UUID {ws_id}: {projects}; "
            "set ROUNDTABLE_PROJECT_DIR explicitly"
        )
    if len(legacy_matches) == 1:
        return legacy_matches[0]
    if len(legacy_matches) > 1:
        projects = ", ".join(str(root) for root in legacy_matches)
        raise SystemExit(
            f"roundtable: multiple legacy projects match caller workspace ref {ws_ref}: {projects}; "
            "set ROUNDTABLE_PROJECT_DIR explicitly"
        )
    return None


def find_project_root(tool, *, allow_cmux_workspace=False):
    """Find a project without consulting a terminal API by default.

    Core maildir commands use explicit configuration, cwd, or the documented
    fallback. Only cmux adapter commands opt into workspace-based discovery.
    """
    override = os.environ.get("ROUNDTABLE_PROJECT_DIR")
    if override:
        root = Path(override).expanduser().resolve()
        if is_project_root(root):
            return root
        raise SystemExit(not_project_message(tool))

    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if is_project_root(candidate):
            return candidate

    fallback = fallback_project_root()
    if fallback and is_project_root(fallback):
        return fallback

    if allow_cmux_workspace:
        bound = project_for_current_workspace()
        if bound is not None:
            return bound

    raise SystemExit(not_project_message(tool))


def load_agents_doc(root, tool):
    if yaml is None:
        raise SystemExit(f"{tool}: PyYAML is required to read .roundtable/agents.yaml")
    with project_config_path(root).open() as fh:
        return yaml.safe_load(fh) or {}


def agent_names(agents_doc):
    """Extract agent names from agents.yaml (dynamic, not hardcoded)."""
    return list((agents_doc.get("agents") or {}).keys())


def run_json(*args):
    return json.loads(subprocess.check_output(args, text=True))


def caller_cmux_context(identify, tree=None):
    for source in (identify or {}, tree or {}):
        value = source.get("caller") or {}
        if value.get("workspace_ref"):
            return value
    return {}


def caller_workspace_ref(identify, tree=None):
    """Return only the real cmux caller's workspace ref.

    GUI focus is deliberately excluded: focused/active describe what the user is
    looking at, not which workspace launched the calling process.
    """
    return caller_cmux_context(identify, tree).get("workspace_ref") or ""


def current_workspace_ref(identify, tree=None):
    """Return caller/focused/active workspace for read-only display paths only."""
    for source in (identify or {}, tree or {}):
        for key in ("caller", "focused", "active"):
            value = source.get(key) or {}
            if value.get("workspace_ref"):
                return value.get("workspace_ref")
    return ""


def workspace_by_id(tree, workspace_id):
    """Find a cmux workspace by its stable UUID, returning (window, workspace)."""
    if not workspace_id:
        return None, None
    for window in (tree or {}).get("windows", []):
        for workspace in window.get("workspaces", []):
            if (workspace.get("id") or workspace.get("workspace_id")) == workspace_id:
                return window, workspace
    return None, None


def iter_ledgers(msg_dir):
    for path in sorted(Path(msg_dir).glob("*.jsonl")):
        if not path.is_file():
            continue
        if path.name.count("-") and path.stem.rsplit("-", 1)[-1].isdigit():
            continue
        yield path


def read_records(msg_dir):
    records = []
    for path in iter_ledgers(msg_dir):
        try:
            with path.open() as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("msg_id") and record.get("lifecycle") in LIFECYCLE_ORDINAL:
                        records.append(record)
        except (OSError, UnicodeError):
            # Maildir is the delivery fact source. One degraded optional ledger
            # must not make otherwise valid inbox mail unreadable.
            continue
    return records


def current_records(records):
    current = {}
    for record in records:
        msg_id = record.get("msg_id")
        rank = LIFECYCLE_ORDINAL.get(record.get("lifecycle"), -1)
        old = current.get(msg_id)
        old_rank = LIFECYCLE_ORDINAL.get((old or {}).get("lifecycle"), -1)
        if old is None or rank > old_rank or (
            rank == old_rank and record.get("ts", "") > old.get("ts", "")
        ):
            current[msg_id] = record
    return current


def has_lifecycle(records, msg_id, lifecycle):
    return any(
        record.get("msg_id") == msg_id and record.get("lifecycle") == lifecycle
        for record in records
    )


def find_msg(records, msg_id):
    matches = [record for record in records if record.get("msg_id") == msg_id]
    if not matches:
        return None
    return current_records(matches).get(msg_id)


def acquire_lock(path, timeout=10.0, tool="rt"):
    deadline = time.time() + timeout
    while True:
        try:
            path.mkdir(parents=True)
            return
        except FileExistsError:
            if time.time() >= deadline:
                raise SystemExit(f"{tool}: timed out waiting for lock {path}")
            time.sleep(0.05)
