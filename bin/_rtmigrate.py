"""Crash-safe local/central Roundtable mailbox migration primitives."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from _rtlib import (
    CENTRAL_MAIL_MARKER_NAME,
    CENTRAL_MAIL_MARKER_SCHEMA,
    PROJECTS_SCHEMA,
    ProjectRegistryError,
    _clean_git_environment,
    _close_project_guard,
    _open_project_guard,
    _read_owned_regular_bytes,
    _read_project_identity_at,
    _read_projects_snapshot,
    _registry_path,
    _strict_entries_from_document,
    _update_project_registry,
    _validate_owned_directory_fd,
    _verify_project_guard,
    central_mail_root,
    locked_project_mailbox_checked,
    project_identity_path,
    resolve_project_mailbox_checked,
    validate_central_mail_marker,
)


MIGRATION_MANIFEST_SCHEMA = "roundtable.mail-migration.v1"
ROLLBACK_MARKER_SCHEMA = "roundtable.mail-rollback.v1"
ROLLBACK_MARKER_NAME = ".mail-rollback.json"
DEFAULT_BACKUP_ROOT = (
    Path.home()
    / "Documents"
    / "Workspace"
    / "backups"
    / "roundtable-central-mail"
)
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
COPY_CHUNK_SIZE = 1024 * 1024
MAIL_TOP_LEVEL = ("inbox", "messages")
GITIGNORE_BEGIN = "# BEGIN Roundtable central mail"
GITIGNORE_END = "# END Roundtable central mail"
GITIGNORE_BLOCK = (
    GITIGNORE_BEGIN,
    "mail",
    ".central-mail-*",
    ".mail-rollback.*",
    GITIGNORE_END,
)


class MailMigrationError(ProjectRegistryError):
    """A mailbox migration cannot safely proceed."""


class MailMigrationCommittedError(MailMigrationError):
    """The registry cutover committed but post-commit repair failed."""


class MailMigrationCommitUnknownError(MailMigrationError):
    """The registry cutover may have committed and cannot be re-read."""


class InjectedMigrationFailure(MailMigrationError):
    """A test-only phase barrier interrupted a migration."""


@dataclass(frozen=True)
class MailSnapshot:
    entries: tuple[dict, ...]
    file_ids: dict[str, tuple[int, int]]
    digest: str
    files: int
    bytes: int


@dataclass
class MigrationMetrics:
    copy_seconds: float = 0.0
    fsync_seconds: float = 0.0
    registry_flip_seconds: float = 0.0

    def fsync(self, descriptor: int) -> None:
        started = time.monotonic()
        os.fsync(descriptor)
        self.fsync_seconds += time.monotonic() - started


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _operation_name(operation_id: str | None = None) -> tuple[str, str]:
    selected = str(uuid.UUID(operation_id)) if operation_id else str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return selected, f"{timestamp}-{selected}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _json_bytes(document: dict) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _maybe_fail(phase: str) -> None:
    if os.environ.get("RT_MIGRATION_FAILPOINT", "") == phase:
        raise InjectedMigrationFailure(
            f"injected central-mail migration failure at {phase}"
        )


def _path_info(path: Path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _validate_owned_node(info, path: Path, label: str, *, directory: bool) -> None:
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != os.getuid():
        raise MailMigrationError(
            f"{label} has invalid type or ownership: {path}"
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise MailMigrationError(f"{label} is group/other writable: {path}")


def _open_owned_directory(
    path: Path,
    label: str,
    *,
    exact_private: bool = False,
) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise MailMigrationError(f"cannot open {label} {path}: {error}") from error
    try:
        _validate_owned_directory_fd(descriptor, path, label)
        if exact_private and stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            raise MailMigrationError(
                f"{label} mode must be 0700: {path}"
            )
        visible = path.lstat()
        held = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(visible.st_mode)
            or visible.st_dev != held.st_dev
            or visible.st_ino != held.st_ino
        ):
            raise MailMigrationError(f"{label} changed while opening: {path}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _sync_new_private_directory(
    parent_fd: int,
    child_fd: int,
    path: Path,
    metrics: MigrationMetrics,
) -> None:
    """Persist a newly-created private directory and its parent entry."""

    del path
    metrics.fsync(child_fd)
    metrics.fsync(parent_fd)


def _sync_private_directory_entry(
    path: Path,
    child_fd: int,
    label: str,
    metrics: MigrationMetrics,
) -> None:
    if path.parent == path:
        metrics.fsync(child_fd)
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        parent_fd = os.open(path.parent, flags)
    except OSError as error:
        raise MailMigrationError(
            f"cannot open {label} parent {path.parent}: {error}"
        ) from error
    try:
        visible = path.parent.lstat()
        held = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(visible.st_mode)
            or visible.st_dev != held.st_dev
            or visible.st_ino != held.st_ino
        ):
            raise MailMigrationError(
                f"{label} parent changed while opening: {path.parent}"
            )
        try:
            _sync_new_private_directory(
                parent_fd,
                child_fd,
                path,
                metrics,
            )
        except OSError as error:
            raise MailMigrationError(
                f"cannot persist {label} {path}: {error}"
            ) from error
    finally:
        os.close(parent_fd)


def _ensure_private_directory(
    path: Path,
    label: str,
    metrics: MigrationMetrics,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    missing: list[Path] = []
    ancestor = absolute
    while _path_info(ancestor) is None:
        if ancestor.parent == ancestor:
            raise MailMigrationError(
                f"cannot find an existing parent for {label} {absolute}"
            )
        missing.append(ancestor)
        ancestor = ancestor.parent
    current_fd = _open_owned_directory(
        ancestor,
        f"{label} existing ancestor",
    )
    try:
        _sync_private_directory_entry(
            ancestor,
            current_fd,
            f"{label} existing ancestor",
            metrics,
        )
        for directory in reversed(missing):
            try:
                os.mkdir(directory.name, 0o700, dir_fd=current_fd)
            except FileExistsError:
                pass
            except OSError as error:
                raise MailMigrationError(
                    f"cannot create {label} {directory}: {error}"
                ) from error
            child_fd = None
            try:
                child_fd = _open_child_directory(
                    current_fd,
                    directory.name,
                    directory,
                )
                held = os.fstat(child_fd)
                visible = os.stat(
                    directory.name,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
                if (
                    stat.S_IMODE(held.st_mode) != 0o700
                    or held.st_dev != visible.st_dev
                    or held.st_ino != visible.st_ino
                ):
                    raise MailMigrationError(
                        f"{label} mode or identity is invalid: {directory}"
                    )
                try:
                    _sync_new_private_directory(
                        current_fd,
                        child_fd,
                        directory,
                        metrics,
                    )
                except OSError as error:
                    raise MailMigrationError(
                        f"cannot persist {label} {directory}: {error}"
                    ) from error
            except Exception:
                if child_fd is not None:
                    os.close(child_fd)
                raise
            os.close(current_fd)
            current_fd = child_fd
        info = os.fstat(current_fd)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise MailMigrationError(
                f"{label} mode must be 0700: {absolute}"
            )
    finally:
        os.close(current_fd)
    descriptor = _open_owned_directory(absolute, label)
    try:
        info = os.fstat(descriptor)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise MailMigrationError(
                f"{label} mode must be 0700: {absolute}"
            )
    finally:
        os.close(descriptor)
    return absolute


def _timed_fsync_directory(path: Path, metrics: MigrationMetrics) -> None:
    descriptor = _open_owned_directory(path, "directory to sync")
    try:
        metrics.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_file(
    path: Path,
    payload: bytes,
    metrics: MigrationMetrics,
) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = None
    try:
        descriptor = os.open(path, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            metrics.fsync(handle.fileno())
    except OSError as error:
        raise MailMigrationError(
            f"cannot create private migration file {path}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_private_file(path: Path, label: str) -> bytes:
    payload = _read_owned_regular_bytes(path, label)
    info = path.lstat()
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise MailMigrationError(f"{label} mode must be 0600: {path}")
    return payload


def _manifest_path(value: Path | str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise MailMigrationError(f"manifest path is not absolute: {raw}")
    absolute = Path(os.path.abspath(os.fspath(raw)))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise MailMigrationError(
            f"cannot resolve migration manifest {absolute}: {error}"
        ) from error
    if resolved != absolute:
        raise MailMigrationError(
            f"migration manifest path traverses a symlink: {absolute}"
        )
    parent = _open_owned_directory(
        absolute.parent,
        "migration manifest directory",
        exact_private=True,
    )
    os.close(parent)
    return absolute


def _path_spellings(path: Path | str) -> tuple[Path, Path]:
    absolute = Path(
        os.path.abspath(os.fspath(Path(path).expanduser()))
    )
    try:
        resolved = absolute.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise MailMigrationError(
            f"cannot resolve migration path {absolute}: {error}"
        ) from error
    return absolute, resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    if (
        left == right
        or left in right.parents
        or right in left.parents
    ):
        return True
    if sys.platform != "darwin":
        return False
    normalized_left = Path(
        unicodedata.normalize("NFC", os.fspath(left)).casefold()
    )
    normalized_right = Path(
        unicodedata.normalize("NFC", os.fspath(right)).casefold()
    )
    return (
        normalized_left == normalized_right
        or normalized_left in normalized_right.parents
        or normalized_right in normalized_left.parents
    )


def _validate_backup_location(
    backup_root: Path,
    project_uuid: str,
    forbidden_roots: tuple[Path, ...],
    *,
    operation_name: str | None = None,
    forbidden_bundle_roots: tuple[Path, ...] = (),
) -> None:
    candidate_spellings = _path_spellings(
        Path(backup_root).expanduser() / project_uuid
    )
    for forbidden in forbidden_roots:
        forbidden_spellings = _path_spellings(forbidden)
        if any(
            _paths_overlap(candidate, protected)
            for candidate in candidate_spellings
            for protected in forbidden_spellings
        ):
            raise MailMigrationError(
                "migration backup UUID root overlaps protected state: "
                f"{candidate_spellings[0]} and {forbidden_spellings[0]}"
            )
    if operation_name is None:
        if forbidden_bundle_roots:
            raise MailMigrationError(
                "backup bundle overlap validation requires an operation name"
            )
        return
    bundle_spellings = _path_spellings(
        candidate_spellings[0] / operation_name
    )
    for forbidden in forbidden_bundle_roots:
        forbidden_spellings = _path_spellings(forbidden)
        if any(
            _paths_overlap(bundle, protected)
            for bundle in bundle_spellings
            for protected in forbidden_spellings
        ):
            raise MailMigrationError(
                "migration backup bundle overlaps protected recovery state: "
                f"{bundle_spellings[0]} and {forbidden_spellings[0]}"
            )


def _safe_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise MailMigrationError(f"invalid manifest relative path: {value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise MailMigrationError(f"unsafe manifest relative path: {value!r}")
    if parsed.parts[0] not in MAIL_TOP_LEVEL:
        raise MailMigrationError(
            f"manifest path is outside inbox/messages: {value!r}"
        )
    return parsed.as_posix()


def _snapshot_digest(entries: list[dict] | tuple[dict, ...]) -> str:
    payload = json.dumps(
        list(entries),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(payload)


def _semantic_entry(entry: dict) -> dict:
    return {
        key: entry.get(key)
        for key in ("path", "kind", "size", "sha256", "hardlink")
        if key in entry
    }


def _open_child_directory(parent_fd: int, name: str, path: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    _validate_owned_directory_fd(descriptor, path, "mail directory")
    return descriptor


def _scan_directory(
    descriptor: int,
    prefix: PurePosixPath,
    absolute: Path,
    entries: list[dict],
    file_ids: dict[str, tuple[int, int]],
) -> None:
    info = os.fstat(descriptor)
    _validate_owned_node(info, absolute, "mail directory", directory=True)
    entries.append(
        {
            "path": prefix.as_posix(),
            "kind": "directory",
            "source_mode": stat.S_IMODE(info.st_mode),
        }
    )
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as error:
        raise MailMigrationError(
            f"cannot list mail directory {absolute}: {error}"
        ) from error
    for name in names:
        if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
            raise MailMigrationError(
                f"invalid mail entry name in {absolute}: {name!r}"
            )
        relative = prefix / name
        path = absolute / name
        try:
            visible = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise MailMigrationError(f"cannot inspect mail entry {path}: {error}") from error
        if stat.S_ISDIR(visible.st_mode):
            child = None
            try:
                child = _open_child_directory(descriptor, name, path)
                held = os.fstat(child)
                if (
                    held.st_dev != visible.st_dev
                    or held.st_ino != visible.st_ino
                ):
                    raise MailMigrationError(
                        f"mail directory changed while opening: {path}"
                    )
                _scan_directory(child, relative, path, entries, file_ids)
            finally:
                if child is not None:
                    os.close(child)
            continue
        if not stat.S_ISREG(visible.st_mode):
            raise MailMigrationError(
                f"mail tree contains a symlink or special file: {path}"
            )
        _validate_owned_node(visible, path, "mail file", directory=False)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            file_descriptor = os.open(name, flags, dir_fd=descriptor)
        except OSError as error:
            raise MailMigrationError(f"cannot open mail file {path}: {error}") from error
        try:
            held = os.fstat(file_descriptor)
            _validate_owned_node(held, path, "mail file", directory=False)
            identity = (held.st_dev, held.st_ino)
            if identity != (visible.st_dev, visible.st_ino):
                raise MailMigrationError(f"mail file changed while opening: {path}")
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
                while True:
                    chunk = handle.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            after = os.fstat(file_descriptor)
            if (
                (after.st_dev, after.st_ino) != identity
                or after.st_size != held.st_size
                or stat.S_IMODE(after.st_mode) != stat.S_IMODE(held.st_mode)
                or size != held.st_size
            ):
                raise MailMigrationError(f"mail file changed while reading: {path}")
        finally:
            os.close(file_descriptor)
        relative_text = relative.as_posix()
        file_ids[relative_text] = identity
        entries.append(
            {
                "path": relative_text,
                "kind": "file",
                "source_mode": stat.S_IMODE(held.st_mode),
                "size": size,
                "sha256": digest.hexdigest(),
                "hardlink": None,
            }
        )


def scan_mail_tree(root: Path) -> MailSnapshot:
    """Inventory owned inbox/messages bytes without following links."""

    root = Path(root)
    root_fd = _open_owned_directory(root, "mail root")
    entries: list[dict] = []
    file_ids: dict[str, tuple[int, int]] = {}
    try:
        for name in MAIL_TOP_LEVEL:
            path = root / name
            try:
                descriptor = _open_child_directory(root_fd, name, path)
            except FileNotFoundError:
                raise MailMigrationError(
                    f"mail directory is missing: {path}"
                ) from None
            except OSError as error:
                raise MailMigrationError(
                    f"cannot open mail directory {path}: {error}"
                ) from error
            try:
                _scan_directory(
                    descriptor,
                    PurePosixPath(name),
                    path,
                    entries,
                    file_ids,
                )
            finally:
                os.close(descriptor)
    finally:
        os.close(root_fd)

    groups: dict[tuple[int, int], list[str]] = {}
    for relative, identity in file_ids.items():
        groups.setdefault(identity, []).append(relative)
    first_by_identity = {
        identity: sorted(paths)[0]
        for identity, paths in groups.items()
        if len(paths) > 1
    }
    for entry in entries:
        if entry["kind"] != "file":
            continue
        identity = file_ids[entry["path"]]
        aliases = groups[identity]
        try:
            visible = (root / entry["path"]).lstat()
        except OSError as error:
            raise MailMigrationError(
                f"mail file vanished after scan: {root / entry['path']}: {error}"
            ) from error
        if visible.st_nlink != len(aliases):
            raise MailMigrationError(
                f"mail file has hard links outside the mailbox snapshot: "
                f"{root / entry['path']}"
            )
        entry["hardlink"] = first_by_identity.get(identity)
    entries.sort(key=lambda item: item["path"])
    file_count = sum(entry["kind"] == "file" for entry in entries)
    byte_count = sum(
        int(entry.get("size") or 0)
        for entry in entries
        if entry["kind"] == "file"
    )
    return MailSnapshot(
        entries=tuple(entries),
        file_ids=file_ids,
        digest=_snapshot_digest(entries),
        files=file_count,
        bytes=byte_count,
    )


def _assert_snapshot_unchanged(
    root: Path,
    expected: MailSnapshot,
    label: str,
) -> MailSnapshot:
    current = scan_mail_tree(root)
    if (
        current.digest != expected.digest
        or current.entries != expected.entries
    ):
        raise MailMigrationError(f"{label} changed during cutover")
    return current


def _open_relative_parent(root_fd: int, relative: str) -> tuple[int, str]:
    parts = PurePosixPath(relative).parts
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            next_fd = _open_child_directory(current, part, Path(relative))
            os.close(current)
            current = next_fd
        return current, parts[-1]
    except Exception:
        os.close(current)
        raise


def _copy_regular_file(
    source_fd: int,
    destination_fd: int,
    entry: dict,
    expected_identity: tuple[int, int],
    metrics: MigrationMetrics,
) -> None:
    source_parent = destination_parent = None
    source_file = destination_file = None
    relative = entry["path"]
    started = time.monotonic()
    try:
        source_parent, source_name = _open_relative_parent(source_fd, relative)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        source_file = os.open(source_name, flags, dir_fd=source_parent)
        source_info = os.fstat(source_file)
        if (
            not stat.S_ISREG(source_info.st_mode)
            or source_info.st_uid != os.getuid()
            or (source_info.st_dev, source_info.st_ino) != expected_identity
            or source_info.st_size != entry["size"]
            or stat.S_IMODE(source_info.st_mode) != entry["source_mode"]
        ):
            raise MailMigrationError(
                f"mail source changed before copy: {relative}"
            )
        destination_parent, destination_name = _open_relative_parent(
            destination_fd,
            relative,
        )
        destination_file = os.open(
            destination_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=destination_parent,
        )
        os.fchmod(destination_file, 0o600)
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_file, COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_file, view)
                if written == 0:
                    raise MailMigrationError(
                        f"short write while copying mail file {relative}"
                    )
                view = view[written:]
        if copied != entry["size"] or digest.hexdigest() != entry["sha256"]:
            raise MailMigrationError(
                f"mail source changed during copy: {relative}"
            )
        after = os.fstat(source_file)
        if (
            (after.st_dev, after.st_ino) != expected_identity
            or after.st_size != source_info.st_size
            or stat.S_IMODE(after.st_mode) != stat.S_IMODE(source_info.st_mode)
        ):
            raise MailMigrationError(
                f"mail source changed during copy: {relative}"
            )
        metrics.fsync(destination_file)
    except OSError as error:
        raise MailMigrationError(
            f"cannot copy mail file {relative}: {error}"
        ) from error
    finally:
        metrics.copy_seconds += time.monotonic() - started
        for descriptor in (
            source_file,
            destination_file,
            source_parent,
            destination_parent,
        ):
            if descriptor is not None:
                os.close(descriptor)


def copy_snapshot(
    source_root: Path,
    destination_root: Path,
    snapshot: MailSnapshot,
    metrics: MigrationMetrics,
) -> None:
    """Copy one exact snapshot into an empty private root, preserving links."""

    source_fd = _open_owned_directory(source_root, "mail copy source")
    destination_fd = _open_owned_directory(
        destination_root,
        "mail copy destination",
        exact_private=True,
    )
    try:
        if os.listdir(destination_fd):
            raise MailMigrationError(
                f"mail copy destination is not empty: {destination_root}"
            )
        directories = [
            entry
            for entry in snapshot.entries
            if entry["kind"] == "directory"
        ]
        for entry in sorted(
            directories,
            key=lambda item: (
                len(PurePosixPath(item["path"]).parts),
                item["path"],
            ),
        ):
            try:
                os.mkdir(
                    entry["path"],
                    0o700,
                    dir_fd=destination_fd,
                )
                directory_fd, _unused = _open_relative_parent(
                    destination_fd,
                    f"{entry['path']}/placeholder",
                )
                try:
                    os.fchmod(directory_fd, 0o700)
                finally:
                    os.close(directory_fd)
            except OSError as error:
                raise MailMigrationError(
                    f"cannot create copied mail directory "
                    f"{destination_root / entry['path']}: {error}"
                ) from error

        files = [
            entry for entry in snapshot.entries if entry["kind"] == "file"
        ]
        for entry in sorted(files, key=lambda item: item["path"]):
            first = entry.get("hardlink")
            if first and first != entry["path"]:
                started = time.monotonic()
                try:
                    os.link(
                        first,
                        entry["path"],
                        src_dir_fd=destination_fd,
                        dst_dir_fd=destination_fd,
                        follow_symlinks=False,
                    )
                except OSError as error:
                    raise MailMigrationError(
                        f"cannot preserve mail hardlink "
                        f"{entry['path']} -> {first}: {error}"
                    ) from error
                finally:
                    metrics.copy_seconds += time.monotonic() - started
                continue
            _copy_regular_file(
                source_fd,
                destination_fd,
                entry,
                snapshot.file_ids[entry["path"]],
                metrics,
            )

        for entry in sorted(
            directories,
            key=lambda item: (
                -len(PurePosixPath(item["path"]).parts),
                item["path"],
            ),
        ):
            directory_fd, _unused = _open_relative_parent(
                destination_fd,
                f"{entry['path']}/placeholder",
            )
            try:
                metrics.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        metrics.fsync(destination_fd)
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def verify_snapshot_copy(
    root: Path,
    expected_entries: list[dict] | tuple[dict, ...],
) -> MailSnapshot:
    actual = scan_mail_tree(root)
    expected_semantic = [_semantic_entry(entry) for entry in expected_entries]
    actual_semantic = [_semantic_entry(entry) for entry in actual.entries]
    if actual_semantic != expected_semantic:
        raise MailMigrationError(
            f"verified mailbox copy differs from its manifest: {root}"
        )
    for entry in actual.entries:
        expected_mode = (
            PRIVATE_DIRECTORY_MODE
            if entry["kind"] == "directory"
            else PRIVATE_FILE_MODE
        )
        if entry["source_mode"] != expected_mode:
            raise MailMigrationError(
                f"verified mailbox copy has mode "
                f"{entry['source_mode']:04o}, expected {expected_mode:04o}: "
                f"{root / entry['path']}"
            )
    return actual


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish a path without replacing an existing destination."""

    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        function = libc.renameatx_np
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            -2,
            source_bytes,
            -2,
            destination_bytes,
            0x00000004,
        )
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            -100,
            source_bytes,
            -100,
            destination_bytes,
            0x00000001,
        )
    else:
        raise MailMigrationError(
            "atomic no-clobber directory publication is unsupported "
            f"on {sys.platform}"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), destination)


