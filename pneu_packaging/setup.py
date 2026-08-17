"""Ownership-safe onboarding for pneu harness integrations.

The command is deliberately dry-run first: invoking it without a subcommand is
equivalent to ``plan``.  ``plan`` and ``status`` only inspect files.  ``apply``
and ``remove`` use a separate manifest from the package installer so that each
configuration fragment can be removed without restoring an entire user file.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import fnmatch
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

try:
    import _rtcodex
except ModuleNotFoundError:  # Source-tree tests add ``bin`` before using Codex.
    _rtcodex = None  # type: ignore[assignment]


SCHEMA = "roundtable.harness-setup.v1"
HARNESSES = ("claude", "hermes", "codex")
CODEX_LABELS = (
    "com.roundtable.codex-app-server",
    "com.roundtable.codex-wake",
    # Re-applies the Desktop join switch at login; `launchctl setenv` alone
    # does not survive logout or reboot.
    "com.roundtable.codex-daemon-join",
)
CODEX_DAEMON_JOIN_LABEL = "com.roundtable.codex-daemon-join"
# The two long-running service jobs, as opposed to the login-time join agent
# that only sets one launchd variable.
CODEX_SERVICE_LABELS = (
    "com.roundtable.codex-app-server",
    "com.roundtable.codex-wake",
)
CODEX_DAEMON_JOIN_VARIABLE = "CODEX_APP_SERVER_USE_LOCAL_DAEMON"
CODEX_DAEMON_JOIN_VALUE = "1"
CODEX_RELOAD_MARKER_SCHEMA = "roundtable.codex-app-server-reload-required.v1"
CODEX_RELOAD_MARKER_NAME = "codex-app-server-reload-required.json"
# rt-wait-inbox is a long-lived async hook that renews its lease silently while
# waiting for mail. Claude Code otherwise applies a ten-minute default timeout
# to async command hooks.
CLAUDE_HOOK_TIMEOUT_SECONDS = 1_209_600
CODEX_HOOK_TIMEOUT_SECONDS = 5


class SetupError(RuntimeError):
    """A fail-closed setup, ownership, or drift error."""


class SetupMutationError(SetupError):
    """A setup mutation failed, with explicit rollback outcome metadata."""

    def __init__(
        self,
        message: str,
        *,
        writes: bool,
        rolled_back: bool,
        launchctl_invoked: bool = False,
        rollback_errors: tuple[str, ...] = (),
    ) -> None:
        detail = message
        if writes:
            detail += (
                "; managed config/link/plist/manifest paths were restored"
                if rolled_back
                else "; setup mutations could not be fully restored"
            )
        if rollback_errors:
            detail += "; rollback errors: " + " | ".join(rollback_errors)
        super().__init__(detail)
        self.writes = writes
        self.rolled_back = rolled_back
        self.launchctl_invoked = launchctl_invoked
        self.rollback_errors = rollback_errors


class _LaunchctlOperationError(SetupError):
    """An unload operation failed after launchctl may have changed live state."""

    def __init__(
        self,
        message: str,
        *,
        invoked: bool,
        external_changed: bool,
    ) -> None:
        super().__init__(message)
        self.invoked = invoked
        self.external_changed = external_changed


@dataclass(frozen=True)
class _PathSnapshot:
    path: Path
    kind: str
    payload: bytes | str | None
    mode: int | None


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def _enabled_rc_host_states(prefix: Path) -> list[Path]:
    configured = os.environ.get("RT_PROJECTS_FILE")
    registry = _absolute(configured) if configured else prefix / "projects.yaml"
    directory = registry.parent / "rc-hosts"
    try:
        directory_info = directory.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise SetupError(f"cannot inspect rc-host state {directory}: {error}") from error
    if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(
        directory_info.st_mode
    ):
        raise SetupError(f"refusing unsafe rc-host state directory: {directory}")
    if directory_info.st_uid != os.getuid():
        raise SetupError(f"rc-host state directory is not user-owned: {directory}")

    states: list[Path] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".json":
            continue
        try:
            info = path.lstat()
        except OSError as error:
            raise SetupError(f"cannot inspect rc-host state {path}: {error}") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SetupError(f"refusing unsafe rc-host state file: {path}")
        if info.st_uid != os.getuid():
            raise SetupError(f"rc-host state file is not user-owned: {path}")
        states.append(path)
    return states


def _require_rc_hosts_disabled(prefix: Path) -> None:
    states = _enabled_rc_host_states(prefix)
    if not states:
        return
    rendered = ", ".join(str(path) for path in states)
    raise SetupError(
        "Claude phone access is still enabled for one or more projects; run "
        "`pneu rc-host disable` from each enabled project before removing "
        f"Claude onboarding ({rendered})"
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _yaml_bytes(value: object) -> bytes:
    return yaml.safe_dump(
        value,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")


def _path_kind(info: os.stat_result) -> str:
    if stat.S_ISDIR(info.st_mode):
        return "directory"
    if stat.S_ISREG(info.st_mode):
        return "regular file"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    return "special file"


def _inspect_owned(path: Path, *, kind: str | None = None) -> os.stat_result | None:
    """Inspect one existing path without following a leaf symlink."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SetupError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise SetupError(f"refusing symlink where {kind or 'managed path'} is expected: {path}")
    if info.st_uid != os.getuid():
        raise SetupError(
            f"refusing path owned by uid {info.st_uid}, expected {os.getuid()}: {path}"
        )
    if kind == "file" and not stat.S_ISREG(info.st_mode):
        raise SetupError(f"expected regular file, found {_path_kind(info)}: {path}")
    if kind == "directory" and not stat.S_ISDIR(info.st_mode):
        raise SetupError(f"expected directory, found {_path_kind(info)}: {path}")
    return info


def _validate_user_chain(home: Path, path: Path) -> None:
    """Reject symlinked or foreign-owned components below the selected home."""

    home_info = _inspect_owned(home, kind="directory")
    if home_info is None:
        raise SetupError(f"home directory does not exist: {home}")
    try:
        relative = path.relative_to(home)
    except ValueError as error:
        raise SetupError(f"user configuration path escapes home {home}: {path}") from error
    current = home
    for part in relative.parts:
        current = current / part
        if not _lexists(current):
            break
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise SetupError(f"refusing symlink in user configuration path: {current}")
        if info.st_uid != os.getuid():
            raise SetupError(
                f"refusing path owned by uid {info.st_uid}, expected "
                f"{os.getuid()}: {current}"
            )


def _validate_prefix(prefix: Path) -> None:
    info = _inspect_owned(prefix, kind="directory")
    if info is None:
        raise SetupError(
            f"pneu install prefix does not exist: {prefix}; install the package first"
        )


def _ensure_user_dir(home: Path, path: Path) -> None:
    _validate_user_chain(home, path)
    relative = path.relative_to(home)
    current = home
    for part in relative.parts:
        current = current / part
        if _lexists(current):
            _inspect_owned(current, kind="directory")
            continue
        try:
            current.mkdir(mode=0o700)
        except OSError as error:
            raise SetupError(f"cannot create private directory {current}: {error}") from error
        os.chmod(current, 0o700)


def _ensure_private_dir(path: Path) -> None:
    """Create a private directory tree below an existing owned prefix."""

    missing: list[Path] = []
    current = path
    while not _lexists(current):
        missing.append(current)
        current = current.parent
    _inspect_owned(current, kind="directory")
    for candidate in reversed(missing):
        try:
            candidate.mkdir(mode=0o700)
        except OSError as error:
            raise SetupError(f"cannot create private directory {candidate}: {error}") from error
        os.chmod(candidate, 0o700)
    _inspect_owned(path, kind="directory")


def _validate_owned_chain(root: Path, path: Path) -> None:
    """Reject symlinked, foreign-owned, or non-directory components below root."""

    _inspect_owned(root, kind="directory")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise SetupError(f"managed path escapes ownership root {root}: {path}") from error
    current = root
    for part in relative.parts:
        current = current / part
        if not _lexists(current):
            break
        _inspect_owned(current, kind="directory")


def _nearest_existing_directory(path: Path) -> Path:
    current = path
    while not _lexists(current):
        parent = current.parent
        if parent == current:
            raise SetupError(f"no existing directory anchors managed path: {path}")
        current = parent
    _inspect_owned(current, kind="directory")
    return current


def _require_mutable_parent(home: Path, prefix: Path, path: Path) -> None:
    """Preflight the directory that an atomic write, link, or unlink needs."""

    try:
        path.relative_to(home)
    except ValueError:
        try:
            path.relative_to(prefix)
        except ValueError as error:
            raise SetupError(
                f"planned setup mutation escapes home and install prefix: {path}"
            ) from error
        _validate_owned_chain(prefix, path.parent)
    else:
        _validate_user_chain(home, path.parent)
    anchor = _nearest_existing_directory(path.parent)
    if not os.access(anchor, os.W_OK | os.X_OK):
        raise SetupError(
            f"planned setup mutation parent is not writable and searchable: "
            f"{anchor} (target {path})"
        )