def _registry_evidence(
    registry: Path,
    project_uuid: str,
    project_root: Path,
) -> tuple[bytes, dict]:
    try:
        document, payload = _read_projects_snapshot(registry)
    except (ProjectRegistryError, ValueError) as error:
        raise MailMigrationError(
            f"cannot read strict project registry {registry}: {error}"
        ) from error
    if payload is None:
        raise MailMigrationError(f"project registry is missing: {registry}")
    entries, _warnings = _strict_entries_from_document(document, registry)
    matches = [
        entry
        for entry in entries
        if entry["uuid"] == project_uuid
    ]
    if len(matches) != 1 or matches[0]["status"] != "active":
        raise MailMigrationError(
            f"project {project_uuid} is not one exact active registry row"
        )
    if matches[0]["root"] != project_root:
        raise MailMigrationError(
            f"project registry path is not reconciled: "
            f"{matches[0]['root']} != {project_root}"
        )
    raw_matches = [
        raw
        for raw in document["projects"]
        if isinstance(raw, dict) and raw.get("uuid") == project_uuid
    ]
    if len(raw_matches) != 1:
        raise MailMigrationError(
            f"project {project_uuid} has {len(raw_matches)} raw registry rows"
        )
    return payload, json.loads(json.dumps(raw_matches[0]))