@contextlib.contextmanager
def _mutation_lock(prefix: Path) -> Iterator[None]:
    """Serialize setup mutations without making plan/status write anything."""

    runtime = prefix / ".runtime"
    _ensure_private_dir(runtime)
    path = runtime / "harness-setup.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise SetupError(f"cannot open setup lock {path}: {error}") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SetupError(f"refusing unsafe setup lock: {path}")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except SetupError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise SetupError(f"cannot lock setup state at {path}: {error}") from error
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_rewrite_owned(path: Path, payload: bytes, mode: int) -> None:
    """Rewrite or create a managed file beneath an existing owned directory.

    This is the ownership-safe contract.  Bootstrap publication into fresh
    installer trees uses ``cli._atomic_publish_bootstrap`` instead.
    """

    if _lexists(path):
        _inspect_owned(path, kind="file")
    _inspect_owned(path.parent, kind="directory")
    temporary = path.with_name(f".{path.name}.roundtable-{os.getpid()}")
    if _lexists(temporary):
        raise SetupError(f"temporary setup path already exists: {temporary}")
    descriptor: int | None = None
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
    except OSError as error:
        raise SetupError(f"cannot atomically write {path}: {error}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _create_symlink(path: Path, target: Path) -> None:
    if _lexists(path):
        raise SetupError(f"refusing to replace existing path with a symlink: {path}")
    try:
        path.symlink_to(str(target))
    except OSError as error:
        raise SetupError(f"cannot create managed symlink {path}: {error}") from error


def _read_regular(path: Path) -> tuple[bytes, int] | None:
    if not _lexists(path):
        return None
    info = _inspect_owned(path, kind="file")
    assert info is not None
    try:
        return path.read_bytes(), stat.S_IMODE(info.st_mode)
    except OSError as error:
        raise SetupError(f"cannot read {path}: {error}") from error


def _snapshot_path(path: Path) -> _PathSnapshot:
    if not _lexists(path):
        return _PathSnapshot(path, "absent", None, None)
    info = path.lstat()
    if info.st_uid != os.getuid():
        raise SetupError(
            f"refusing snapshot of path owned by uid {info.st_uid}: {path}"
        )
    if stat.S_ISLNK(info.st_mode):
        return _PathSnapshot(path, "symlink", os.readlink(path), None)
    if stat.S_ISREG(info.st_mode):
        try:
            payload = path.read_bytes()
        except OSError as error:
            raise SetupError(f"cannot snapshot managed file {path}: {error}") from error
        return _PathSnapshot(
            path,
            "file",
            payload,
            stat.S_IMODE(info.st_mode),
        )
    raise SetupError(
        f"cannot transactionally snapshot {_path_kind(info)} at managed path: {path}"
    )


def _snapshot_paths(paths: list[Path]) -> list[_PathSnapshot]:
    snapshots: list[_PathSnapshot] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        snapshots.append(_snapshot_path(path))
    return snapshots


def _snapshot_matches(snapshot: _PathSnapshot) -> bool:
    path = snapshot.path
    try:
        if snapshot.kind == "absent":
            return not _lexists(path)
        if not _lexists(path):
            return False
        info = path.lstat()
        if info.st_uid != os.getuid():
            return False
        if snapshot.kind == "symlink":
            return stat.S_ISLNK(info.st_mode) and os.readlink(path) == snapshot.payload
        if snapshot.kind == "file":
            return (
                stat.S_ISREG(info.st_mode)
                and path.read_bytes() == snapshot.payload
                and stat.S_IMODE(info.st_mode) == snapshot.mode
            )
    except OSError:
        return False
    return False


def _remove_snapshot_leaf(path: Path) -> None:
    if not _lexists(path):
        return
    info = path.lstat()
    if info.st_uid != os.getuid():
        raise SetupError(
            f"refusing rollback of path owned by uid {info.st_uid}: {path}"
        )
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise SetupError(
            f"refusing rollback over {_path_kind(info)} at managed path: {path}"
        )
    path.unlink()


def _restore_snapshot(snapshot: _PathSnapshot) -> None:
    path = snapshot.path
    if snapshot.kind == "absent":
        _remove_snapshot_leaf(path)
        return
    if _lexists(path):
        info = path.lstat()
        expected_kind = (
            stat.S_ISLNK(info.st_mode)
            if snapshot.kind == "symlink"
            else stat.S_ISREG(info.st_mode)
        )
        if not expected_kind:
            _remove_snapshot_leaf(path)
    if snapshot.kind == "file":
        assert isinstance(snapshot.payload, bytes)
        assert isinstance(snapshot.mode, int)
        _atomic_rewrite_owned(path, snapshot.payload, snapshot.mode)
        return
    assert snapshot.kind == "symlink"
    assert isinstance(snapshot.payload, str)
    if _lexists(path):
        if path.is_symlink() and os.readlink(path) == snapshot.payload:
            return
        _remove_snapshot_leaf(path)
    _create_symlink(path, Path(snapshot.payload))


def _rollback_snapshots(
    snapshots: list[_PathSnapshot],
) -> tuple[bool, bool, tuple[str, ...]]:
    """Return (changed, fully_restored, rollback_errors)."""

    changed = any(not _snapshot_matches(snapshot) for snapshot in snapshots)
    errors: list[str] = []
    if changed:
        for snapshot in reversed(snapshots):
            if _snapshot_matches(snapshot):
                continue
            try:
                _restore_snapshot(snapshot)
            except Exception as error:  # Best effort must continue across paths.
                errors.append(f"{snapshot.path}: {error}")
    fully_restored = all(_snapshot_matches(snapshot) for snapshot in snapshots)
    return changed, fully_restored, tuple(errors)


def _load_json_config(path: Path) -> tuple[dict[str, Any], bytes | None, int]:
    raw = _read_regular(path)
    if raw is None:
        return {}, None, 0o600
    payload, mode = raw
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid JSON configuration at {path}: {error}") from error
    if not isinstance(value, dict):
        raise SetupError(f"configuration root must be an object: {path}")
    return value, payload, mode


def _load_yaml_config(path: Path) -> tuple[dict[str, Any], bytes | None, int]:
    raw = _read_regular(path)
    if raw is None:
        return {}, None, 0o600
    payload, mode = raw
    try:
        value = yaml.safe_load(payload.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise SetupError(f"invalid YAML configuration at {path}: {error}") from error
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise SetupError(f"configuration root must be a mapping: {path}")
    return value, payload, mode


def _yaml_mapping_entries(
    node: yaml.MappingNode,
    *,
    context: str,
) -> dict[str, yaml.Node]:
    entries: dict[str, yaml.Node] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or not isinstance(
            key_node.value, str
        ):
            raise SetupError(f"{context} contains a non-scalar mapping key")
        if key_node.value in entries:
            raise SetupError(f"{context} contains duplicate key {key_node.value!r}")
        entries[key_node.value] = value_node
    return entries


def _hermes_managed_insertion(
    path: Path,
    raw: bytes | None,
    value: dict[str, Any],
) -> tuple[bytes, str | None]:
    """Insert one marked YAML list item without reserializing the user file."""

    payload = b"" if raw is None else raw
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise SetupError(f"invalid UTF-8 YAML configuration at {path}: {error}") from error
    try:
        tokens = list(yaml.scan(text))
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        raise SetupError(f"invalid YAML configuration at {path}: {error}") from error
    rejected_tokens = (
        yaml.tokens.AnchorToken,
        yaml.tokens.AliasToken,
        yaml.tokens.TagToken,
    )
    if any(isinstance(token, rejected_tokens) for token in tokens):
        raise SetupError(
            f"Hermes config uses YAML anchors, aliases, or tags that cannot be "
            f"edited ownership-safely: {path}"
        )
    if root is None:
        root = yaml.MappingNode(
            tag="tag:yaml.org,2002:map",
            value=[],
            start_mark=None,
            end_mark=None,
            flow_style=False,
        )
    if not isinstance(root, yaml.MappingNode) or root.flow_style:
        raise SetupError(
            f"Hermes config must use a block-style top-level mapping: {path}"
        )
    root_entries = _yaml_mapping_entries(root, context="Hermes config")

    plugins_value = value.get("plugins")
    enabled_value = (
        plugins_value.get("enabled") if isinstance(plugins_value, dict) else None
    )
    if isinstance(enabled_value, list) and "pneu" in enabled_value:
        return payload, None

    plugins_node = root_entries.get("plugins")
    if plugins_node is None:
        index = len(text)
        indent = ""
        body = (
            "# >>> pneu managed: hermes plugin\n"
            "plugins:\n"
            "  enabled:\n"
            "    - pneu\n"
            "# <<< pneu managed: hermes plugin\n"
        )
    else:
        if not isinstance(plugins_node, yaml.MappingNode) or plugins_node.flow_style:
            raise SetupError(
                f"Hermes plugins must use a block-style mapping for safe setup: {path}"
            )
        plugin_entries = _yaml_mapping_entries(
            plugins_node,
            context="Hermes plugins",
        )
        enabled_node = plugin_entries.get("enabled")
        if enabled_node is None:
            index = plugins_node.end_mark.index
            indent = " " * plugins_node.start_mark.column
            body = (
                f"{indent}# >>> pneu managed: hermes plugin\n"
                f"{indent}enabled:\n"
                f"{indent}  - pneu\n"
                f"{indent}# <<< pneu managed: hermes plugin\n"
            )
        else:
            if not isinstance(enabled_node, yaml.SequenceNode) or enabled_node.flow_style:
                raise SetupError(
                    f"Hermes plugins.enabled must use a block-style list for "
                    f"safe setup: {path}"
                )
            index = enabled_node.end_mark.index
            indent = " " * enabled_node.start_mark.column
            body = (
                f"{indent}# >>> pneu managed: hermes plugin\n"
                f"{indent}- pneu\n"
                f"{indent}# <<< pneu managed: hermes plugin\n"
            )

    separator = "" if index == 0 or text[index - 1] == "\n" else "\n"
    fragment = separator + body
    updated = (text[:index] + fragment + text[index:]).encode("utf-8")
    return updated, fragment


def _backup_path(prefix: Path, label: str, payload: bytes) -> Path:
    return (
        prefix
        / "backups"
        / "harness-setup"
        / f"{label}.{_digest(payload)[:16]}.bak"
    )


def _backup(prefix: Path, label: str, payload: bytes) -> Path:
    path = _backup_path(prefix, label, payload)
    _ensure_private_dir(path.parent)
    if _lexists(path):
        existing = _read_regular(path)
        assert existing is not None
        if existing[0] != payload:
            raise SetupError(f"backup collision at {path}")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            os.chmod(path, 0o600)
        return path
    _atomic_rewrite_owned(path, payload, 0o600)
    return path


def _manifest_path(prefix: Path) -> Path:
    return prefix / "harness-setup.json"


def _codex_reload_marker_path(prefix: Path) -> Path:
    return prefix / ".runtime" / CODEX_RELOAD_MARKER_NAME


def _codex_reload_marker_value(
    prefix: Path,
    app_plist_path: Path,
    app_plist_digest: str,
) -> dict[str, str]:
    return {
        "schema": CODEX_RELOAD_MARKER_SCHEMA,
        "prefix": str(prefix),
        "label": "com.roundtable.codex-app-server",
        "appPlistPath": str(app_plist_path),
        "appPlistDigest": app_plist_digest,
    }


def _validate_codex_reload_marker(
    prefix: Path,
    app_plist_path: Path,
    app_plist_digest: str,
) -> None:
    """Fail closed on a present marker not owned by this exact app plist."""

    path = _codex_reload_marker_path(prefix)
    _validate_owned_chain(prefix, path.parent)
    raw = _read_regular(path)
    if raw is None:
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SetupError(f"unsafe Codex reload marker mode {mode:04o}: {path}")
    try:
        value = json.loads(raw[0])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid Codex reload marker at {path}: {error}") from error
    expected = _codex_reload_marker_value(
        prefix,
        app_plist_path,
        app_plist_digest,
    )
    if value != expected:
        raise SetupError(
            "Codex reload marker does not match the managed app-server plist: "
            f"{path}"
        )


def _load_manifest(prefix: Path, home: Path) -> dict[str, Any] | None:
    path = _manifest_path(prefix)
    raw = _read_regular(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw[0])
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SetupError(f"invalid harness setup manifest at {path}: {error}") from error
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise SetupError(f"refusing unknown harness setup manifest at {path}")
    if value.get("prefix") != str(prefix) or value.get("home") != str(home):
        raise SetupError(f"harness setup manifest path scope mismatch at {path}")
    harnesses = value.get("harnesses")
    if not isinstance(harnesses, dict):
        raise SetupError(f"invalid harness ownership entries at {path}")
    return value


def _unknown_harnesses(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    return sorted(
        harness for harness in manifest["harnesses"] if harness not in HARNESSES
    )


def _report_unknown_harnesses(
    result: dict[str, Any], manifest: dict[str, Any] | None
) -> None:
    unknown_harnesses = _unknown_harnesses(manifest)
    if unknown_harnesses:
        result["unknown_harnesses"] = unknown_harnesses


def _new_manifest(prefix: Path, home: Path) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "prefix": str(prefix),
        "home": str(home),
        "harnesses": {},
    }


def _write_manifest(prefix: Path, manifest: dict[str, Any]) -> None:
    _atomic_rewrite_owned(_manifest_path(prefix), _json_bytes(manifest), 0o600)


def _claude_groups(prefix: Path) -> dict[str, dict[str, Any]]:
    session_waiter = {
        "type": "command",
        "command": str(prefix / "bin" / "rt-wait-inbox"),
        "args": ["--claude-hook"],
        "asyncRewake": True,
        "timeout": CLAUDE_HOOK_TIMEOUT_SECONDS,
    }
    return {
        "SessionStart": {
            "matcher": "startup|resume|clear|compact",
            "hooks": [session_waiter],
        },
        "Stop": {
            "hooks": [
                {
                    **session_waiter,
                    "args": ["--claude-stop-hook"],
                }
            ],
        },
    }


def _legacy_claude_groups(prefix: Path) -> dict[str, tuple[dict[str, Any], ...]]:
    """Known setup-owned hook generations that may be upgraded in place."""

    return {
        "Stop": (
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": str(prefix / "bin" / "rt-stop-gate"),
                        "args": [],
                    }
                ],
            },
        ),
    }


def _claude_permission_rules(prefix: Path) -> tuple[str, ...]:
    """Narrow, stable commands needed for an unattended mail-drain turn."""

    rendered_prefix = str(prefix)
    unsafe = set("*?[](){}'\"\\$`;|&<>")
    if any(character.isspace() or character in unsafe for character in rendered_prefix):
        raise SetupError(
            "Claude integration requires a shell-safe pneu install prefix "
            f"for exact permission rules; unsupported prefix: {rendered_prefix!r}"
        )
    command_root = prefix / "bin"
    return (
        f"Bash({command_root / 'rt-inbox'} --fenced "
        "--archive-quiet-acks -f json)",
        f"Bash({command_root / 'rt-ack'} --fenced *)",
        f"Bash({command_root / 'rt-say'} --fenced --no-nudge *)",
    )


def _claude_permission_conflicts(
    permissions: dict[str, Any],
    expected_rules: tuple[str, ...],
) -> list[tuple[str, str]]:
    conflicts: list[tuple[str, str]] = []
    for policy in ("deny", "ask"):
        values = permissions.get(policy, [])
        if not isinstance(values, list) or any(
            not isinstance(item, str) for item in values
        ):
            raise SetupError(f"Claude permissions.{policy} must be a string list")
        for rule in values:
            pattern = rule
            if pattern.endswith(":*)"):
                pattern = pattern[:-3] + " *)"
            bare_matches_bash = "(" not in pattern and fnmatch.fnmatchcase(
                "Bash", pattern
            )
            if bare_matches_bash or any(
                fnmatch.fnmatchcase(expected, pattern)
                for expected in expected_rules
            ):
                conflicts.append((policy, rule))
    return conflicts


def _codex_groups(prefix: Path) -> dict[str, dict[str, Any]]:
    """Return the one lifecycle hook pneu owns in Codex.

    The hook only records a fenced bind request.  The wake bridge performs the
    app-server identity validation later, outside the SessionStart callback.
    """

    return {
        "SessionStart": {
            "matcher": "startup|resume|clear",
            "hooks": [
                {
                    "type": "command",
                    "command": str(prefix / "bin" / "rt-codex-session-start"),
                    "timeout": CODEX_HOOK_TIMEOUT_SECONDS,
                }
            ],
        }
    }


def _uses_ambient_harness_paths(home: Path) -> bool:
    configured_home = os.environ.get("HOME")
    return configured_home is not None and _absolute(configured_home) == home


def _selected_codex_home(home: Path) -> Path:
    configured = (
        os.environ.get("CODEX_HOME")
        if _uses_ambient_harness_paths(home)
        else None
    )
    selected = _absolute(configured) if configured else home / ".codex"
    try:
        selected.relative_to(home)
    except ValueError as error:
        raise SetupError(
            f"CODEX_HOME must stay under the selected home {home}: {selected}"
        ) from error
    return selected


def _selected_runtime(home: Path, prefix: Path) -> Path:
    if not _uses_ambient_harness_paths(home):
        return prefix / ".runtime"
    generic = os.environ.get("RT_RUNTIME_DIR")
    legacy = os.environ.get("RT_CODEX_RUNTIME_DIR")

    def selected(value: str | None, label: str) -> Path | None:
        if not value:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            raise SetupError(f"{label} must resolve to an absolute path: {value!r}")
        return Path(os.path.normpath(str(candidate)))

    generic_path = selected(generic, "RT_RUNTIME_DIR")
    legacy_path = selected(legacy, "RT_CODEX_RUNTIME_DIR")
    if (
        generic_path is not None
        and legacy_path is not None
        and generic_path != legacy_path
    ):
        raise SetupError(
            "RT_RUNTIME_DIR and RT_CODEX_RUNTIME_DIR select different runtime "
            f"roots: {generic_path} != {legacy_path}"
        )
    return generic_path or legacy_path or prefix / ".runtime"


def _skill_path(home: Path, harness: str) -> Path:
    if harness == "codex":
        return _selected_codex_home(home) / "skills" / "pneu"
    return home / f".{harness}" / "skills" / "pneu"


def _skill_target(prefix: Path) -> Path:
    return prefix / "skills" / "shared" / "pneu"


def _hermes_plugin_path(home: Path) -> Path:
    return home / ".hermes" / "plugins" / "pneu"


def _hermes_plugin_target(prefix: Path) -> Path:
    return (
        prefix
        / "current"
        / "share"
        / "pneu"
        / "integrations"
        / "hermes"
        / "pneu"
    )


def _link_plan(home: Path, path: Path, target: Path) -> dict[str, Any]:
    _validate_user_chain(home, path.parent)
    if _lexists(path):
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise SetupError(f"refusing foreign-owned link path: {path}")
        if not stat.S_ISLNK(info.st_mode):
            raise SetupError(f"refusing non-symlink at managed link path: {path}")
        actual = os.readlink(path)
        if actual != str(target):
            raise SetupError(
                f"managed link collision at {path}: {actual!r} != {str(target)!r}"
            )
        return {
            "path": str(path),
            "target": str(target),
            "added": False,
        }
    return {
        "path": str(path),
        "target": str(target),
        "added": True,
    }


def _validate_source(target: Path, description: str) -> None:
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SetupError(f"{description} is missing at {target}") from error
    if not resolved.is_dir():
        raise SetupError(f"{description} is not a directory: {target}")


def _validate_executable(target: Path, description: str) -> None:
    try:
        resolved = target.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SetupError(f"{description} is missing at {target}") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise SetupError(f"{description} is not executable: {target}")