def _flip_layout(
    registry: Path,
    project_uuid: str,
    project_root: Path,
    expected: str,
    desired: str,
    *,
    guard,
    lock_timeout: float,
    metrics: MigrationMetrics,
) -> None:
    def mutate(document, _source_payload, _parent_fd):
        entries, _warnings = _strict_entries_from_document(document, registry)
        matches = [
            entry for entry in entries if entry["uuid"] == project_uuid
        ]
        if len(matches) != 1:
            raise MailMigrationError(
                f"project {project_uuid} has {len(matches)} registry rows"
            )
        entry = matches[0]
        if entry["status"] != "active":
            raise MailMigrationError(
                f"project {project_uuid} is not active"
            )
        if entry["root"] != project_root:
            raise MailMigrationError(
                f"project path changed before cutover: "
                f"{entry['root']} != {project_root}"
            )
        if entry["layout"] != expected:
            raise MailMigrationError(
                f"project layout changed before cutover: "
                f"{entry['layout']} != {expected}"
            )
        raw_matches = [
            raw
            for raw in document["projects"]
            if isinstance(raw, dict) and raw.get("uuid") == project_uuid
        ]
        if len(raw_matches) != 1:
            raise MailMigrationError(
                f"project {project_uuid} has {len(raw_matches)} raw rows"
            )
        raw_matches[0]["layout"] = desired
        return True

    started = time.monotonic()
    try:
        changed = _update_project_registry(
            mutate,
            registry,
            guard=guard,
            lock_timeout=lock_timeout,
        )
    finally:
        metrics.registry_flip_seconds += time.monotonic() - started
    if not changed:
        raise MailMigrationError(
            f"project layout cutover did not change {expected} to {desired}"
        )