def _prepare_claude(
    home: Path,
    prefix: Path,
    existing_record: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = home / ".claude" / "settings.json"
    _validate_user_chain(home, path)
    before, raw, mode = _load_json_config(path)
    after = copy.deepcopy(before)
    if after.get("disableAllHooks") is True:
        raise SetupError(
            "Claude disableAllHooks=true prevents pneu wake hooks; "
            "review that user choice with /hooks before applying setup"
        )
    prior_config = (
        existing_record.get("config")
        if isinstance(existing_record, dict)
        else None
    )
    if existing_record is not None and not isinstance(prior_config, dict):
        raise SetupError("invalid Claude configuration ownership record")

    hooks_added = (
        bool(prior_config["hooks_container_added"])
        if prior_config is not None
        else "hooks" not in after
    )
    if "hooks" not in after:
        after["hooks"] = {}
    hooks = after.get("hooks")
    if not isinstance(hooks, dict):
        raise SetupError(f"Claude hooks must be an object: {path}")

    fragments: list[dict[str, Any]] = []
    event_added: dict[str, bool] = (
        copy.deepcopy(prior_config["event_containers_added"])
        if prior_config is not None
        else {}
    )
    prior_fragments = {
        fragment.get("event"): fragment
        for fragment in (prior_config or {}).get("fragments", [])
        if isinstance(fragment, dict)
    }
    for event, group in _claude_groups(prefix).items():
        if prior_config is None:
            event_added[event] = event not in hooks
        if event not in hooks:
            hooks[event] = []
        groups = hooks.get(event)
        if not isinstance(groups, list):
            raise SetupError(f"Claude hook event {event} must be a list: {path}")
        prior_fragment = prior_fragments.get(event)
        if prior_fragment is None:
            count = groups.count(group)
            if count > 1:
                raise SetupError(
                    f"Claude hook event {event} contains duplicate pneu "
                    f"fragments: {path}"
                )
            added = count == 0
            if added:
                groups.append(copy.deepcopy(group))
        else:
            prior_group = prior_fragment["group"]
            added = bool(prior_fragment["added"])
            if prior_group != group:
                if prior_group not in _legacy_claude_groups(prefix).get(event, ()):
                    raise SetupError(
                        f"unsupported managed Claude {event} hook generation"
                    )
                if not added:
                    raise SetupError(
                        f"cannot replace pre-existing legacy Claude {event} "
                        "hook; first remove pneu setup, then remove that "
                        "user-owned fragment, and apply setup again"
                    )
                if groups.count(group):
                    raise SetupError(
                        f"Claude hook event {event} already contains the new "
                        f"pneu fragment: {path}"
                    )
                index = groups.index(prior_group)
                groups[index] = copy.deepcopy(group)
        fragments.append(
            {
                "event": event,
                "group": copy.deepcopy(group),
                "added": added,
            }
        )

    prior_permissions = (
        prior_config.get("permissions") if prior_config is not None else None
    )
    if prior_permissions is not None and not isinstance(prior_permissions, dict):
        raise SetupError("invalid Claude permission ownership record")
    permissions_container_added = (
        bool(prior_permissions["permissions_container_added"])
        if prior_permissions is not None
        else "permissions" not in after
    )
    if "permissions" not in after:
        after["permissions"] = {}
    permissions = after.get("permissions")
    if not isinstance(permissions, dict):
        raise SetupError(f"Claude permissions must be an object: {path}")
    expected_permission_rules = _claude_permission_rules(prefix)
    conflicts = _claude_permission_conflicts(
        permissions,
        expected_permission_rules,
    )
    if conflicts:
        rendered = ", ".join(
            f"permissions.{policy}={rule!r}" for policy, rule in conflicts
        )
        raise SetupError(
            "Claude ask/deny rules override pneu's narrow unattended "
            f"mail permissions: {rendered}; review them with /permissions "
            "before applying setup"
        )
    allow_container_added = (
        bool(prior_permissions["allow_container_added"])
        if prior_permissions is not None
        else "allow" not in permissions
    )
    if "allow" not in permissions:
        permissions["allow"] = []
    allow = permissions.get("allow")
    if not isinstance(allow, list) or any(
        not isinstance(item, str) for item in allow
    ):
        raise SetupError(f"Claude permissions.allow must be a string list: {path}")
    prior_rules = {
        item.get("rule"): item
        for item in (prior_permissions or {}).get("rules", [])
        if isinstance(item, dict)
    }
    permission_rules: list[dict[str, Any]] = []
    permission_rules_added: list[str] = []
    for rule in expected_permission_rules:
        count = allow.count(rule)
        if count > 1:
            raise SetupError(
                f"Claude permissions.allow contains duplicate pneu rule "
                f"{rule!r}: {path}"
            )
        prior_rule = prior_rules.get(rule)
        if prior_rule is not None:
            added = bool(prior_rule["added"])
        else:
            added = count == 0
            if added:
                allow.append(rule)
                permission_rules_added.append(rule)
        permission_rules.append({"rule": rule, "added": added})

    record = {
        "config": {
            "path": str(path),
            "created": (
                bool(prior_config["created"])
                if prior_config is not None
                else raw is None
            ),
            "backup": (
                prior_config.get("backup") if prior_config is not None else None
            ),
            "hooks_container_added": hooks_added,
            "event_containers_added": event_added,
            "fragments": fragments,
            "permissions": {
                "permissions_container_added": permissions_container_added,
                "allow_container_added": allow_container_added,
                "rules": permission_rules,
            },
        },
        "skill": (
            copy.deepcopy(existing_record["skill"])
            if existing_record is not None
            else _link_plan(
                home,
                _skill_path(home, "claude"),
                _skill_target(prefix),
            )
        ),
    }
    operation = {
        "path": path,
        "before": raw,
        "after": _json_bytes(after),
        "mode": mode,
        "changed": after != before,
        "backup_label": "claude-settings.json",
        "existing": existing_record is not None,
        "permission_rules_added": permission_rules_added,
    }
    return record, operation


def _prepare_hermes(home: Path, prefix: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    path = home / ".hermes" / "config.yaml"
    _validate_user_chain(home, path)
    before, raw, mode = _load_yaml_config(path)
    plugins = before.get("plugins")
    if plugins is None:
        plugins = {}
    if not isinstance(plugins, dict):
        raise SetupError(f"Hermes plugins must be a mapping: {path}")

    disabled = plugins.get("disabled", [])
    if not isinstance(disabled, list):
        raise SetupError(f"Hermes plugins.disabled must be a list: {path}")
    if "pneu" in disabled:
        raise SetupError(
            f"Hermes plugin 'pneu' is explicitly disabled in {path}; "
            "remove that user choice before applying setup"
        )

    enabled = plugins.get("enabled", [])
    if not isinstance(enabled, list):
        raise SetupError(f"Hermes plugins.enabled must be a list: {path}")
    enabled_count = enabled.count("pneu")
    if enabled_count > 1:
        raise SetupError(
            f"Hermes plugins.enabled contains duplicate 'pneu' entries: {path}"
        )
    enabled_added = enabled_count == 0
    after_payload, managed_fragment = _hermes_managed_insertion(
        path,
        raw,
        before,
    )
    if enabled_added != (managed_fragment is not None):
        raise SetupError(f"cannot establish exact Hermes plugin ownership at {path}")

    record = {
        "config": {
            "path": str(path),
            "created": raw is None,
            "backup": None,
            "enabled_added": enabled_added,
            "managed_fragment": managed_fragment,
        },
        "plugin": _link_plan(
            home,
            _hermes_plugin_path(home),
            _hermes_plugin_target(prefix),
        ),
        "skill": _link_plan(home, _skill_path(home, "hermes"), _skill_target(prefix)),
    }
    operation = {
        "path": path,
        "before": raw,
        "after": after_payload,
        "mode": mode,
        "changed": managed_fragment is not None,
        "backup_label": "hermes-config.yaml",
    }
    return record, operation


@contextlib.contextmanager
def _codex_context(home: Path, prefix: Path) -> Iterator[Any]:
    if _rtcodex is None:
        raise SetupError("pneu Codex support module is not installed")
    module = _rtcodex
    names = (
        "INSTALL_PREFIX",
        "ROUND_ROOT",
        "RUNTIME_DIR",
        "CODEX_HOME",
        "DEFAULT_SOCKET",
    )
    saved_attributes = {name: getattr(module, name) for name in names}
    env_names = (
        "HOME",
        "ROUNDTABLE_INSTALL_PREFIX",
        "RT_RUNTIME_DIR",
        "RT_CODEX_RUNTIME_DIR",
        "CODEX_HOME",
    )
    saved_environment = {name: os.environ.get(name) for name in env_names}
    runtime = _selected_runtime(home, prefix)
    codex_home = _selected_codex_home(home)
    socket = codex_home / "app-server-control" / "app-server-control.sock"
    try:
        os.environ.update(
            {
                "HOME": str(home),
                "ROUNDTABLE_INSTALL_PREFIX": str(prefix),
                "RT_RUNTIME_DIR": str(runtime),
                "RT_CODEX_RUNTIME_DIR": str(runtime),
                "CODEX_HOME": str(codex_home),
            }
        )
        module.INSTALL_PREFIX = str(prefix)
        module.ROUND_ROOT = prefix / "current"
        module.RUNTIME_DIR = runtime
        module.CODEX_HOME = codex_home
        module.DEFAULT_SOCKET = socket
        yield module
    finally:
        for name, value in saved_attributes.items():
            setattr(module, name, value)
        for name, value in saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _call_plist_builder(
    builder: Any,
    socket: Path,
    *,
    ensure_runtime: bool,
) -> dict[str, Any]:
    try:
        value = builder(socket, ensure_runtime=ensure_runtime)
    except TypeError as error:
        # Older supported artifacts did not expose the no-write switch.  A
        # fallback is safe only during ``apply``, where runtime creation is
        # already authorized.
        if not ensure_runtime or "ensure_runtime" not in str(error):
            raise
        value = builder(socket)
    if not isinstance(value, dict):
        raise SetupError("Codex plist builder returned a non-dictionary payload")
    return value


def _daemon_join_builder(module: Any) -> Any:
    builder = getattr(module, "daemon_join_plist", None)
    if builder is None:
        raise SetupError(
            "the installed pneu Codex module predates the canonical-daemon "
            "join agent; reinstall pneu so setup and its Codex module match"
        )
    return builder


def _codex_payloads(
    home: Path,
    prefix: Path,
    *,
    ensure_runtime: bool,
) -> dict[str, dict[str, Any]]:
    socket = (
        _selected_codex_home(home)
        / "app-server-control"
        / "app-server-control.sock"
    )
    try:
        with _codex_context(home, prefix) as module:
            return {
                "com.roundtable.codex-app-server": _call_plist_builder(
                    module.app_server_plist,
                    socket,
                    ensure_runtime=ensure_runtime,
                ),
                "com.roundtable.codex-wake": _call_plist_builder(
                    module.wake_plist,
                    socket,
                    ensure_runtime=ensure_runtime,
                ),
                CODEX_DAEMON_JOIN_LABEL: _call_plist_builder(
                    _daemon_join_builder(module),
                    socket,
                    ensure_runtime=ensure_runtime,
                ),
            }
    except SetupError:
        raise
    except Exception as error:
        raise SetupError(f"cannot build Codex LaunchAgent configuration: {error}") from error


def _require_validated_codex_release(home: Path, prefix: Path) -> None:
    """Gate on the version floor before writing Codex service definitions.

    Setup never probes the daemon and never starts one; the live protocol
    probe that proves any at-or-above-floor release runs later, in the launch
    preflight and the wake bridge, once a daemon has answered.  The checker is
    called only for its fail-closed side effect, so this stays compatible with
    both the current floor+parse gate and any older module signature.
    """
    try:
        with _codex_context(home, prefix) as module:
            checker = getattr(module, "require_supported_version", None)
            if checker is None:
                # Older supported artifacts expose only the legacy shim; call
                # it so a mixed-version upgrade still gates on their policy.
                module.require_validated_version()
            else:
                checker()
    except SetupError:
        raise
    except Exception as error:
        raise SetupError(f"cannot validate Codex CLI release: {error}") from error


def _prepare_codex_config(
    home: Path,
    prefix: Path,
    existing_record: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    path = _selected_codex_home(home) / "hooks.json"
    _validate_user_chain(home, path)
    before, raw, mode = _load_json_config(path)

    if existing_record is not None and isinstance(
        existing_record.get("config"), dict
    ):
        # The owned fragment was validated against the current file before
        # preparation.  Keep its original creation/ownership metadata.
        return copy.deepcopy(existing_record["config"]), {
            "path": path,
            "before": raw,
            "after": raw,
            "mode": mode,
            "changed": False,
            "backup_label": "codex-hooks.json",
        }

    after = copy.deepcopy(before)
    hooks_added = "hooks" not in after
    if hooks_added:
        after["hooks"] = {}
    hooks = after.get("hooks")
    if not isinstance(hooks, dict):
        raise SetupError(f"Codex hooks must be an object: {path}")

    event = "SessionStart"
    event_added = event not in hooks
    if event_added:
        hooks[event] = []
    groups = hooks.get(event)
    if not isinstance(groups, list):
        raise SetupError(f"Codex hook event {event} must be a list: {path}")
    group = _codex_groups(prefix)[event]
    count = groups.count(group)
    if count > 1:
        raise SetupError(
            f"Codex hook event {event} contains duplicate pneu "
            f"fragments: {path}"
        )
    added = count == 0
    if added:
        groups.append(copy.deepcopy(group))

    config = {
        "path": str(path),
        "created": raw is None,
        "backup": None,
        "hooks_container_added": hooks_added,
        "event_containers_added": {event: event_added},
        "fragments": [
            {
                "event": event,
                "group": group,
                "added": added,
            }
        ],
    }
    return config, {
        "path": path,
        "before": raw,
        "after": _json_bytes(after),
        "mode": mode,
        "changed": after != before,
        "backup_label": "codex-hooks.json",
    }


def _prepare_codex(
    home: Path,
    prefix: Path,
    *,
    ensure_runtime: bool,
    existing_record: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    launch_agents = home / "Library" / "LaunchAgents"
    _validate_user_chain(home, launch_agents)
    _require_validated_codex_release(home, prefix)
    payloads = _codex_payloads(home, prefix, ensure_runtime=ensure_runtime)
    existing_by_label = {
        item.get("label"): item
        for item in (existing_record or {}).get("plists", [])
        if isinstance(item, dict)
    }
    plists: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    for label in CODEX_LABELS:
        path = launch_agents / f"{label}.plist"
        _validate_user_chain(home, path)
        payload = plistlib.dumps(payloads[label], fmt=plistlib.FMT_XML, sort_keys=True)
        raw = _read_regular(path)
        previous = existing_by_label.get(label)
        if previous is None and raw is not None and raw[0] != payload:
            raise SetupError(
                f"refusing to replace non-owned Codex LaunchAgent plist: {path}"
            )
        if previous is not None and raw is None:
            raise SetupError(f"managed Codex LaunchAgent drift: missing {path}")
        added = raw is None if previous is None else bool(previous["added"])
        content_changed = raw is None or raw[0] != payload
        mode_changed = raw is not None and raw[1] != 0o600
        changed = content_changed or mode_changed
        plists.append(
            {
                "label": label,
                "path": str(path),
                "digest": _digest(payload),
                "added": added,
            }
        )
        writes.append(
            {
                "path": path,
                "payload": payload,
                "added": added,
                "changed": changed,
                "content_changed": content_changed,
                "mode_changed": mode_changed,
                "existing": previous is not None,
            }
        )
    app_record = next(
        item
        for item in plists
        if item["label"] == "com.roundtable.codex-app-server"
    )
    app_write = next(
        item
        for item in writes
        if Path(item["path"]).stem == "com.roundtable.codex-app-server"
    )
    marker_path = _codex_reload_marker_path(prefix)
    if existing_record is None and _lexists(marker_path):
        raise SetupError(
            f"refusing unowned Codex reload marker at {marker_path}"
        )
    marker_payload = _json_bytes(
        _codex_reload_marker_value(
            prefix,
            Path(app_record["path"]),
            app_record["digest"],
        )
    )
    config, config_operation = _prepare_codex_config(
        home,
        prefix,
        existing_record,
    )
    skill = (
        copy.deepcopy(existing_record["skill"])
        if existing_record is not None
        else _link_plan(home, _skill_path(home, "codex"), _skill_target(prefix))
    )
    return (
        {
            "config": config,
            "plists": plists,
            "skill": skill,
            # The join switch is an owned change like any other: it appears in
            # the preview, is recorded here, and is undone before the daemon
            # it points at is removed.
            "daemon_join": {
                "label": CODEX_DAEMON_JOIN_LABEL,
                "variable": CODEX_DAEMON_JOIN_VARIABLE,
                "value": CODEX_DAEMON_JOIN_VALUE,
            },
        },
        {
            "config": config_operation,
            "plists": writes,
            "daemon_join": {
                "variable": CODEX_DAEMON_JOIN_VARIABLE,
                "value": CODEX_DAEMON_JOIN_VALUE,
                "changed": (existing_record or {}).get("daemon_join") is None,
            },
            "reload_marker": {
                "path": marker_path,
                "payload": marker_payload,
                # launchd need not reload when only the plist permissions are
                # tightened.  The atomic rewrite below still normalizes the
                # managed leaf before setup reports success.
                "changed": app_write["content_changed"],
            },
            "existing": existing_record is not None,
        },
    )


def _prepare(
    harness: str,
    home: Path,
    prefix: Path,
    *,
    ensure_runtime: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if harness == "claude":
        return _prepare_claude(home, prefix)
    if harness == "hermes":
        return _prepare_hermes(home, prefix)
    if harness == "codex":
        return _prepare_codex(home, prefix, ensure_runtime=ensure_runtime)
    raise SetupError(f"unsupported harness: {harness}")


def _validate_link_record(
    home: Path,
    record: dict[str, Any],
    expected_path: Path,
    expected_target: Path,
    *,
    require_present: bool,
) -> None:
    if (
        record.get("path") != str(expected_path)
        or record.get("target") != str(expected_target)
        or not isinstance(record.get("added"), bool)
    ):
        raise SetupError(f"invalid managed link ownership record for {expected_path}")
    _validate_user_chain(home, expected_path.parent)
    if not require_present:
        return
    if not _lexists(expected_path):
        raise SetupError(f"managed link drift: missing {expected_path}")
    info = expected_path.lstat()
    if (
        info.st_uid != os.getuid()
        or not stat.S_ISLNK(info.st_mode)
        or os.readlink(expected_path) != str(expected_target)
    ):
        raise SetupError(f"managed link drift at {expected_path}")


def _validate_claude(
    home: Path,
    prefix: Path,
    record: dict[str, Any],
    *,
    added_only: bool,
) -> None:
    expected_path = home / ".claude" / "settings.json"
    config = record.get("config")
    if not isinstance(config, dict) or config.get("path") != str(expected_path):
        raise SetupError("invalid Claude configuration ownership record")
    if (
        not isinstance(config.get("created"), bool)
        or not isinstance(config.get("hooks_container_added"), bool)
        or not isinstance(config.get("event_containers_added"), dict)
    ):
        raise SetupError("invalid Claude configuration ownership metadata")
    value, _raw, _mode = _load_json_config(expected_path)
    hooks = value.get("hooks")
    fragments = config.get("fragments")
    if not isinstance(fragments, list):
        raise SetupError("invalid Claude hook ownership record")
    expected_groups = _claude_groups(prefix)
    legacy_groups = _legacy_claude_groups(prefix)
    seen_events: set[str] = set()
    for fragment in fragments:
        if not isinstance(fragment, dict):
            raise SetupError("invalid Claude hook fragment record")
        event = fragment.get("event")
        group = fragment.get("group")
        added = fragment.get("added")
        if (
            event not in expected_groups
            or event in seen_events
            or (
                group != expected_groups[event]
                and group not in legacy_groups.get(event, ())
            )
            or not isinstance(added, bool)
            or not isinstance(
                config["event_containers_added"].get(event),
                bool,
            )
        ):
            raise SetupError("invalid Claude hook fragment ownership")
        seen_events.add(event)
        if added_only and not added:
            continue
        groups = hooks.get(event) if isinstance(hooks, dict) else None
        count = groups.count(group) if isinstance(groups, list) else 0
        if count != 1:
            raise SetupError(
                f"managed Claude {event} hook drift: expected exactly one owned fragment"
            )
    if seen_events != set(expected_groups):
        raise SetupError("invalid Claude hook fragment ownership")

    permissions_record = config.get("permissions")
    if permissions_record is not None:
        if (
            not isinstance(permissions_record, dict)
            or not isinstance(
                permissions_record.get("permissions_container_added"), bool
            )
            or not isinstance(
                permissions_record.get("allow_container_added"), bool
            )
            or not isinstance(permissions_record.get("rules"), list)
        ):
            raise SetupError("invalid Claude permission ownership record")
        expected_rules = set(_claude_permission_rules(prefix))
        seen_rules: set[str] = set()
        permissions = value.get("permissions")
        allow = permissions.get("allow") if isinstance(permissions, dict) else None
        for item in permissions_record["rules"]:
            if not isinstance(item, dict):
                raise SetupError("invalid Claude permission rule ownership")
            rule = item.get("rule")
            added = item.get("added")
            if (
                not isinstance(rule, str)
                or rule not in expected_rules
                or rule in seen_rules
                or not isinstance(added, bool)
            ):
                raise SetupError("invalid Claude permission rule ownership")
            seen_rules.add(rule)
            if added_only and not added:
                continue
            count = allow.count(rule) if isinstance(allow, list) else 0
            if count != 1:
                raise SetupError(
                    "managed Claude permission drift: expected exactly one "
                    f"owned rule {rule!r}"
                )
        if seen_rules != expected_rules:
            raise SetupError("invalid Claude permission rule ownership")
    skill = record.get("skill")
    if not isinstance(skill, dict):
        raise SetupError("invalid Claude skill ownership record")
    _validate_link_record(
        home,
        skill,
        _skill_path(home, "claude"),
        _skill_target(prefix),
        require_present=not added_only or bool(skill.get("added")),
    )


def _validate_hermes(
    home: Path,
    prefix: Path,
    record: dict[str, Any],
    *,
    added_only: bool,
) -> None:
    expected_path = home / ".hermes" / "config.yaml"
    config = record.get("config")
    if not isinstance(config, dict) or config.get("path") != str(expected_path):
        raise SetupError("invalid Hermes configuration ownership record")
    if not isinstance(config.get("created"), bool):
        raise SetupError("invalid Hermes configuration ownership metadata")
    value, raw, _mode = _load_yaml_config(expected_path)
    plugins = value.get("plugins")
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    disabled = plugins.get("disabled", []) if isinstance(plugins, dict) else []
    added = config.get("enabled_added")
    if not isinstance(added, bool):
        raise SetupError("invalid Hermes plugin ownership record")
    if not added_only or added:
        if not isinstance(enabled, list) or enabled.count("pneu") != 1:
            raise SetupError(
                "managed Hermes plugin drift: expected exactly one enabled entry"
            )
        fragment = config.get("managed_fragment")
        if added:
            if not isinstance(fragment, str) or raw is None:
                raise SetupError("invalid Hermes managed block ownership record")
            try:
                rendered = raw.decode("utf-8")
            except UnicodeError as error:
                raise SetupError(
                    f"managed Hermes configuration is not UTF-8: {expected_path}"
                ) from error
            if rendered.count(fragment) != 1:
                raise SetupError(
                    "managed Hermes plugin drift: exact marked block is missing"
                )
        elif fragment is not None:
            raise SetupError("invalid pre-existing Hermes plugin ownership record")
    elif config.get("managed_fragment") is not None:
        raise SetupError("invalid pre-existing Hermes plugin ownership record")
    if isinstance(disabled, list) and "pneu" in disabled:
        raise SetupError("managed Hermes plugin drift: pneu is now disabled")
    for key, path, target in (
        (
            "plugin",
            _hermes_plugin_path(home),
            _hermes_plugin_target(prefix),
        ),
        ("skill", _skill_path(home, "hermes"), _skill_target(prefix)),
    ):
        link = record.get(key)
        if not isinstance(link, dict):
            raise SetupError(f"invalid Hermes {key} ownership record")
        _validate_link_record(
            home,
            link,
            path,
            target,
            require_present=not added_only or bool(link.get("added")),
        )


def _validate_codex(
    home: Path,
    prefix: Path,
    record: dict[str, Any],
    *,
    added_only: bool,
) -> None:
    plists = record.get("plists")
    if (
        not isinstance(plists, list)
        or not plists
        or len(plists) > len(CODEX_LABELS)
    ):
        raise SetupError("invalid Codex LaunchAgent ownership record")
    by_label = {
        item.get("label"): item for item in plists if isinstance(item, dict)
    }
    if set(by_label) - set(CODEX_LABELS):
        raise SetupError("unknown Codex LaunchAgent ownership record")
    for label in CODEX_LABELS:
        item = by_label.get(label)
        if item is None:
            # A manifest written before this label existed claims nothing
            # about it; the upgrade path adds it as a normal owned write.
            continue
        path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        if (
            not isinstance(item, dict)
            or item.get("path") != str(path)
            or not isinstance(item.get("digest"), str)
            or not isinstance(item.get("added"), bool)
        ):
            raise SetupError(f"invalid Codex plist ownership record for {label}")
        if added_only and not item["added"]:
            continue
        raw = _read_regular(path)
        if raw is None or _digest(raw[0]) != item["digest"]:
            raise SetupError(f"managed Codex LaunchAgent drift at {path}")
    app_record = by_label.get("com.roundtable.codex-app-server")
    if app_record is None:
        raise SetupError("invalid Codex LaunchAgent ownership record")
    _validate_codex_reload_marker(
        prefix,
        Path(app_record["path"]),
        app_record["digest"],
    )
    daemon_join = record.get("daemon_join")
    if daemon_join is not None and (
        not isinstance(daemon_join, dict)
        or daemon_join.get("variable") != CODEX_DAEMON_JOIN_VARIABLE
        or daemon_join.get("value") != CODEX_DAEMON_JOIN_VALUE
        or daemon_join.get("label") != CODEX_DAEMON_JOIN_LABEL
    ):
        raise SetupError("invalid Codex daemon-join ownership record")
    config = record.get("config")
    if config is not None:
        expected_path = _selected_codex_home(home) / "hooks.json"
        if not isinstance(config, dict) or config.get("path") != str(expected_path):
            raise SetupError("invalid Codex hook configuration ownership record")
        if (
            not isinstance(config.get("created"), bool)
            or not isinstance(config.get("hooks_container_added"), bool)
            or not isinstance(config.get("event_containers_added"), dict)
        ):
            raise SetupError("invalid Codex hook configuration ownership metadata")
        value, _raw, _mode = _load_json_config(expected_path)
        hooks = value.get("hooks")
        fragments = config.get("fragments")
        if not isinstance(fragments, list) or len(fragments) != 1:
            raise SetupError("invalid Codex hook fragment ownership record")
        expected_groups = _codex_groups(prefix)
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise SetupError("invalid Codex hook fragment ownership record")
            event = fragment.get("event")
            group = fragment.get("group")
            added = fragment.get("added")
            if (
                event not in expected_groups
                or group != expected_groups[event]
                or not isinstance(added, bool)
                or not isinstance(
                    config["event_containers_added"].get(event),
                    bool,
                )
            ):
                raise SetupError("invalid Codex hook fragment ownership")
            if added_only and not added:
                continue
            groups = hooks.get(event) if isinstance(hooks, dict) else None
            count = groups.count(group) if isinstance(groups, list) else 0
            if count != 1:
                raise SetupError(
                    f"managed Codex {event} hook drift: expected exactly one "
                    "owned fragment"
                )
    skill = record.get("skill")
    if not isinstance(skill, dict):
        raise SetupError("invalid Codex skill ownership record")
    _validate_link_record(
        home,
        skill,
        _skill_path(home, "codex"),
        _skill_target(prefix),
        require_present=not added_only or bool(skill.get("added")),
    )


def _validate_record(
    harness: str,
    home: Path,
    prefix: Path,
    record: dict[str, Any],
    *,
    added_only: bool,
) -> None:
    if not isinstance(record, dict):
        raise SetupError(f"invalid {harness} harness ownership record")
    if harness == "claude":
        _validate_claude(home, prefix, record, added_only=added_only)
    elif harness == "hermes":
        _validate_hermes(home, prefix, record, added_only=added_only)
    elif harness == "codex":
        _validate_codex(home, prefix, record, added_only=added_only)
    else:
        raise SetupError(f"unsupported harness ownership record: {harness}")


def _apply_link(home: Path, record: dict[str, Any]) -> None:
    if not record["added"]:
        return
    path = Path(record["path"])
    target = Path(record["target"])
    _ensure_user_dir(home, path.parent)
    _create_symlink(path, target)


def _apply_config(
    home: Path,
    prefix: Path,
    record: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    if not operation["changed"]:
        return
    path: Path = operation["path"]
    before: bytes | None = operation["before"]
    _ensure_user_dir(home, path.parent)
    if before is not None:
        backup = _backup(prefix, operation["backup_label"], before)
        record["config"]["backup"] = str(backup)
    _atomic_rewrite_owned(path, operation["after"], operation["mode"])


def _apply_prepared(
    harness: str,
    home: Path,
    prefix: Path,
    record: dict[str, Any],
    operation: dict[str, Any],
) -> None:
    if harness in ("claude", "hermes"):
        _apply_config(home, prefix, record, operation)
    elif harness == "codex":
        _apply_config(home, prefix, record, operation["config"])
    if harness == "hermes":
        _apply_link(home, record["plugin"])
    if harness == "codex":
        marker = operation["reload_marker"]
        if marker["changed"]:
            _ensure_private_dir(Path(marker["path"]).parent)
            _atomic_rewrite_owned(Path(marker["path"]), marker["payload"], 0o600)
        changed_plists = [item for item in operation["plists"] if item["changed"]]
        for item in changed_plists:
            path: Path = item["path"]
            _ensure_user_dir(home, path.parent)
            _atomic_rewrite_owned(path, item["payload"], 0o600)
        _apply_codex_daemon_join(operation)
    if not operation.get("existing"):
        _apply_link(home, record["skill"])


def _codex_daemon_join_env(arguments: list[str]) -> tuple[bool, bool, str]:
    """Return (launchctl_invoked, succeeded, detail)."""

    try:
        launchctl = _launchctl_path()
    except SetupError as error:
        return False, False, str(error)
    try:
        result = subprocess.run(
            [launchctl, *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        return True, False, str(error)
    if result.returncode != 0:
        return True, False, result.stderr.strip() or f"exit {result.returncode}"
    return True, True, ""


def _apply_codex_daemon_join(operation: dict[str, Any]) -> None:
    """Set the Desktop join switch now; the login agent re-applies it later.

    Desktop is a GUI app, so its environment comes from the launchd user
    domain and nothing else.  A failure here is not fatal: the owned login
    agent applies the same value at the next login and doctor reports the
    drift meanwhile.  Failing the whole setup on a semi-documented upstream
    switch would be the worse trade.
    """

    join = operation.get("daemon_join")
    if not isinstance(join, dict) or not join.get("changed"):
        # Apply exactly what the preview announced.  A switch later unset by
        # hand is drift, not an upgrade: doctor reports it and the owned login
        # agent re-applies it, so a silent re-set here would hide the fact.
        return
    _invoked, applied, detail = _codex_daemon_join_env(
        ["setenv", join["variable"], join["value"]]
    )
    join["applied"] = applied
    join["detail"] = detail


def _unset_codex_daemon_join(record: dict[str, Any]) -> tuple[bool, str]:
    """Point Desktop back at its own host before the daemon can disappear.

    Ordering is the whole point: leaving the switch set while the socket it
    names is removed would aim Desktop at a daemon that no longer exists.
    """

    join = record.get("daemon_join")
    if not isinstance(join, dict):
        return False, ""
    invoked, _ok, detail = _codex_daemon_join_env(["unsetenv", join["variable"]])
    return invoked, detail


def _apply_mutation_paths(
    prefix: Path,
    prepared: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> list[Path]:
    paths: list[Path] = []
    for harness, (record, operation) in prepared.items():
        config_operation = (
            operation.get("config") if harness == "codex" else operation
        )
        if harness in ("claude", "hermes", "codex") and config_operation["changed"]:
            paths.append(config_operation["path"])
            before = config_operation["before"]
            if before is not None:
                paths.append(
                    _backup_path(prefix, config_operation["backup_label"], before)
                )
        if harness == "hermes" and record["plugin"]["added"]:
            paths.append(Path(record["plugin"]["path"]))
        if harness == "codex":
            marker = operation["reload_marker"]
            if marker["changed"]:
                paths.append(Path(marker["path"]))
            paths.extend(
                item["path"] for item in operation["plists"] if item["changed"]
            )
        if record["skill"]["added"] and not operation.get("existing"):
            paths.append(Path(record["skill"]["path"]))
    if prepared:
        paths.append(_manifest_path(prefix))
    return paths


def _remove_mutation_paths(
    prefix: Path,
    selected_owned: list[str],
    owned: dict[str, Any],
) -> list[Path]:
    paths: list[Path] = []
    for harness in selected_owned:
        record = owned[harness]
        if harness == "claude" and (
            any(
                fragment["added"]
                for fragment in record["config"]["fragments"]
            )
            or any(
                item.get("added")
                for item in record["config"].get("permissions", {}).get(
                    "rules", []
                )
                if isinstance(item, dict)
            )
        ):
            paths.append(Path(record["config"]["path"]))
        elif harness == "hermes" and record["config"]["enabled_added"]:
            paths.append(Path(record["config"]["path"]))
        elif harness == "codex" and isinstance(record.get("config"), dict) and any(
            fragment.get("added")
            for fragment in record["config"].get("fragments", [])
            if isinstance(fragment, dict)
        ):
            paths.append(Path(record["config"]["path"]))
        if harness == "hermes" and record["plugin"]["added"]:
            paths.append(Path(record["plugin"]["path"]))
        if harness == "codex":
            paths.append(_codex_reload_marker_path(prefix))
            paths.extend(
                Path(item["path"]) for item in record["plists"] if item["added"]
            )
        if record["skill"]["added"]:
            paths.append(Path(record["skill"]["path"]))
    if selected_owned:
        paths.append(_manifest_path(prefix))
    return paths


def _preflight_mutation_paths(
    home: Path,
    prefix: Path,
    paths: list[Path],
) -> None:
    for path in paths:
        _require_mutable_parent(home, prefix, path)


def _preflight_codex_runtime(
    home: Path,
    prefix: Path,
    operation: dict[str, Any],
) -> bool:
    """Create/check Codex runtime payloads before any user config is changed."""

    changed_plists = [item for item in operation["plists"] if item["changed"]]
    if not changed_plists:
        return False
    live_payloads = _codex_payloads(home, prefix, ensure_runtime=True)
    for item in operation["plists"]:
        expected = plistlib.dumps(
            live_payloads[Path(item["path"]).stem],
            fmt=plistlib.FMT_XML,
            sort_keys=True,
        )
        if expected != item["payload"]:
            raise SetupError(
                f"Codex LaunchAgent payload changed between preflight and "
                f"apply: {item['path']}"
            )
    app_write = next(
        item
        for item in operation["plists"]
        if Path(item["path"]).stem == "com.roundtable.codex-app-server"
    )
    expected_marker = _json_bytes(
        _codex_reload_marker_value(
            prefix,
            Path(app_write["path"]),
            _digest(app_write["payload"]),
        )
    )
    marker = operation.get("reload_marker")
    if (
        not isinstance(marker, dict)
        or marker.get("path") != _codex_reload_marker_path(prefix)
        or marker.get("payload") != expected_marker
        or marker.get("changed") is not app_write["content_changed"]
    ):
        raise SetupError("Codex reload marker changed between preflight and apply")
    return any(item["content_changed"] for item in changed_plists)


def _remove_claude(home: Path, record: dict[str, Any]) -> None:
    config = record["config"]
    path = Path(config["path"])
    added_fragments = [
        fragment for fragment in config["fragments"] if fragment["added"]
    ]
    permissions_record = config.get("permissions")
    added_rules = (
        [item for item in permissions_record["rules"] if item["added"]]
        if isinstance(permissions_record, dict)
        else []
    )
    if added_fragments or added_rules:
        value, _raw, mode = _load_json_config(path)
        if added_fragments:
            hooks = value.get("hooks")
            assert isinstance(hooks, dict)
            for fragment in added_fragments:
                groups = hooks[fragment["event"]]
                groups.remove(fragment["group"])
                if (
                    config["event_containers_added"].get(fragment["event"])
                    and not groups
                ):
                    hooks.pop(fragment["event"])
            if config["hooks_container_added"] and not hooks:
                value.pop("hooks")
        if added_rules:
            permissions = value.get("permissions")
            assert isinstance(permissions, dict)
            allow = permissions.get("allow")
            assert isinstance(allow, list)
            for item in added_rules:
                allow.remove(item["rule"])
            if permissions_record["allow_container_added"] and not allow:
                permissions.pop("allow")
            if (
                permissions_record["permissions_container_added"]
                and not permissions
            ):
                value.pop("permissions")
        if config["created"] and value == {}:
            path.unlink()
        else:
            _atomic_rewrite_owned(path, _json_bytes(value), mode)
    skill = record["skill"]
    if skill["added"]:
        Path(skill["path"]).unlink()


def _remove_hermes(home: Path, record: dict[str, Any]) -> None:
    config = record["config"]
    path = Path(config["path"])
    _value, raw, mode = _load_yaml_config(path)
    if config["enabled_added"]:
        assert raw is not None
        fragment = config["managed_fragment"].encode("utf-8")
        updated = raw.replace(fragment, b"", 1)
        if config["created"] and updated == b"":
            path.unlink()
        else:
            _atomic_rewrite_owned(path, updated, mode)
    for key in ("plugin", "skill"):
        link = record[key]
        if link["added"]:
            Path(link["path"]).unlink()


def _remove_codex(record: dict[str, Any]) -> None:
    config = record.get("config")
    if isinstance(config, dict):
        path = Path(config["path"])
        added_fragments = [
            fragment
            for fragment in config["fragments"]
            if fragment["added"]
        ]
        if added_fragments:
            value, _raw, mode = _load_json_config(path)
            hooks = value.get("hooks")
            assert isinstance(hooks, dict)
            for fragment in added_fragments:
                groups = hooks[fragment["event"]]
                groups.remove(fragment["group"])
                if (
                    config["event_containers_added"].get(fragment["event"])
                    and not groups
                ):
                    hooks.pop(fragment["event"])
            if config["hooks_container_added"] and not hooks:
                value.pop("hooks")
            if config["created"] and value == {}:
                path.unlink()
            else:
                _atomic_rewrite_owned(path, _json_bytes(value), mode)
    for item in record["plists"]:
        if item["added"]:
            Path(item["path"]).unlink()
    skill = record["skill"]
    if skill["added"]:
        Path(skill["path"]).unlink()


def _codex_unload_required(
    harnesses: list[str],
    manifest: dict[str, Any] | None,
) -> bool:
    if manifest is None or "codex" not in harnesses:
        return False
    record = manifest["harnesses"].get("codex")
    if not isinstance(record, dict):
        return False
    return any(
        isinstance(item, dict) and item.get("added") is True
        for item in record.get("plists", [])
    )


def _launchctl_path() -> str:
    configured = os.environ.get("RT_LAUNCHCTL", "/bin/launchctl")
    if os.path.isabs(configured):
        path = Path(configured)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    else:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    raise SetupError(f"launchctl is not executable: {configured}")


def _require_outside_codex() -> None:
    """Refuse a live Codex teardown from inside the session being torn down."""

    if os.environ.get("CODEX_THREAD_ID"):
        raise SetupError(
            "refusing to unload Codex LaunchAgents from inside a Codex "
            "session; run this command from Terminal.app, iTerm, Ghostty, "
            "or another shell outside Codex"
        )


def _unload_codex_jobs(record: dict[str, Any]) -> tuple[bool, bool]:
    """Return (launchctl_invoked, live_state_may_have_changed)."""

    _require_outside_codex()
    launchctl = _launchctl_path()
    domain = f"gui/{os.getuid()}"
    invoked = False
    external_changed = False
    for item in record["plists"]:
        if not item["added"]:
            continue
        label = item["label"]
        target = f"{domain}/{label}"
        invoked = True
        try:
            inspected = subprocess.run(
                [launchctl, "print", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as error:
            raise _LaunchctlOperationError(
                f"cannot inspect LaunchAgent {label}: {error}",
                invoked=invoked,
                external_changed=external_changed,
            ) from error
        if inspected.returncode == 113:
            continue
        if inspected.returncode != 0:
            detail = inspected.stderr.strip() or f"exit {inspected.returncode}"
            raise _LaunchctlOperationError(
                f"cannot inspect LaunchAgent {label}: {detail}",
                invoked=invoked,
                external_changed=external_changed,
            )
        # A bootout attempt can change live state even if launchctl later
        # reports failure, so filesystem rollback must not claim a full undo.
        external_changed = True
        try:
            removed = subprocess.run(
                [launchctl, "bootout", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as error:
            raise _LaunchctlOperationError(
                f"cannot unload LaunchAgent {label}: {error}",
                invoked=invoked,
                external_changed=external_changed,
            ) from error
        if removed.returncode != 0:
            detail = removed.stderr.strip() or f"exit {removed.returncode}"
            raise _LaunchctlOperationError(
                f"cannot unload LaunchAgent {label}: {detail}",
                invoked=invoked,
                external_changed=external_changed,
            )
    return invoked, external_changed


def _remove_record(harness: str, home: Path, record: dict[str, Any]) -> None:
    if harness == "claude":
        _remove_claude(home, record)
    elif harness == "hermes":
        _remove_hermes(home, record)
    elif harness == "codex":
        _remove_codex(record)


def _detect_harnesses(home: Path) -> list[str]:
    paths = {
        "claude": (home / ".claude", home / ".local" / "bin" / "claude"),
        "hermes": (home / ".hermes", home / ".local" / "bin" / "hermes"),
        "codex": (
            _selected_codex_home(home),
            home / ".npm-global" / "bin" / "codex",
        ),
    }
    selected: list[str] = []
    for harness in HARNESSES:
        if any(_lexists(path) for path in paths[harness]) or shutil.which(harness):
            selected.append(harness)
    return selected


def _selected(
    requested: list[str] | None,
    home: Path,
    manifest: dict[str, Any] | None,
    *,
    command: str,
) -> list[str]:
    if requested:
        return list(dict.fromkeys(requested))
    detected = _detect_harnesses(home)
    if command in ("status", "remove") and manifest is not None:
        detected.extend(manifest["harnesses"])
    return [name for name in HARNESSES if name in set(detected)]


def _source_preflight(prefix: Path, harnesses: list[str]) -> None:
    if harnesses:
        _validate_source(_skill_target(prefix), "installed pneu skill")
    if "claude" in harnesses:
        for command, description in (
            ("rt-wait-inbox", "Claude inbox wake hook"),
            ("rt-inbox", "Claude inbox reader"),
            ("rt-ack", "Claude acknowledgement command"),
            ("rt-say", "Claude mail sender"),
        ):
            _validate_executable(
                prefix / "bin" / command,
                f"installed pneu {description}",
            )
    if "hermes" in harnesses:
        _validate_source(
            _hermes_plugin_target(prefix),
            "installed Hermes pneu integration",
        )
    if "codex" in harnesses:
        _validate_executable(
            prefix / "bin" / "rt-codex-session-start",
            "installed pneu Codex SessionStart hook",
        )


def _actions(
    record: dict[str, Any],
    operation: dict[str, Any] | None = None,
) -> list[str]:
    actions: list[str] = []
    existing = bool((operation or {}).get("existing"))
    config_operation = (operation or {}).get("config", operation or {})
    config = record.get("config")
    if isinstance(config, dict) and (
        not existing or bool(config_operation.get("changed"))
    ):
        if any(item.get("added") for item in config.get("fragments", [])):
            actions.append(f"merge {config['path']}")
        elif config.get("enabled_added"):
            actions.append(f"merge {config['path']}")
        permissions_record = config.get("permissions")
        if isinstance(permissions_record, dict):
            actions.extend(
                f"allow {rule} in {config['path']}"
                for rule in config_operation.get("permission_rules_added", [])
            )
    for key in ("plugin", "skill"):
        item = record.get(key)
        if (
            isinstance(item, dict)
            and item.get("added")
            and not (key == "skill" and (operation or {}).get("existing"))
        ):
            actions.append(f"link {item['path']}")
    plist_operations = {
        str(item["path"]): item
        for item in (operation or {}).get("plists", [])
    }
    for item in record.get("plists", []):
        planned = plist_operations.get(item["path"], {})
        if planned.get("mode_changed") and not planned.get("content_changed"):
            actions.append(f"secure {item['path']} permissions to 0600")
        elif planned.get("changed") and planned.get("existing"):
            actions.append(f"update {item['path']} (reload deferred)")
        elif planned.get("changed") and item.get("added"):
            actions.append(f"write {item['path']} (not loaded)")
        elif planned.get("changed"):
            actions.append(f"update {item['path']} (reload deferred)")
    join = (operation or {}).get("daemon_join")
    if isinstance(join, dict) and join.get("changed"):
        actions.append(
            f"set {join['variable']}={join['value']} in the launchd user "
            "domain so Codex Desktop joins the pneu daemon "
            "(restart Desktop to take effect)"
        )
    return actions or ["no changes"]


def _codex_needs_upgrade(
    record: dict[str, Any],
    previous: dict[str, Any],
    operation: dict[str, Any],
) -> bool:
    """Include metadata-only repairs that do not change manifest content."""

    return bool(
        record != previous
        or operation["config"]["changed"]
        or operation["reload_marker"]["changed"]
        or any(item["changed"] for item in operation["plists"])
    )


def _claude_needs_upgrade(
    record: dict[str, Any],
    previous: dict[str, Any],
    operation: dict[str, Any],
) -> bool:
    return bool(record != previous or operation["changed"])


def _plan(
    home: Path,
    prefix: Path,
    harnesses: list[str],
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    _source_preflight(prefix, harnesses)
    results: dict[str, Any] = {}
    prepared: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    owned = manifest["harnesses"] if manifest else {}
    if "codex" in harnesses and "codex" in owned:
        _require_validated_codex_release(home, prefix)
    for harness in harnesses:
        if harness in owned:
            _validate_record(
                harness,
                home,
                prefix,
                owned[harness],
                added_only=False,
            )
            if harness == "claude":
                record, operation = _prepare_claude(
                    home,
                    prefix,
                    existing_record=owned[harness],
                )
                if _claude_needs_upgrade(record, owned[harness], operation):
                    prepared[harness] = (record, operation)
                    actions = _actions(record, operation)
                    if actions == ["no changes"]:
                        actions = ["record existing Claude integration state"]
                    results[harness] = {
                        "state": "upgrade_planned",
                        "actions": actions,
                    }
                    continue
            elif harness == "codex":
                record, operation = _prepare_codex(
                    home,
                    prefix,
                    ensure_runtime=False,
                    existing_record=owned[harness],
                )
                if _codex_needs_upgrade(record, owned[harness], operation):
                    prepared[harness] = (record, operation)
                    actions = _actions(record, operation)
                    if actions == ["no changes"]:
                        actions = ["record existing Codex integration state"]
                    results[harness] = {
                        "state": "upgrade_planned",
                        "actions": actions,
                    }
                    continue
            results[harness] = {"state": "configured", "actions": ["no changes"]}
            continue
        record, operation = _prepare(
            harness,
            home,
            prefix,
            ensure_runtime=False,
        )
        prepared[harness] = (record, operation)
        results[harness] = {
            "state": "planned",
            "actions": _actions(record, operation),
        }
    _preflight_mutation_paths(
        home,
        prefix,
        _apply_mutation_paths(prefix, prepared),
    )
    return {
        "ok": True,
        "command": "plan",
        "home": str(home),
        "prefix": str(prefix),
        "harnesses": results,
        "writes": False,
        "rolled_back": False,
        "launchctl_invoked": False,
    }


def _status(
    home: Path,
    prefix: Path,
    harnesses: list[str],
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    _source_preflight(prefix, harnesses)
    results: dict[str, Any] = {}
    owned = manifest["harnesses"] if manifest else {}
    for harness in harnesses:
        if harness not in owned:
            results[harness] = {"state": "not_configured"}
            continue
        _validate_record(
            harness,
            home,
            prefix,
            owned[harness],
            added_only=False,
        )
        if harness == "claude":
            record, operation = _prepare_claude(
                home,
                prefix,
                existing_record=owned[harness],
            )
            if _claude_needs_upgrade(record, owned[harness], operation):
                actions = _actions(record, operation)
                if actions == ["no changes"]:
                    actions = ["record existing Claude integration state"]
                results[harness] = {
                    "state": "upgrade_required",
                    "actions": actions,
                }
                continue
        elif harness == "codex":
            record, operation = _prepare_codex(
                home,
                prefix,
                ensure_runtime=False,
                existing_record=owned[harness],
            )
            if _codex_needs_upgrade(record, owned[harness], operation):
                actions = _actions(record, operation)
                if actions == ["no changes"]:
                    actions = ["record existing Codex integration state"]
                results[harness] = {
                    "state": "upgrade_required",
                    "actions": actions,
                }
                continue
        results[harness] = {"state": "configured"}
    return {
        "ok": True,
        "command": "status",
        "home": str(home),
        "prefix": str(prefix),
        "harnesses": results,
        "writes": False,
        "rolled_back": False,
        "launchctl_invoked": False,
    }


def _apply(
    home: Path,
    prefix: Path,
    harnesses: list[str],
    manifest: dict[str, Any] | None,
) -> dict[str, Any]:
    _source_preflight(prefix, harnesses)
    value = copy.deepcopy(manifest) if manifest else _new_manifest(prefix, home)
    owned = value["harnesses"]
    prepared: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    results: dict[str, Any] = {}

    # Complete every ownership and collision check before the first mutation.
    if "codex" in harnesses and "codex" in owned:
        _require_validated_codex_release(home, prefix)
    for harness in harnesses:
        if harness in owned:
            _validate_record(
                harness,
                home,
                prefix,
                owned[harness],
                added_only=False,
            )
            if harness == "claude":
                record, operation = _prepare_claude(
                    home,
                    prefix,
                    existing_record=owned[harness],
                )
                if _claude_needs_upgrade(record, owned[harness], operation):
                    prepared[harness] = (record, operation)
                    continue
            elif harness == "codex":
                record, operation = _prepare_codex(
                    home,
                    prefix,
                    ensure_runtime=False,
                    existing_record=owned[harness],
                )
                if _codex_needs_upgrade(record, owned[harness], operation):
                    prepared[harness] = (record, operation)
                    continue
            results[harness] = {"state": "configured", "actions": ["no changes"]}
            continue
        prepared[harness] = _prepare(
            harness,
            home,
            prefix,
            ensure_runtime=False,
        )

    mutation_paths = _apply_mutation_paths(prefix, prepared)
    _preflight_mutation_paths(home, prefix, mutation_paths)
    snapshots = _snapshot_paths(mutation_paths)
    codex_restart_required = False
    try:
        if "codex" in prepared:
            _record, codex_operation = prepared["codex"]
            codex_restart_required = _preflight_codex_runtime(
                home,
                prefix,
                codex_operation,
            )

        for harness in harnesses:
            if harness not in prepared:
                continue
            record, operation = prepared[harness]
            actions = _actions(record, operation)
            if actions == ["no changes"] and harness in owned:
                actions = [f"record existing {harness.title()} integration state"]
            _apply_prepared(harness, home, prefix, record, operation)
            owned[harness] = record
            results[harness] = {"state": "configured", "actions": actions}

        if prepared:
            _write_manifest(prefix, value)
    except Exception as error:
        changed, restored, rollback_errors = _rollback_snapshots(snapshots)
        raise SetupMutationError(
            f"apply failed: {error}",
            writes=changed,
            rolled_back=bool(changed and restored),
            rollback_errors=rollback_errors,
        ) from error
    return {
        "ok": True,
        "command": "apply",
        "home": str(home),
        "prefix": str(prefix),
        "harnesses": results,
        "writes": bool(prepared),
        "rolled_back": False,
        "launchctl_invoked": False,
        "restart_required": codex_restart_required,
    }


def _remove(
    home: Path,
    prefix: Path,
    harnesses: list[str],
    manifest: dict[str, Any] | None,
    *,
    unload_codex: bool = False,
) -> dict[str, Any]:
    if manifest is None:
        return {
            "ok": True,
            "command": "remove",
            "home": str(home),
            "prefix": str(prefix),
            "harnesses": {
                harness: {"state": "not_configured"} for harness in harnesses
            },
            "writes": False,
            "rolled_back": False,
            "launchctl_invoked": False,
        }
    value = copy.deepcopy(manifest)
    owned = value["harnesses"]
    selected_owned = [harness for harness in harnesses if harness in owned]
    if "claude" in selected_owned:
        _require_rc_hosts_disabled(prefix)

    # Drift is checked for all owned fragments before removing any of them.
    for harness in selected_owned:
        _validate_record(
            harness,
            home,
            prefix,
            owned[harness],
            added_only=True,
        )
    mutation_paths = _remove_mutation_paths(prefix, selected_owned, owned)
    _preflight_mutation_paths(home, prefix, mutation_paths)
    snapshots = _snapshot_paths(mutation_paths)
    launchctl_invoked = False
    external_changed = False
    try:
        unload_required = _codex_unload_required(harnesses, manifest)
        if unload_required and not unload_codex:
            raise SetupError(
                "Codex LaunchAgents may still be loaded; from a normal "
                "terminal outside Codex, rerun with `roundtable-setup remove "
                "--unload-codex`"
            )
        if unload_required:
            _require_outside_codex()
        if "codex" in selected_owned:
            # Unset before anything else in the codex teardown: the daemon,
            # its socket, its plists, and the login agent all disappear below,
            # and a switch left pointing at a removed daemon is worse than no
            # switch at all.  A user-domain variable cannot be restored by a
            # filesystem rollback, so a real invocation is reported as an
            # external change.
            unset_invoked, _unset_detail = _unset_codex_daemon_join(
                owned["codex"]
            )
            if unset_invoked:
                launchctl_invoked = True
                external_changed = True
        if unload_required:
            unload_invoked, unload_changed = _unload_codex_jobs(owned["codex"])
            launchctl_invoked = launchctl_invoked or unload_invoked
            external_changed = external_changed or unload_changed
        for harness in selected_owned:
            _remove_record(harness, home, owned[harness])
            if harness == "codex":
                marker_path = _codex_reload_marker_path(prefix)
                if _lexists(marker_path):
                    _inspect_owned(marker_path, kind="file")
                    marker_path.unlink()
            del owned[harness]

        manifest_path = _manifest_path(prefix)
        if selected_owned:
            if owned:
                _write_manifest(prefix, value)
            else:
                _inspect_owned(manifest_path, kind="file")
                manifest_path.unlink()
    except Exception as error:
        if isinstance(error, _LaunchctlOperationError):
            launchctl_invoked = launchctl_invoked or error.invoked
            external_changed = external_changed or error.external_changed
        changed, restored, rollback_errors = _rollback_snapshots(snapshots)
        writes = bool(changed or external_changed)
        raise SetupMutationError(
            f"remove failed: {error}",
            writes=writes,
            rolled_back=bool(writes and restored and not external_changed),
            launchctl_invoked=launchctl_invoked,
            rollback_errors=rollback_errors,
        ) from error
    return {
        "ok": True,
        "command": "remove",
        "home": str(home),
        "prefix": str(prefix),
        "harnesses": {
            harness: {
                "state": "removed" if harness in selected_owned else "not_configured"
            }
            for harness in harnesses
        },
        "writes": bool(selected_owned),
        "rolled_back": False,
        "launchctl_invoked": launchctl_invoked,
    }


def _preflight_remove(
    home: Path,
    prefix: Path,
    harnesses: list[str],
    manifest: dict[str, Any] | None,
    *,
    unload_codex: bool,
) -> None:
    if manifest is None:
        return
    owned = manifest["harnesses"]
    selected_owned = [harness for harness in harnesses if harness in owned]
    if "claude" in selected_owned:
        _require_rc_hosts_disabled(prefix)
    for harness in harnesses:
        if harness in owned:
            _validate_record(
                harness,
                home,
                prefix,
                owned[harness],
                added_only=True,
            )
    _preflight_mutation_paths(
        home,
        prefix,
        _remove_mutation_paths(prefix, selected_owned, owned),
    )
    if _codex_unload_required(harnesses, manifest) and not unload_codex:
        raise SetupError(
            "Codex LaunchAgents may still be loaded; from a normal terminal "
            "outside Codex, rerun with `roundtable-setup remove "
            "--unload-codex`"
        )


def _render(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"roundtable-setup {result['command']}:")
    harnesses = result.get("harnesses", {})
    if not harnesses:
        print("  no installed harnesses detected; use --harness to select one")
    for harness, detail in harnesses.items():
        print(f"  {harness}: {detail['state']}")
        for action in detail.get("actions", []):
            print(f"    - {action}")
    unknown_harnesses = result.get("unknown_harnesses", [])
    if unknown_harnesses:
        print(
            "  preserving opaque manifest records for unknown harnesses: "
            + ", ".join(unknown_harnesses)
        )
    if result["command"] == "remove":
        print(
            "  preserved project registry, UUID layout locks, "
            "registry-selected local/central mailboxes, bookmarks, "
            "migration recovery records, and archival backups"
        )
    if result["command"] == "plan":
        print("  dry run only; run `roundtable-setup apply` to make these changes")
    if result.get("restart_required"):
        print(
            "  Codex service activation/reload is deferred to the next "
            "pneu Codex launch; pneu may ask before a shared reload"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roundtable-setup",
        description="Plan, apply, inspect, or remove harness onboarding.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("plan", "apply", "status", "remove"),
        default="plan",
    )
    parser.add_argument(
        "--harness",
        choices=HARNESSES,
        action="append",
        help="harness to configure; repeat for more than one",
    )
    parser.add_argument("--prefix", help="pneu installation prefix")
    parser.add_argument("--home", help="home directory to configure")
    parser.add_argument(
        "--unload-codex",
        action="store_true",
        help=(
            "on remove, unload owned Codex LaunchAgents before deleting their "
            "plists; must be run outside Codex"
        ),
    )
    parser.add_argument("--json", action="store_true", help="emit structured JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    home = _absolute(args.home) if args.home else _absolute(Path.home())
    prefix = (
        _absolute(args.prefix)
        if args.prefix
        else _absolute(os.environ.get("ROUNDTABLE_INSTALL_PREFIX", home / ".pneu"))
    )
    try:
        _validate_user_chain(home, home)
        _validate_prefix(prefix)
        if args.unload_codex and args.command != "remove":
            raise SetupError("--unload-codex is valid only with remove")
        if args.command in ("apply", "remove"):
            # Keep rejected setup attempts genuinely read-only. The same
            # preflight is repeated under the lock before any mutation so a
            # concurrent process cannot invalidate these observations.
            manifest = _load_manifest(prefix, home)
            harnesses = _selected(
                args.harness,
                home,
                manifest,
                command=args.command,
            )
            if args.command == "apply":
                _plan(home, prefix, harnesses, manifest)
            else:
                _preflight_remove(
                    home,
                    prefix,
                    harnesses,
                    manifest,
                    unload_codex=args.unload_codex,
                )
            if not harnesses:
                result = (
                    _apply(home, prefix, harnesses, manifest)
                    if args.command == "apply"
                    else _remove(
                        home,
                        prefix,
                        harnesses,
                        manifest,
                        unload_codex=args.unload_codex,
                    )
                )
                _report_unknown_harnesses(result, manifest)
                _render(result, as_json=args.json)
                return 0
            with _mutation_lock(prefix):
                manifest = _load_manifest(prefix, home)
                harnesses = _selected(
                    args.harness,
                    home,
                    manifest,
                    command=args.command,
                )
                if args.command == "apply":
                    result = _apply(home, prefix, harnesses, manifest)
                else:
                    result = _remove(
                        home,
                        prefix,
                        harnesses,
                        manifest,
                        unload_codex=args.unload_codex,
                    )
        else:
            manifest = _load_manifest(prefix, home)
            harnesses = _selected(
                args.harness,
                home,
                manifest,
                command=args.command,
            )
            if args.command == "plan":
                result = _plan(home, prefix, harnesses, manifest)
            else:
                result = _status(home, prefix, harnesses, manifest)
    except (SetupError, OSError) as error:
        writes = bool(getattr(error, "writes", False))
        rolled_back = bool(getattr(error, "rolled_back", False))
        launchctl_invoked = bool(getattr(error, "launchctl_invoked", False))
        result = {
            "ok": False,
            "command": args.command,
            "home": str(home),
            "prefix": str(prefix),
            "error": str(error),
            "writes": writes,
            "rolled_back": rolled_back,
            "launchctl_invoked": launchctl_invoked,
        }
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print(f"roundtable-setup: {error}", file=sys.stderr)
        return 2
    _report_unknown_harnesses(result, manifest)
    _render(result, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