def _current_layout(registry: Path, project_uuid: str) -> str:
    document, _payload = _read_projects_snapshot(registry)
    entries, _warnings = _strict_entries_from_document(document, registry)
    matches = [
        entry for entry in entries if entry["uuid"] == project_uuid
    ]
    if len(matches) != 1:
        raise MailMigrationError(
            f"project {project_uuid} has {len(matches)} registry rows"
        )
    return matches[0]["layout"]


def _commit_observed_after_error(
    registry: Path,
    project_uuid: str,
    desired: str,
    cutover_error: Exception,
) -> bool:
    try:
        return _current_layout(registry, project_uuid) == desired
    except Exception as probe_error:
        unknown = MailMigrationCommitUnknownError(
            "registry cutover outcome is unknown; "
            f"cutover error: {cutover_error}; "
            f"registry re-read error: {probe_error}"
        )
        unknown.cutover_error = cutover_error
        unknown.probe_error = probe_error
        raise unknown from cutover_error


def _ensure_source_mail_directories(
    state_dir: Path,
    metrics: MigrationMetrics,
) -> None:
    changed = False
    for name in MAIL_TOP_LEVEL:
        path = state_dir / name
        info = _path_info(path)
        if info is None:
            path.mkdir(mode=0o700)
            path.chmod(0o700)
            changed = True
            continue
        _validate_owned_node(info, path, "mail directory", directory=True)
    locks = state_dir / "locks"
    info = _path_info(locks)
    if info is None:
        locks.mkdir(mode=0o700)
        locks.chmod(0o700)
        changed = True
    else:
        _validate_owned_node(info, locks, "mail locks", directory=True)
    if changed:
        _timed_fsync_directory(state_dir, metrics)


def _manifest_entries(document: dict) -> list[dict]:
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise MailMigrationError("migration manifest has no entry inventory")
    normalized = []
    seen = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise MailMigrationError("migration manifest entry is not a mapping")
        path = _safe_relative_path(raw.get("path"))
        if path in seen:
            raise MailMigrationError(
                f"migration manifest duplicates path {path}"
            )
        seen.add(path)
        kind = raw.get("kind")
        source_mode = raw.get("source_mode")
        if kind not in {"directory", "file"}:
            raise MailMigrationError(
                f"migration manifest has invalid kind for {path}: {kind!r}"
            )
        if (
            not isinstance(source_mode, int)
            or isinstance(source_mode, bool)
            or source_mode < 0
            or source_mode > 0o7777
            or source_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise MailMigrationError(
                f"migration manifest has unsafe mode for {path}"
            )
        entry = {
            "path": path,
            "kind": kind,
            "source_mode": source_mode,
        }
        if kind == "file":
            size = raw.get("size")
            digest = raw.get("sha256")
            hardlink = raw.get("hardlink")
            if (
                not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
                or hardlink is not None
                and _safe_relative_path(hardlink) not in seen
            ):
                raise MailMigrationError(
                    f"migration manifest has invalid file metadata for {path}"
                )
            entry.update(
                {
                    "size": size,
                    "sha256": digest,
                    "hardlink": hardlink,
                }
            )
        normalized.append(entry)
    if normalized != sorted(normalized, key=lambda item: item["path"]):
        raise MailMigrationError("migration manifest inventory is not sorted")
    if {entry["path"] for entry in normalized if entry["kind"] == "directory"} < {
        "inbox",
        "messages",
    }:
        raise MailMigrationError(
            "migration manifest is missing inbox/messages roots"
        )
    if _snapshot_digest(normalized) != document.get("snapshot_digest"):
        raise MailMigrationError(
            "migration manifest snapshot digest does not match its inventory"
        )
    return normalized


def load_manifest(
    manifest_path: Path | str,
    *,
    expected_uuid: str,
    expected_registry: Path,
) -> tuple[Path, dict, str]:
    path = _manifest_path(manifest_path)
    payload = _read_private_file(path, "migration manifest")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MailMigrationError(
            f"cannot parse migration manifest {path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise MailMigrationError(
            f"migration manifest is not a mapping: {path}"
        )
    if document.get("schema") != MIGRATION_MANIFEST_SCHEMA:
        raise MailMigrationError(
            f"unsupported migration manifest schema: {document.get('schema')!r}"
        )
    try:
        operation_id = str(uuid.UUID(document.get("operation_id")))
    except (ValueError, TypeError, AttributeError) as error:
        raise MailMigrationError(
            f"migration manifest has invalid operation UUID: {path}"
        ) from error
    if operation_id != document.get("operation_id"):
        raise MailMigrationError(
            f"migration manifest operation UUID is not canonical: {path}"
        )
    if document.get("project_uuid") != expected_uuid:
        raise MailMigrationError(
            f"migration manifest UUID does not match {expected_uuid}: {path}"
        )
    if document.get("registry_path") != str(expected_registry):
        raise MailMigrationError(
            f"migration manifest registry does not match "
            f"{expected_registry}: {path}"
        )
    if document.get("bundle") != str(path.parent):
        raise MailMigrationError(
            f"migration manifest bundle binding is invalid: {path}"
        )
    direction = document.get("direction")
    if direction not in {"local-to-central", "central-to-local"}:
        raise MailMigrationError(
            f"migration manifest has invalid direction: {direction!r}"
        )
    expected_layouts = {
        "local-to-central": ("local", "central"),
        "central-to-local": ("central", "local"),
    }[direction]
    if (
        document.get("source_layout"),
        document.get("target_layout"),
    ) != expected_layouts:
        raise MailMigrationError(
            f"migration manifest layout binding is invalid: {path}"
        )
    expected_central = central_mail_root(
        expected_registry,
        expected_uuid,
    )
    if document.get("central_root") != str(expected_central):
        raise MailMigrationError(
            f"migration manifest central root is invalid: {path}"
        )
    if _paths_overlap(path.parent, expected_central):
        raise MailMigrationError(
            f"migration manifest is stored inside central mail: {path}"
        )
    entries = _manifest_entries(document)
    registry_before = _read_private_file(
        path.parent / "registry.before.yaml",
        "migration registry backup",
    )
    if _sha256(registry_before) != document.get("registry_sha256"):
        raise MailMigrationError(
            f"migration registry backup digest mismatch: {path.parent}"
        )
    witness = _read_private_file(
        path.parent / "project.before.json",
        "migration project witness backup",
    )
    if _sha256(witness) != document.get("project_witness_sha256"):
        raise MailMigrationError(
            f"migration project witness digest mismatch: {path.parent}"
        )
    verify_snapshot_copy(path.parent / "payload", entries)
    return path, document, _sha256(payload)


def _create_backup(
    *,
    source_root: Path,
    source_snapshot: MailSnapshot,
    registry: Path,
    registry_payload: bytes,
    registry_row: dict,
    project_root: Path,
    project_uuid: str,
    direction: str,
    backup_root: Path,
    operation_id: str,
    operation_name: str,
    central_root: Path,
    source_manifest: str | None,
    metrics: MigrationMetrics,
    forbidden_roots: tuple[Path, ...],
    forbidden_bundle_roots: tuple[Path, ...] = (),
) -> tuple[Path, dict, str]:
    _validate_backup_location(
        backup_root,
        project_uuid,
        forbidden_roots,
        operation_name=operation_name,
        forbidden_bundle_roots=forbidden_bundle_roots,
    )
    base = _ensure_private_directory(
        backup_root,
        "migration backup root",
        metrics,
    )
    uuid_root = _ensure_private_directory(
        base / project_uuid,
        "project migration backup root",
        metrics,
    )
    temporary = uuid_root / f".{operation_name}.tmp"
    bundle = uuid_root / operation_name
    if _path_info(bundle) is not None:
        raise MailMigrationError(f"migration backup collision: {bundle}")
    try:
        temporary.mkdir(mode=0o700)
        temporary.chmod(0o700)
    except OSError as error:
        raise MailMigrationError(
            f"cannot create migration backup staging {temporary}: {error}"
        ) from error
    try:
        payload_root = temporary / "payload"
        payload_root.mkdir(mode=0o700)
        payload_root.chmod(0o700)
        copy_snapshot(source_root, payload_root, source_snapshot, metrics)
        verify_snapshot_copy(payload_root, source_snapshot.entries)
        project_witness = _read_owned_regular_bytes(
            project_identity_path(project_root),
            "project identity witness",
        )
        _write_private_file(
            temporary / "registry.before.yaml",
            registry_payload,
            metrics,
        )
        _write_private_file(
            temporary / "project.before.json",
            project_witness,
            metrics,
        )
        manifest = {
            "schema": MIGRATION_MANIFEST_SCHEMA,
            "operation_id": operation_id,
            "created_at": _utc_now(),
            "direction": direction,
            "project_uuid": project_uuid,
            "project_root": str(project_root),
            "registry_path": str(registry),
            "registry_sha256": _sha256(registry_payload),
            "registry_row": registry_row,
            "project_witness_sha256": _sha256(project_witness),
            "source_layout": (
                "local" if direction == "local-to-central" else "central"
            ),
            "target_layout": (
                "central" if direction == "local-to-central" else "local"
            ),
            "snapshot_digest": source_snapshot.digest,
            "entries": list(source_snapshot.entries),
            "files": source_snapshot.files,
            "bytes": source_snapshot.bytes,
            "bundle": str(bundle),
            "central_root": str(central_root),
            "bookmark": str(project_root / ".roundtable" / "mail"),
            "source_manifest": source_manifest,
        }
        manifest_payload = _json_bytes(manifest)
        _write_private_file(
            temporary / "manifest.json",
            manifest_payload,
            metrics,
        )
        _timed_fsync_directory(temporary, metrics)
        _maybe_fail("after_backup_staging")
        _rename_noreplace(temporary, bundle)
        _timed_fsync_directory(uuid_root, metrics)
        path, verified, digest = load_manifest(
            bundle / "manifest.json",
            expected_uuid=project_uuid,
            expected_registry=registry,
        )
        if verified != manifest or digest != _sha256(manifest_payload):
            raise MailMigrationError(
                f"published migration backup verification failed: {bundle}"
            )
        return path, verified, digest
    except Exception:
        if _path_info(temporary) is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def _write_central_marker(
    staging: Path,
    *,
    project_uuid: str,
    operation_id: str,
    manifest: Path,
    manifest_sha256: str,
    snapshot_digest: str,
    metrics: MigrationMetrics,
) -> dict:
    marker = {
        "schema": CENTRAL_MAIL_MARKER_SCHEMA,
        "project_uuid": project_uuid,
        "operation_id": operation_id,
        "manifest": str(manifest),
        "manifest_sha256": manifest_sha256,
        "snapshot_digest": snapshot_digest,
        "created_at": _utc_now(),
    }
    _write_private_file(
        staging / CENTRAL_MAIL_MARKER_NAME,
        _json_bytes(marker),
        metrics,
    )
    return marker


def _verify_central_generation(
    root: Path,
    project_uuid: str,
    registry: Path,
    *,
    expected_manifest: Path | None = None,
    expected_source_digest: str | None = None,
) -> tuple[dict, Path, dict]:
    expected_root = central_mail_root(registry, project_uuid)
    if root != expected_root:
        raise MailMigrationError(
            f"central generation path is not UUID-derived: {root}"
        )
    marker = validate_central_mail_marker(root, project_uuid)
    manifest_path, manifest, manifest_digest = load_manifest(
        marker["manifest"],
        expected_uuid=project_uuid,
        expected_registry=registry,
    )
    if marker["manifest_sha256"] != manifest_digest:
        raise MailMigrationError(
            f"central generation manifest digest mismatch: {root}"
        )
    if marker["operation_id"] != manifest["operation_id"]:
        raise MailMigrationError(
            f"central generation operation mismatch: {root}"
        )
    if marker["snapshot_digest"] != manifest["snapshot_digest"]:
        raise MailMigrationError(
            f"central generation snapshot mismatch: {root}"
        )
    if manifest["direction"] != "local-to-central":
        raise MailMigrationError(
            f"central generation is not a forward manifest: {root}"
        )
    if expected_manifest is not None and manifest_path != expected_manifest:
        raise MailMigrationError(
            f"central generation is bound to {manifest_path}, "
            f"not requested manifest {expected_manifest}"
        )
    if (
        expected_source_digest is not None
        and manifest["snapshot_digest"] != expected_source_digest
    ):
        raise MailMigrationError(
            f"central generation is stale for current local snapshot: {root}"
        )
    # The immutable manifest describes the cutover snapshot. Once the
    # registry points at central, ordinary delivery legitimately changes the
    # live tree, so exact byte comparison is only valid for a pre-cutover
    # candidate or immediately after its publication.
    if expected_source_digest is not None:
        verify_snapshot_copy(root, manifest["entries"])
    return marker, manifest_path, manifest


def _publish_central_generation(
    *,
    verified_backup: Path,
    manifest: dict,
    manifest_path: Path,
    manifest_digest: str,
    central_root_path: Path,
    project_uuid: str,
    operation_id: str,
    metrics: MigrationMetrics,
) -> None:
    mail_parent = _ensure_private_directory(
        central_root_path.parent,
        "central mail parent",
        metrics,
    )
    staging = mail_parent / f".{project_uuid}.prepare.{operation_id}"
    if _path_info(staging) is not None:
        raise MailMigrationError(
            f"central mail staging collision: {staging}"
        )
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    try:
        backup_snapshot = scan_mail_tree(verified_backup / "payload")
        copy_snapshot(
            verified_backup / "payload",
            staging,
            backup_snapshot,
            metrics,
        )
        verify_snapshot_copy(staging, manifest["entries"])
        locks = staging / "locks"
        locks.mkdir(mode=0o700)
        locks.chmod(0o700)
        _write_central_marker(
            staging,
            project_uuid=project_uuid,
            operation_id=operation_id,
            manifest=manifest_path,
            manifest_sha256=manifest_digest,
            snapshot_digest=manifest["snapshot_digest"],
            metrics=metrics,
        )
        _timed_fsync_directory(locks, metrics)
        _timed_fsync_directory(staging, metrics)
        _maybe_fail("before_central_publish")
        _rename_noreplace(staging, central_root_path)
        _timed_fsync_directory(mail_parent, metrics)
        _maybe_fail("after_central_publish")
        _verify_central_generation(
            central_root_path,
            project_uuid,
            _registry_path(manifest["registry_path"]),
            expected_manifest=manifest_path,
            expected_source_digest=manifest["snapshot_digest"],
        )
    except Exception:
        if _path_info(staging) is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _quarantine_central(
    central_root_path: Path,
    project_uuid: str,
    operation_id: str,
    *,
    registry: Path,
    expected_manifest: Path,
    require_pristine: bool,
    suffix: str,
    metrics: MigrationMetrics,
) -> Path:
    _marker, _manifest_path_value, manifest = _verify_central_generation(
        central_root_path,
        project_uuid,
        registry,
        expected_manifest=expected_manifest,
    )
    if require_pristine:
        verify_snapshot_copy(
            central_root_path,
            manifest["entries"],
        )
    destination = central_root_path.parent / (
        f".{project_uuid}.{suffix}.{operation_id}"
    )
    _rename_noreplace(central_root_path, destination)
    _timed_fsync_directory(central_root_path.parent, metrics)
    return destination


def _gitignore_content(content: str) -> str:
    lines = content.splitlines()
    try:
        begin = lines.index(GITIGNORE_BEGIN)
    except ValueError:
        begin = -1
    if begin >= 0:
        try:
            end = lines.index(GITIGNORE_END, begin + 1)
        except ValueError as error:
            raise MailMigrationError(
                "project .gitignore has an unterminated Roundtable "
                "central-mail block"
            ) from error
        lines[begin : end + 1] = list(GITIGNORE_BLOCK)
    else:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(GITIGNORE_BLOCK)
    return "\n".join(lines) + "\n"


def _ensure_gitignore(state_dir: Path, metrics: MigrationMetrics) -> None:
    path = state_dir / ".gitignore"
    info = _path_info(path)
    if info is None:
        original = b""
        mode = 0o644
    else:
        _validate_owned_node(info, path, "project .gitignore", directory=False)
        original = _read_owned_regular_bytes(path, "project .gitignore")
        mode = stat.S_IMODE(info.st_mode)
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MailMigrationError(
            f"project .gitignore is not UTF-8: {path}"
        ) from error
    updated = _gitignore_content(text).encode("utf-8")
    if updated == original:
        return
    temporary = state_dir / f".gitignore.tmp.{os.getpid()}.{time.time_ns()}"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(updated)
            handle.flush()
            metrics.fsync(handle.fileno())
        os.replace(temporary, path)
        _timed_fsync_directory(state_dir, metrics)
    except OSError as error:
        raise MailMigrationError(
            f"cannot update project .gitignore {path}: {error}"
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _bookmark_is_tracked(project_root: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "ls-files",
                "--error-unmatch",
                "--",
                ".roundtable/mail",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
            env=_clean_git_environment(),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _bookmark_target(bookmark: Path) -> str | None:
    info = _path_info(bookmark)
    if info is None:
        return None
    if not stat.S_ISLNK(info.st_mode):
        raise MailMigrationError(
            f"central mail bookmark collision is not a symlink: {bookmark}"
        )
    try:
        return os.readlink(bookmark)
    except OSError as error:
        raise MailMigrationError(
            f"cannot read central mail bookmark {bookmark}: {error}"
        ) from error


def _preflight_bookmark(
    project_root: Path,
    central_root_path: Path,
    *,
    allow_exact: bool,
) -> None:
    bookmark = project_root / ".roundtable" / "mail"
    if _bookmark_is_tracked(project_root):
        raise MailMigrationError(
            f"central mail bookmark path is tracked by Git: {bookmark}"
        )
    target = _bookmark_target(bookmark)
    if target is None:
        return
    if allow_exact and target == str(central_root_path):
        return
    raise MailMigrationError(
        f"central mail bookmark collision at {bookmark}: {target!r}"
    )


def _install_bookmark(
    project_root: Path,
    central_root_path: Path,
    metrics: MigrationMetrics,
) -> None:
    state_dir = project_root / ".roundtable"
    bookmark = state_dir / "mail"
    _preflight_bookmark(
        project_root,
        central_root_path,
        allow_exact=True,
    )
    if _bookmark_target(bookmark) is None:
        try:
            os.symlink(str(central_root_path), bookmark)
        except OSError as error:
            raise MailMigrationError(
                f"cannot create central mail bookmark {bookmark}: {error}"
            ) from error
        _timed_fsync_directory(state_dir, metrics)
    if _bookmark_target(bookmark) != str(central_root_path):
        raise MailMigrationError(
            f"central mail bookmark verification failed: {bookmark}"
        )


def _remove_bookmark(
    project_root: Path,
    central_root_path: Path,
    metrics: MigrationMetrics,
) -> None:
    bookmark = project_root / ".roundtable" / "mail"
    target = _bookmark_target(bookmark)
    if target is None:
        return
    if target != str(central_root_path):
        raise MailMigrationError(
            f"refusing to remove foreign central mail bookmark "
            f"{bookmark}: {target!r}"
        )
    bookmark.unlink()
    _timed_fsync_directory(bookmark.parent, metrics)


def _validate_locks_tree(path: Path) -> None:
    info = _path_info(path)
    if info is None:
        return
    _validate_owned_node(info, path, "mail locks", directory=True)
    descriptor = _open_owned_directory(path, "mail locks")
    entries: list[dict] = []
    file_ids: dict[str, tuple[int, int]] = {}
    try:
        _scan_directory(
            descriptor,
            PurePosixPath("locks"),
            path,
            entries,
            file_ids,
        )
    finally:
        os.close(descriptor)


def _quarantine_local_mail(
    state_dir: Path,
    operation_id: str,
    metrics: MigrationMetrics,
) -> Path | None:
    existing = [
        name
        for name in (*MAIL_TOP_LEVEL, "locks")
        if _path_info(state_dir / name) is not None
    ]
    if not existing:
        return None
    for name in existing:
        path = state_dir / name
        if name == "locks":
            _validate_locks_tree(path)
        else:
            info = path.lstat()
            _validate_owned_node(info, path, "local mail directory", directory=True)
    quarantine = state_dir / f".central-mail-retired.{operation_id}"
    if _path_info(quarantine) is not None:
        quarantine = state_dir / (
            f".central-mail-retired.{operation_id}.{uuid.uuid4()}"
        )
    quarantine.mkdir(mode=0o700)
    quarantine.chmod(0o700)
    for name in existing:
        _rename_noreplace(state_dir / name, quarantine / name)
    _timed_fsync_directory(quarantine, metrics)
    _timed_fsync_directory(state_dir, metrics)
    return quarantine


def _cleanup_local_after_forward(
    state_dir: Path,
    expected: list[dict],
    operation_id: str,
    metrics: MigrationMetrics,
) -> list[str]:
    existing_mail = [
        name
        for name in MAIL_TOP_LEVEL
        if _path_info(state_dir / name) is not None
    ]
    locks_exist = _path_info(state_dir / "locks") is not None
    if not existing_mail and not locks_exist:
        return []
    if len(existing_mail) == len(MAIL_TOP_LEVEL):
        current = scan_mail_tree(state_dir)
        expected_digest = _snapshot_digest(expected)
        drifted = current.digest != expected_digest
    elif not existing_mail:
        drifted = False
    else:
        # A crash may have retired only one top-level directory. Preserve the
        # remainder as non-authoritative evidence rather than pretending the
        # incomplete tree still matches the forward manifest.
        drifted = True
    quarantine = _quarantine_local_mail(state_dir, operation_id, metrics)
    if quarantine is None:
        return []
    if drifted:
        return [
            f"preserved changed non-authoritative local mail at {quarantine}"
        ]
    shutil.rmtree(quarantine)
    _timed_fsync_directory(state_dir, metrics)
    return []


def _write_rollback_marker(
    state_dir: Path,
    *,
    project_uuid: str,
    source_manifest: Path,
    source_manifest_sha256: str,
    rollback_manifest: Path,
    snapshot_digest: str,
    operation_id: str,
    metrics: MigrationMetrics,
) -> None:
    marker = {
        "schema": ROLLBACK_MARKER_SCHEMA,
        "project_uuid": project_uuid,
        "operation_id": operation_id,
        "source_manifest": str(source_manifest),
        "source_manifest_sha256": source_manifest_sha256,
        "rollback_manifest": str(rollback_manifest),
        "snapshot_digest": snapshot_digest,
        "created_at": _utc_now(),
    }
    path = state_dir / ROLLBACK_MARKER_NAME
    temporary = state_dir / f"{ROLLBACK_MARKER_NAME}.tmp.{operation_id}"
    if _path_info(temporary) is not None:
        raise MailMigrationError(
            f"rollback marker staging collision: {temporary}"
        )
    _write_private_file(temporary, _json_bytes(marker), metrics)
    os.replace(temporary, path)
    _timed_fsync_directory(state_dir, metrics)


def _load_rollback_marker(
    state_dir: Path,
    *,
    project_uuid: str,
    source_manifest: Path,
    source_manifest_sha256: str,
) -> dict:
    path = state_dir / ROLLBACK_MARKER_NAME
    payload = _read_private_file(path, "mail rollback marker")
    try:
        marker = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MailMigrationError(
            f"cannot parse mail rollback marker {path}: {error}"
        ) from error
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != ROLLBACK_MARKER_SCHEMA
        or marker.get("project_uuid") != project_uuid
        or marker.get("source_manifest") != str(source_manifest)
        or marker.get("source_manifest_sha256") != source_manifest_sha256
    ):
        raise MailMigrationError(
            f"mail rollback marker does not match requested manifest: {path}"
        )
    return marker


def _preflight_rollback_marker(state_dir: Path, project_uuid: str) -> None:
    path = state_dir / ROLLBACK_MARKER_NAME
    if _path_info(path) is None:
        return
    payload = _read_private_file(path, "mail rollback marker")
    try:
        marker = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MailMigrationError(
            f"cannot parse existing mail rollback marker {path}: {error}"
        ) from error
    if (
        not isinstance(marker, dict)
        or marker.get("schema") != ROLLBACK_MARKER_SCHEMA
        or marker.get("project_uuid") != project_uuid
    ):
        raise MailMigrationError(
            f"refusing to replace foreign mail rollback marker: {path}"
        )


def _prepare_local_candidate(
    *,
    state_dir: Path,
    verified_backup: Path,
    source_snapshot: MailSnapshot,
    original_manifest: Path,
    original_manifest_sha256: str,
    rollback_manifest: Path,
    project_uuid: str,
    operation_id: str,
    metrics: MigrationMetrics,
) -> Path | None:
    staging = state_dir / f".mail-rollback.prepare.{operation_id}"
    staging.mkdir(mode=0o700)
    staging.chmod(0o700)
    try:
        backup_snapshot = scan_mail_tree(verified_backup / "payload")
        copy_snapshot(
            verified_backup / "payload",
            staging,
            backup_snapshot,
            metrics,
        )
        verify_snapshot_copy(staging, source_snapshot.entries)
        locks = staging / "locks"
        locks.mkdir(mode=0o700)
        locks.chmod(0o700)
        _timed_fsync_directory(locks, metrics)
        _timed_fsync_directory(staging, metrics)

        quarantine = _quarantine_local_mail(
            state_dir,
            operation_id,
            metrics,
        )
        for name in (*MAIL_TOP_LEVEL, "locks"):
            _rename_noreplace(staging / name, state_dir / name)
        _timed_fsync_directory(state_dir, metrics)
        staging.rmdir()
        _write_rollback_marker(
            state_dir,
            project_uuid=project_uuid,
            source_manifest=original_manifest,
            source_manifest_sha256=original_manifest_sha256,
            rollback_manifest=rollback_manifest,
            snapshot_digest=source_snapshot.digest,
            operation_id=operation_id,
            metrics=metrics,
        )
        verify_snapshot_copy(state_dir, source_snapshot.entries)
        return quarantine
    except Exception:
        if _path_info(staging) is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _result(
    *,
    operation: str,
    status: str,
    manifest: Path,
    rollback_manifest: Path | None,
    snapshot: MailSnapshot,
    lock_wait: float,
    exclusive_hold: float,
    metrics: MigrationMetrics,
    committed: bool,
    warnings: list[str],
) -> dict:
    return {
        "operation": operation,
        "status": status,
        "manifest": str(manifest),
        "rollback_manifest": (
            str(rollback_manifest) if rollback_manifest is not None else None
        ),
        "files": snapshot.files,
        "bytes": snapshot.bytes,
        "lock_wait_ms": round(lock_wait * 1000, 3),
        "exclusive_hold_ms": round(exclusive_hold * 1000, 3),
        "copy_ms": round(metrics.copy_seconds * 1000, 3),
        "fsync_ms": round(metrics.fsync_seconds * 1000, 3),
        "registry_flip_ms": round(
            metrics.registry_flip_seconds * 1000,
            3,
        ),
        "committed": committed,
        "warnings": warnings,
    }


def migrate_project(
    project: Path | str,
    *,
    registry_path: Path | str | None = None,
    backup_root: Path | str | None = None,
    layout_lock_timeout: float = 10.0,
    registry_lock_timeout: float = 10.0,
) -> dict:
    """Migrate one exact local mailbox into the UUID central store."""

    registry = _registry_path(registry_path)
    root = Path(project).expanduser().resolve()
    initial = resolve_project_mailbox_checked(
        root,
        registry_path=registry,
        reconcile=True,
    )
    project_uuid = initial.project_uuid
    central = central_mail_root(registry, project_uuid)
    selected_backup_root = (
        DEFAULT_BACKUP_ROOT
        if backup_root is None
        else Path(backup_root).expanduser()
    )
    metrics = MigrationMetrics()
    operation_id, operation_name = _operation_name()
    lock_started = time.monotonic()
    entered_at = None
    committed = False
    manifest_path: Path | None = None
    snapshot: MailSnapshot | None = None
    warnings: list[str] = []
    status = "migrated"
    try:
        with locked_project_mailbox_checked(
            root,
            registry_path=registry,
            exclusive=True,
            timeout=layout_lock_timeout,
        ) as mailbox:
            entered_at = time.monotonic()
            _registry_evidence(registry, project_uuid, root)
            root_fd, state_fd = _open_project_guard(root)
            try:
                def guard():
                    _verify_project_guard(root, root_fd, state_fd)
                    witness = _read_project_identity_at(root, state_fd)
                    if witness != project_uuid:
                        raise MailMigrationError(
                            f"project identity changed during migration: "
                            f"{witness} != {project_uuid}"
                        )

                guard()
                _preflight_bookmark(
                    root,
                    central,
                    allow_exact=mailbox.layout == "central",
                )
                if mailbox.layout == "central":
                    _ensure_gitignore(mailbox.state_dir, metrics)
                    marker, manifest_path, manifest = (
                        _verify_central_generation(
                            central,
                            project_uuid,
                            registry,
                        )
                    )
                    snapshot = scan_mail_tree(central)
                    committed = True
                    status = "already central"
                    warnings.extend(
                        _cleanup_local_after_forward(
                            mailbox.state_dir,
                            manifest["entries"],
                            marker["operation_id"],
                            metrics,
                        )
                    )
                    _install_bookmark(root, central, metrics)
                else:
                    forward_forbidden_roots = (
                        mailbox.state_dir,
                        central.parent,
                        registry.parent / "layout-locks",
                    )
                    _validate_backup_location(
                        selected_backup_root,
                        project_uuid,
                        forward_forbidden_roots,
                    )
                    _ensure_source_mail_directories(
                        mailbox.state_dir,
                        metrics,
                    )
                    snapshot = scan_mail_tree(mailbox.state_dir)
                    registry_payload, registry_row = _registry_evidence(
                        registry,
                        project_uuid,
                        root,
                    )
                    reusable = False
                    stale_owned = False
                    stale_manifest_path: Path | None = None
                    if _path_info(central) is not None:
                        (
                            _marker,
                            candidate_manifest_path,
                            candidate_manifest,
                        ) = _verify_central_generation(
                            central,
                            project_uuid,
                            registry,
                        )
                        verify_snapshot_copy(
                            central,
                            candidate_manifest["entries"],
                        )
                        if (
                            candidate_manifest["snapshot_digest"]
                            == snapshot.digest
                        ):
                            manifest_path = candidate_manifest_path
                            reusable = True
                        else:
                            stale_owned = True
                            stale_manifest_path = candidate_manifest_path
                    if not reusable:
                        (
                            manifest_path,
                            manifest,
                            manifest_digest,
                        ) = _create_backup(
                            source_root=mailbox.state_dir,
                            source_snapshot=snapshot,
                            registry=registry,
                            registry_payload=registry_payload,
                            registry_row=registry_row,
                            project_root=root,
                            project_uuid=project_uuid,
                            direction="local-to-central",
                            backup_root=selected_backup_root,
                            operation_id=operation_id,
                            operation_name=operation_name,
                            central_root=central,
                            source_manifest=None,
                            metrics=metrics,
                            forbidden_roots=forward_forbidden_roots,
                        )
                        _maybe_fail("after_backup")
                        if stale_owned:
                            assert stale_manifest_path is not None
                            _quarantine_central(
                                central,
                                project_uuid,
                                operation_id,
                                registry=registry,
                                expected_manifest=stale_manifest_path,
                                require_pristine=True,
                                suffix="stale",
                                metrics=metrics,
                            )
                        _publish_central_generation(
                            verified_backup=manifest_path.parent,
                            manifest=manifest,
                            manifest_path=manifest_path,
                            manifest_digest=manifest_digest,
                            central_root_path=central,
                            project_uuid=project_uuid,
                            operation_id=operation_id,
                            metrics=metrics,
                        )
                    _assert_snapshot_unchanged(
                        mailbox.state_dir,
                        snapshot,
                        "local mailbox",
                    )
                    _verify_central_generation(
                        central,
                        project_uuid,
                        registry,
                        expected_manifest=manifest_path,
                        expected_source_digest=snapshot.digest,
                    )
                    _ensure_gitignore(mailbox.state_dir, metrics)
                    guard()
                    _maybe_fail("before_registry_flip")

                    def cutover_guard():
                        guard()
                        _assert_snapshot_unchanged(
                            mailbox.state_dir,
                            snapshot,
                            "local mailbox",
                        )

                    try:
                        _flip_layout(
                            registry,
                            project_uuid,
                            root,
                            "local",
                            "central",
                            guard=cutover_guard,
                            lock_timeout=registry_lock_timeout,
                            metrics=metrics,
                        )
                        committed = True
                    except Exception as cutover_error:
                        committed = _commit_observed_after_error(
                            registry,
                            project_uuid,
                            "central",
                            cutover_error,
                        )
                        raise
                    _assert_snapshot_unchanged(
                        mailbox.state_dir,
                        snapshot,
                        "local mailbox",
                    )
                    _maybe_fail("after_registry_flip")
                    warnings.extend(
                        _cleanup_local_after_forward(
                            mailbox.state_dir,
                            list(snapshot.entries),
                            operation_id,
                            metrics,
                        )
                    )
                    _maybe_fail("after_local_cleanup")
                    _install_bookmark(root, central, metrics)
                    _maybe_fail("after_bookmark")
            finally:
                _close_project_guard(root_fd, state_fd)
    except Exception as error:
        if committed:
            raise MailMigrationCommittedError(
                "central mailbox cutover committed; rerun migrate to finish "
                f"post-commit repair: {error}"
            ) from error
        raise
    exited_at = time.monotonic()
    assert entered_at is not None
    assert manifest_path is not None
    assert snapshot is not None
    return _result(
        operation="migrate",
        status=status,
        manifest=manifest_path,
        rollback_manifest=None,
        snapshot=snapshot,
        lock_wait=entered_at - lock_started,
        exclusive_hold=exited_at - entered_at,
        metrics=metrics,
        committed=True,
        warnings=warnings,
    )


def rollback_project(
    project: Path | str,
    manifest: Path | str,
    *,
    registry_path: Path | str | None = None,
    backup_root: Path | str | None = None,
    layout_lock_timeout: float = 10.0,
    registry_lock_timeout: float = 10.0,
) -> dict:
    """Rollback one exact central generation using current central mail."""

    registry = _registry_path(registry_path)
    root = Path(project).expanduser().resolve()
    initial = resolve_project_mailbox_checked(
        root,
        registry_path=registry,
        reconcile=True,
    )
    project_uuid = initial.project_uuid
    original_manifest, forward, forward_digest = load_manifest(
        manifest,
        expected_uuid=project_uuid,
        expected_registry=registry,
    )
    if forward["direction"] != "local-to-central":
        raise MailMigrationError(
            f"rollback requires a forward migration manifest: "
            f"{original_manifest}"
        )
    central = central_mail_root(registry, project_uuid)
    selected_backup_root = (
        DEFAULT_BACKUP_ROOT
        if backup_root is None
        else Path(backup_root).expanduser()
    )
    metrics = MigrationMetrics()
    operation_id, operation_name = _operation_name()
    lock_started = time.monotonic()
    entered_at = None
    committed = False
    rollback_manifest: Path | None = None
    snapshot: MailSnapshot | None = None
    warnings: list[str] = []
    status = "rolled back"
    try:
        with locked_project_mailbox_checked(
            root,
            registry_path=registry,
            exclusive=True,
            timeout=layout_lock_timeout,
        ) as mailbox:
            entered_at = time.monotonic()
            _registry_evidence(registry, project_uuid, root)
            root_fd, state_fd = _open_project_guard(root)
            try:
                def guard():
                    _verify_project_guard(root, root_fd, state_fd)
                    witness = _read_project_identity_at(root, state_fd)
                    if witness != project_uuid:
                        raise MailMigrationError(
                            f"project identity changed during rollback: "
                            f"{witness} != {project_uuid}"
                        )

                guard()
                _preflight_rollback_marker(
                    mailbox.state_dir,
                    project_uuid,
                )
                if mailbox.layout == "local":
                    _ensure_gitignore(mailbox.state_dir, metrics)
                    marker = _load_rollback_marker(
                        mailbox.state_dir,
                        project_uuid=project_uuid,
                        source_manifest=original_manifest,
                        source_manifest_sha256=forward_digest,
                    )
                    rollback_manifest, rollback_doc, _rollback_digest = (
                        load_manifest(
                            marker["rollback_manifest"],
                            expected_uuid=project_uuid,
                            expected_registry=registry,
                        )
                    )
                    if (
                        rollback_doc["direction"] != "central-to-local"
                        or rollback_doc.get("source_manifest")
                        != str(original_manifest)
                        or marker.get("rollback_manifest")
                        != str(rollback_manifest)
                        or marker.get("operation_id")
                        != rollback_doc["operation_id"]
                        or marker.get("snapshot_digest")
                        != rollback_doc["snapshot_digest"]
                    ):
                        raise MailMigrationError(
                            "local rollback marker references an invalid "
                            "rollback backup"
                        )
                    snapshot = scan_mail_tree(mailbox.state_dir)
                    committed = True
                    status = "already local"
                    _remove_bookmark(root, central, metrics)
                    if _path_info(central) is not None:
                        (
                            _candidate_marker,
                            candidate_manifest,
                            candidate_document,
                        ) = _verify_central_generation(
                            central,
                            project_uuid,
                            registry,
                        )
                        if candidate_manifest == original_manifest:
                            current_central = scan_mail_tree(central)
                            if (
                                current_central.digest
                                != marker["snapshot_digest"]
                            ):
                                warnings.append(
                                    "preserved changed non-authoritative "
                                    "central mail in rollback quarantine"
                                )
                            _quarantine_central(
                                central,
                                project_uuid,
                                marker["operation_id"],
                                registry=registry,
                                expected_manifest=original_manifest,
                                require_pristine=False,
                                suffix="rolledback",
                                metrics=metrics,
                            )
                        else:
                            verify_snapshot_copy(
                                central,
                                candidate_document["entries"],
                            )
                            warnings.append(
                                "left a different valid non-authoritative "
                                f"central candidate untouched: {central}"
                            )
                else:
                    rollback_forbidden_roots = (
                        mailbox.state_dir,
                        central.parent,
                        registry.parent / "layout-locks",
                    )
                    rollback_bundle_forbidden_roots = (
                        original_manifest.parent,
                    )
                    _validate_backup_location(
                        selected_backup_root,
                        project_uuid,
                        rollback_forbidden_roots,
                        operation_name=operation_name,
                        forbidden_bundle_roots=(
                            rollback_bundle_forbidden_roots
                        ),
                    )
                    _ensure_gitignore(mailbox.state_dir, metrics)
                    (
                        central_marker,
                        generation_manifest,
                        _generation,
                    ) = _verify_central_generation(
                        central,
                        project_uuid,
                        registry,
                        expected_manifest=original_manifest,
                    )
                    if generation_manifest != original_manifest:
                        raise MailMigrationError(
                            "rollback manifest is not the active central "
                            "generation"
                        )
                    _preflight_bookmark(
                        root,
                        central,
                        allow_exact=True,
                    )
                    snapshot = scan_mail_tree(central)
                    registry_payload, registry_row = _registry_evidence(
                        registry,
                        project_uuid,
                        root,
                    )
                    (
                        rollback_manifest,
                        rollback_doc,
                        _rollback_digest,
                    ) = _create_backup(
                        source_root=central,
                        source_snapshot=snapshot,
                        registry=registry,
                        registry_payload=registry_payload,
                        registry_row=registry_row,
                        project_root=root,
                        project_uuid=project_uuid,
                        direction="central-to-local",
                        backup_root=selected_backup_root,
                        operation_id=operation_id,
                        operation_name=operation_name,
                        central_root=central,
                        source_manifest=str(original_manifest),
                        metrics=metrics,
                        forbidden_roots=rollback_forbidden_roots,
                        forbidden_bundle_roots=(
                            rollback_bundle_forbidden_roots
                        ),
                    )
                    _maybe_fail("rollback_after_backup")
                    quarantine = _prepare_local_candidate(
                        state_dir=mailbox.state_dir,
                        verified_backup=rollback_manifest.parent,
                        source_snapshot=snapshot,
                        original_manifest=original_manifest,
                        original_manifest_sha256=forward_digest,
                        rollback_manifest=rollback_manifest,
                        project_uuid=project_uuid,
                        operation_id=operation_id,
                        metrics=metrics,
                    )
                    _maybe_fail("rollback_after_local_install")
                    _assert_snapshot_unchanged(
                        central,
                        snapshot,
                        "central mailbox",
                    )
                    guard()
                    _maybe_fail("rollback_before_registry_flip")

                    def cutover_guard():
                        guard()
                        _assert_snapshot_unchanged(
                            central,
                            snapshot,
                            "central mailbox",
                        )

                    try:
                        _flip_layout(
                            registry,
                            project_uuid,
                            root,
                            "central",
                            "local",
                            guard=cutover_guard,
                            lock_timeout=registry_lock_timeout,
                            metrics=metrics,
                        )
                        committed = True
                    except Exception as cutover_error:
                        committed = _commit_observed_after_error(
                            registry,
                            project_uuid,
                            "local",
                            cutover_error,
                        )
                        raise
                    _assert_snapshot_unchanged(
                        central,
                        snapshot,
                        "central mailbox",
                    )
                    _maybe_fail("rollback_after_registry_flip")
                    _remove_bookmark(root, central, metrics)
                    _quarantine_central(
                        central,
                        project_uuid,
                        central_marker["operation_id"],
                        registry=registry,
                        expected_manifest=original_manifest,
                        require_pristine=False,
                        suffix="rolledback",
                        metrics=metrics,
                    )
                    if quarantine is not None:
                        warnings.append(
                            "preserved prior non-authoritative local mail at "
                            f"{quarantine}"
                        )
                    _maybe_fail("rollback_after_central_retire")
            finally:
                _close_project_guard(root_fd, state_fd)
    except Exception as error:
        if committed:
            raise MailMigrationCommittedError(
                "local rollback cutover committed; rerun rollback with the "
                f"same manifest to finish post-commit repair: {error}"
            ) from error
        raise
    exited_at = time.monotonic()
    assert entered_at is not None
    assert rollback_manifest is not None
    assert snapshot is not None
    return _result(
        operation="rollback",
        status=status,
        manifest=original_manifest,
        rollback_manifest=rollback_manifest,
        snapshot=snapshot,
        lock_wait=entered_at - lock_started,
        exclusive_hold=exited_at - entered_at,
        metrics=metrics,
        committed=True,
        warnings=warnings,
    )
