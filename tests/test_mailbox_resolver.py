from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

import _kit as kit
from _kit import consumers


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlib  # noqa: E402


REGISTERED_AT = "2026-07-29T12:00:00Z"
TOMBSTONED_AT = "2026-07-29T13:00:00Z"
FAIL_CLOSED = (_rtlib.ProjectRegistryError, SystemExit)


def write_project(path: Path) -> Path:
    root = kit.write_project(path, [kit.CODEX], project=kit.PROJECT_DOT)
    (root / ".roundtable" / ".gitignore").write_text(
        "runtime.json\nmessages/\nlocks/\ninbox/\n"
    )
    return root


def read_document(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def write_document(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON is valid YAML and keeps malformed-fixture construction obvious.
    path.write_text(json.dumps(document, indent=2) + "\n")


def write_central_marker(root: Path, project_uuid: str) -> None:
    (root / _rtlib.CENTRAL_MAIL_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema": _rtlib.CENTRAL_MAIL_MARKER_SCHEMA,
                "project_uuid": project_uuid,
                "operation_id": "00000000-0000-4000-8000-000000000003",
                "manifest": str(root.parent / "resolver-test-manifest.json"),
                "manifest_sha256": "1" * 64,
                "snapshot_digest": "2" * 64,
            }
        )
        + "\n"
    )


def read_identity(root: Path) -> dict:
    return json.loads(_rtlib.project_identity_path(root).read_text())


def assert_canonical_uuid(value: str) -> None:
    assert str(uuid.UUID(value)) == value


def raw_entry_for(document: dict, root: Path) -> dict:
    canonical = str(root.resolve())
    matches = [
        entry
        for entry in document["projects"]
        if isinstance(entry, dict) and entry.get("path") == canonical
    ]
    assert len(matches) == 1
    return matches[0]


def loaded_entry_for(entries: list[dict], root: Path) -> dict:
    canonical = root.resolve()
    matches = [
        entry
        for entry in entries
        if Path(entry["root"]).resolve() == canonical
    ]
    assert len(matches) == 1
    return matches[0]


def v2_entry(
    root: Path,
    project_uuid: str,
    *,
    layout: str = "local",
    status: str = "active",
) -> dict:
    entry = {
        "uuid": project_uuid,
        "path": str(root.resolve()),
        "name": root.name,
        "group": root.parent.name,
        "layout": layout,
        "status": status,
        "registered_at": REGISTERED_AT,
    }
    if status == "tombstoned":
        entry["tombstoned_at"] = TOMBSTONED_AT
    return entry


def write_identity(root: Path, project_uuid: str) -> None:
    marker = _rtlib.project_identity_path(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {"schema": _rtlib.PROJECT_ID_SCHEMA, "uuid": project_uuid}
        )
        + "\n"
    )


def test_new_registration_writes_v2_identity_and_local_mailbox(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "work" / "alpha")

    assert _rtlib.register_project(
        project, path=registry, registered_at=REGISTERED_AT
    )

    document = read_document(registry)
    assert document["schema"] == _rtlib.PROJECTS_SCHEMA
    assert _rtlib.PROJECTS_SCHEMA == "roundtable.projects.v2"
    assert _rtlib.LEGACY_PROJECTS_SCHEMA == "roundtable.projects.v1"
    entry = raw_entry_for(document, project)
    assert set(entry) == {
        "uuid",
        "path",
        "name",
        "group",
        "layout",
        "status",
        "registered_at",
    }
    assert_canonical_uuid(entry["uuid"])
    assert entry["name"] == project.name
    assert entry["group"] == _rtlib.derive_project_group(
        project, entry["uuid"]
    )
    assert entry["layout"] == "local"
    assert entry["status"] == "active"
    assert entry["registered_at"] == REGISTERED_AT

    identity = read_identity(project)
    assert identity == {
        "schema": _rtlib.PROJECT_ID_SCHEMA,
        "uuid": entry["uuid"],
    }
    assert _rtlib.PROJECT_ID_SCHEMA == "roundtable.project.v1"
    ignored = (project / ".roundtable" / ".gitignore").read_text().splitlines()
    assert ignored.count("project.json") == 1

    mailbox = _rtlib.resolve_project_mailbox(
        project, registry_path=registry
    )
    state = project / ".roundtable"
    assert mailbox.project_uuid == entry["uuid"]
    assert mailbox.project_root == project
    assert mailbox.layout == "local"
    assert mailbox.state_dir == state
    assert mailbox.mail_root == state
    assert mailbox.inbox_dir == state / "inbox"
    assert mailbox.messages_dir == state / "messages"
    assert mailbox.locks_dir == state / "locks"
    with pytest.raises(FrozenInstanceError):
        mailbox.layout = "central"


def test_registration_is_uuid_idempotent(tmp_path: Path) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")

    assert _rtlib.register_project(
        project, path=registry, registered_at=REGISTERED_AT
    )
    first_document = read_document(registry)
    first_entry = raw_entry_for(first_document, project)
    first_identity = read_identity(project)

    assert not _rtlib.register_project(
        project,
        path=registry,
        registered_at="2099-01-01T00:00:00Z",
    )

    second_document = read_document(registry)
    assert len(second_document["projects"]) == 1
    assert raw_entry_for(second_document, project) == first_entry
    assert read_identity(project) == first_identity


def test_root_swap_after_canonicalization_cannot_redirect_identity_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    target = write_project(tmp_path / "target")
    archived = tmp_path / "archived"
    original_open_guard = _rtlib._open_project_guard

    def swap_before_open(root):
        project.rename(archived)
        project.symlink_to(target, target_is_directory=True)
        return original_open_guard(root)

    monkeypatch.setattr(_rtlib, "_open_project_guard", swap_before_open)

    with pytest.raises(SystemExit, match="cannot open project root"):
        _rtlib.register_project(project, path=registry)

    assert not registry.exists()
    assert not _rtlib.project_identity_path(archived).exists()
    assert not _rtlib.project_identity_path(target).exists()


def test_root_guard_remains_live_through_registry_precommit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "project")
    target = write_project(tmp_path / "target")
    archived = tmp_path / "archived"
    original_write_projects = _rtlib._write_projects_doc

    def swap_before_registry_write(*args, **kwargs):
        project.rename(archived)
        project.symlink_to(target, target_is_directory=True)
        return original_write_projects(*args, **kwargs)

    monkeypatch.setattr(
        _rtlib, "_write_projects_doc", swap_before_registry_write
    )

    with pytest.raises(SystemExit, match="project root changed during update"):
        _rtlib.register_project(project, path=registry)

    assert not registry.exists()
    assert _rtlib.project_identity_path(archived).is_file()
    assert not _rtlib.project_identity_path(target).exists()


def test_identity_retry_fsyncs_a_marker_published_before_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(tmp_path / "project")
    ignore = project / ".roundtable" / ".gitignore"
    ignore.write_text(ignore.read_text() + "project.json\n")
    project_uuid = str(uuid.uuid4())
    state_info = (project / ".roundtable").stat()
    original_fsync = _rtlib.os.fsync
    interrupted = False

    def interrupt_state_directory(descriptor):
        nonlocal interrupted
        info = os.fstat(descriptor)
        if (
            not interrupted
            and stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino)
            == (state_info.st_dev, state_info.st_ino)
        ):
            interrupted = True
            raise OSError("injected state directory interruption")
        return original_fsync(descriptor)

    monkeypatch.setattr(_rtlib.os, "fsync", interrupt_state_directory)
    with pytest.raises(
        _rtlib.ProjectRegistryError,
        match="state directory interruption",
    ):
        _rtlib._write_project_identity(project, project_uuid)

    assert read_identity(project)["uuid"] == project_uuid

    synced = []

    def record_state_directory(descriptor):
        info = os.fstat(descriptor)
        if (
            stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino)
            == (state_info.st_dev, state_info.st_ino)
        ):
            synced.append(True)
        return original_fsync(descriptor)

    monkeypatch.setattr(_rtlib.os, "fsync", record_state_directory)
    assert not _rtlib._write_project_identity(project, project_uuid)
    assert synced == [True]


def test_registry_retry_fsyncs_a_rename_published_before_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    registry.parent.mkdir(mode=0o700)
    registry_parent = registry.parent.stat()
    project = write_project(tmp_path / "project")
    original_fsync = _rtlib.os.fsync
    interrupted = False

    def interrupt_registry_directory(descriptor):
        nonlocal interrupted
        info = os.fstat(descriptor)
        if (
            not interrupted
            and stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino)
            == (registry_parent.st_dev, registry_parent.st_ino)
        ):
            interrupted = True
            raise OSError("injected registry directory interruption")
        return original_fsync(descriptor)

    monkeypatch.setattr(_rtlib.os, "fsync", interrupt_registry_directory)
    with pytest.raises(SystemExit, match="registry directory interruption"):
        _rtlib.register_project(project, path=registry)

    assert read_document(registry)["schema"] == _rtlib.PROJECTS_SCHEMA
    assert _rtlib.project_identity_path(project).is_file()

    synced = []

    def record_registry_directory(descriptor):
        info = os.fstat(descriptor)
        if (
            stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino)
            == (registry_parent.st_dev, registry_parent.st_ino)
        ):
            synced.append(True)
        return original_fsync(descriptor)

    monkeypatch.setattr(_rtlib.os, "fsync", record_registry_directory)
    assert not _rtlib.register_project(project, path=registry)
    assert synced == [True]


def test_idempotent_registry_sync_failure_is_typed_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(project, path=registry)
    registry_parent = registry.parent.stat()
    original_fsync = _rtlib.os.fsync

    def fail_registry_directory(descriptor):
        info = os.fstat(descriptor)
        if (
            stat.S_ISDIR(info.st_mode)
            and (info.st_dev, info.st_ino)
            == (registry_parent.st_dev, registry_parent.st_ino)
        ):
            raise OSError("injected idempotent registry sync failure")
        return original_fsync(descriptor)

    monkeypatch.setattr(_rtlib.os, "fsync", fail_registry_directory)

    with pytest.raises(
        SystemExit,
        match="cannot sync project registry parent",
    ):
        _rtlib.register_project(project, path=registry)


def test_explicit_v1_upgrade_rejects_unavailable_root_before_any_write(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    existing = write_project(tmp_path / "existing")
    missing = (tmp_path / "missing").resolve()
    legacy = {
        "schema": _rtlib.LEGACY_PROJECTS_SCHEMA,
        "projects": [
            {"root": str(existing), "registered_at": REGISTERED_AT},
            {"root": str(missing), "registered_at": REGISTERED_AT},
        ],
    }
    write_document(registry, legacy)
    legacy_bytes = registry.read_bytes()
    backup = _rtlib.legacy_registry_backup_path(registry)
    legacy_entries, legacy_warnings = _rtlib.load_project_registry(registry)
    assert _rtlib.active_project_entries(legacy_entries) == []
    assert any("run rt-projects upgrade" in item for item in legacy_warnings)

    with pytest.raises(SystemExit, match="unavailable"):
        _rtlib.upgrade_project_registry(registry)

    assert registry.read_bytes() == legacy_bytes
    assert not backup.exists()
    assert not _rtlib.project_identity_path(existing).exists()
    assert not _rtlib.project_identity_path(missing).exists()


def test_missing_marker_on_legacy_registry_names_upgrade_not_add(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    write_document(
        registry,
        {
            "schema": _rtlib.LEGACY_PROJECTS_SCHEMA,
            "projects": [
                {
                    "root": str(project),
                    "registered_at": REGISTERED_AT,
                }
            ],
        },
    )

    with pytest.raises(
        SystemExit,
        match=r"rt-projects --registry .* upgrade",
    ):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)


def _write_legacy_registry(registry: Path, roots: list[Path]) -> bytes:
    write_document(
        registry,
        {
            "schema": _rtlib.LEGACY_PROJECTS_SCHEMA,
            "projects": [
                {"root": str(root.resolve()), "registered_at": REGISTERED_AT}
                for root in roots
            ],
        },
    )
    return registry.read_bytes()


def _assert_completed_upgrade(
    registry: Path,
    roots: list[Path],
    legacy_bytes: bytes,
) -> dict[Path, str]:
    backup = _rtlib.legacy_registry_backup_path(registry)
    assert backup.read_bytes() == legacy_bytes
    document = read_document(registry)
    assert document["schema"] == _rtlib.PROJECTS_SCHEMA
    assert len(document["projects"]) == len(roots)
    observed = {
        root: raw_entry_for(document, root)["uuid"]
        for root in roots
    }
    assert len(set(observed.values())) == len(roots)
    for root, project_uuid in observed.items():
        assert read_identity(root)["uuid"] == project_uuid
    return observed


def test_v1_upgrade_retries_after_failure_immediately_after_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    roots = [write_project(tmp_path / "first"), write_project(tmp_path / "second")]
    legacy_bytes = _write_legacy_registry(registry, roots)
    original_write_identity = _rtlib._write_project_identity

    def fail_before_first_marker(*_args, **_kwargs):
        raise _rtlib.ProjectRegistryError("injected marker failure")

    monkeypatch.setattr(
        _rtlib, "_write_project_identity", fail_before_first_marker
    )
    with pytest.raises(SystemExit, match="injected marker failure"):
        _rtlib.upgrade_project_registry(registry)

    assert registry.read_bytes() == legacy_bytes
    assert _rtlib.legacy_registry_backup_path(registry).read_bytes() == legacy_bytes
    assert all(
        not _rtlib.project_identity_path(root).exists() for root in roots
    )

    monkeypatch.setattr(
        _rtlib, "_write_project_identity", original_write_identity
    )
    _rtlib.upgrade_project_registry(registry)
    _assert_completed_upgrade(registry, roots, legacy_bytes)


def test_v1_upgrade_retries_when_atomic_backup_publish_is_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    root = write_project(tmp_path / "project")
    legacy_bytes = _write_legacy_registry(registry, [root])
    backup = _rtlib.legacy_registry_backup_path(registry)
    original_link = _rtlib.os.link
    injected = False

    def interrupt_backup_publish(source, target, *args, **kwargs):
        nonlocal injected
        if target == backup.name and not injected:
            injected = True
            raise OSError("injected atomic publish interruption")
        return original_link(source, target, *args, **kwargs)

    monkeypatch.setattr(_rtlib.os, "link", interrupt_backup_publish)
    with pytest.raises(SystemExit, match="atomic publish interruption"):
        _rtlib.upgrade_project_registry(registry)

    assert registry.read_bytes() == legacy_bytes
    assert not backup.exists()
    assert not _rtlib.project_identity_path(root).exists()

    monkeypatch.setattr(_rtlib.os, "link", original_link)
    _rtlib.upgrade_project_registry(registry)
    _assert_completed_upgrade(registry, [root], legacy_bytes)


def test_v1_upgrade_reuses_first_marker_after_multi_root_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    roots = [write_project(tmp_path / "first"), write_project(tmp_path / "second")]
    legacy_bytes = _write_legacy_registry(registry, roots)
    original_write_identity = _rtlib._write_project_identity
    calls = 0

    def fail_before_second_marker(root, project_uuid, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise _rtlib.ProjectRegistryError("injected second marker failure")
        return original_write_identity(root, project_uuid, **kwargs)

    monkeypatch.setattr(
        _rtlib, "_write_project_identity", fail_before_second_marker
    )
    with pytest.raises(SystemExit, match="injected second marker failure"):
        _rtlib.upgrade_project_registry(registry)

    persisted_uuid = read_identity(roots[0])["uuid"]
    assert not _rtlib.project_identity_path(roots[1]).exists()
    assert registry.read_bytes() == legacy_bytes

    monkeypatch.setattr(
        _rtlib, "_write_project_identity", original_write_identity
    )
    _rtlib.upgrade_project_registry(registry)
    observed = _assert_completed_upgrade(registry, roots, legacy_bytes)
    assert observed[roots[0]] == persisted_uuid


def test_v1_upgrade_reuses_all_markers_after_registry_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    roots = [write_project(tmp_path / "first"), write_project(tmp_path / "second")]
    legacy_bytes = _write_legacy_registry(registry, roots)
    original_write_projects = _rtlib._write_projects_doc

    def fail_registry_write(*_args, **_kwargs):
        raise _rtlib.ProjectRegistryError("injected registry write failure")

    monkeypatch.setattr(_rtlib, "_write_projects_doc", fail_registry_write)
    with pytest.raises(SystemExit, match="injected registry write failure"):
        _rtlib.upgrade_project_registry(registry)

    persisted = {root: read_identity(root)["uuid"] for root in roots}
    assert registry.read_bytes() == legacy_bytes
    assert _rtlib.legacy_registry_backup_path(registry).read_bytes() == legacy_bytes

    monkeypatch.setattr(
        _rtlib, "_write_projects_doc", original_write_projects
    )
    _rtlib.upgrade_project_registry(registry)
    assert _assert_completed_upgrade(registry, roots, legacy_bytes) == persisted


@pytest.mark.parametrize(
    "projects",
    [
        [{"root": "duplicate", "registered_at": REGISTERED_AT}] * 2,
        [{"root": "valid", "registered_at": REGISTERED_AT}, "not-a-mapping"],
    ],
)
def test_malformed_v1_upgrade_has_no_partial_backup_or_identity(
    tmp_path: Path,
    projects: list[object],
) -> None:
    registry = tmp_path / "projects.yaml"
    root = write_project(tmp_path / "valid")
    rendered = []
    for raw in projects:
        if isinstance(raw, dict):
            rendered.append(
                {
                    **raw,
                    "root": str(
                        root
                        if raw["root"] in {"valid", "duplicate"}
                        else raw["root"]
                    ),
                }
            )
        else:
            rendered.append(raw)
    write_document(
        registry,
        {
            "schema": _rtlib.LEGACY_PROJECTS_SCHEMA,
            "projects": rendered,
        },
    )
    before = registry.read_bytes()

    with pytest.raises(SystemExit, match="refusing partial"):
        _rtlib.upgrade_project_registry(registry)

    assert registry.read_bytes() == before
    assert not _rtlib.legacy_registry_backup_path(registry).exists()
    assert not _rtlib.project_identity_path(root).exists()


def test_v1_upgrade_backup_collision_precedes_identity_writes(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    root = write_project(tmp_path / "project")
    legacy_bytes = _write_legacy_registry(registry, [root])
    backup = _rtlib.legacy_registry_backup_path(registry)
    backup.write_bytes(b"different snapshot\n")

    with pytest.raises(SystemExit, match="backup collision"):
        _rtlib.upgrade_project_registry(registry)

    assert registry.read_bytes() == legacy_bytes
    assert backup.read_bytes() == b"different snapshot\n"
    assert not _rtlib.project_identity_path(root).exists()


# The parameter values become part of the collected node id, so they must be
# literals: a collection-time ``uuid.uuid4()`` makes every xdist worker report a
# different node id and the run aborts with "Different tests were collected".
# This case only needs a *valid* UUID, not a unique one.
_VALID_UUID_PARAM = "6b3f2c1e-0d4a-4f8b-9c2d-7e5a1b6f3049"


@pytest.mark.parametrize(
    ("uuid_value", "layout", "diagnostic"),
    [
        ("not-a-uuid", "local", "uuid"),
        (_VALID_UUID_PARAM, "distributed", "layout"),
    ],
)
def test_malformed_uuid_or_layout_fails_closed(
    tmp_path: Path,
    uuid_value: str,
    layout: str,
    diagnostic: str,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    marker_uuid = (
        uuid_value if diagnostic == "layout" else str(uuid.uuid4())
    )
    write_identity(project, marker_uuid)
    write_document(
        registry,
        {
            "schema": _rtlib.PROJECTS_SCHEMA,
            "projects": [
                v2_entry(project, uuid_value, layout=layout),
            ],
        },
    )

    entries, warnings = _rtlib.load_project_registry(registry)
    assert entries == []
    assert any(diagnostic in warning.lower() for warning in warnings)
    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)


def test_duplicate_uuid_fails_closed_for_every_claimant(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    first = write_project(tmp_path / "first")
    second = write_project(tmp_path / "second")
    shared_uuid = str(uuid.uuid4())
    write_identity(first, shared_uuid)
    write_identity(second, shared_uuid)
    write_document(
        registry,
        {
            "schema": _rtlib.PROJECTS_SCHEMA,
            "projects": [
                v2_entry(first, shared_uuid),
                v2_entry(second, shared_uuid),
            ],
        },
    )

    entries, warnings = _rtlib.load_project_registry(registry)
    # The diagnostic loader may retain the first structurally valid row for
    # display, but it must never expose both claims and strict consumers below
    # must reject the registry as a whole.
    assert len(entries) < 2
    assert any(
        "duplicate" in warning.lower() and "uuid" in warning.lower()
        for warning in warnings
    )
    for project in (first, second):
        with pytest.raises(FAIL_CLOSED):
            _rtlib.resolve_project_mailbox(project, registry_path=registry)


def test_noncanonical_semantic_duplicate_uuid_fails_target_resolution(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    target = write_project(tmp_path / "target")
    sibling = write_project(tmp_path / "sibling")
    project_uuid = str(uuid.uuid4())
    write_identity(target, project_uuid)
    write_document(
        registry,
        {
            "schema": _rtlib.PROJECTS_SCHEMA,
            "projects": [
                v2_entry(target, project_uuid),
                v2_entry(sibling, f"{project_uuid} "),
            ],
        },
    )

    with pytest.raises(SystemExit, match="2 registry claims"):
        _rtlib.resolve_project_mailbox(target, registry_path=registry)


def test_invalid_status_row_at_target_path_is_an_ambiguous_claim(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    target = write_project(tmp_path / "target")
    project_uuid = str(uuid.uuid4())
    write_identity(target, project_uuid)
    ambiguous = v2_entry(target, str(uuid.uuid4()))
    ambiguous["status"] = "actve"
    write_document(
        registry,
        {
            "schema": _rtlib.PROJECTS_SCHEMA,
            "projects": [
                v2_entry(target, project_uuid),
                ambiguous,
            ],
        },
    )
    before = registry.read_bytes()

    with pytest.raises(SystemExit, match="aliases live project inode"):
        _rtlib.resolve_project_mailbox(target, registry_path=registry)

    assert registry.read_bytes() == before


def test_structural_warning_containing_missing_is_never_classified_as_orphan(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    valid = write_project(tmp_path / "valid")
    malformed = write_project(tmp_path / "malformed")
    _rtlib.register_project(valid, path=registry)
    document = read_document(registry)
    document["projects"].append(
        v2_entry(malformed, "not missing a uuid")
    )
    write_document(registry, document)

    entries, warnings = _rtlib.load_project_registry(registry)

    assert len(entries) == 1
    assert any("not missing a uuid" in warning for warning in warnings)
    mailbox = _rtlib.resolve_project_mailbox(valid, registry_path=registry)
    assert mailbox.project_uuid == read_identity(valid)["uuid"]
    before = registry.read_bytes()
    with pytest.raises(FAIL_CLOSED):
        _rtlib.register_project(
            write_project(tmp_path / "new-project"),
            path=registry,
        )
    assert registry.read_bytes() == before


def test_active_entry_with_tombstoned_timestamp_is_structurally_invalid(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    project_uuid = str(uuid.uuid4())
    write_identity(project, project_uuid)
    entry = v2_entry(project, project_uuid)
    entry["tombstoned_at"] = TOMBSTONED_AT
    write_document(
        registry,
        {
            "schema": _rtlib.PROJECTS_SCHEMA,
            "projects": [entry],
        },
    )
    before = registry.read_bytes()

    entries, warnings = _rtlib.load_project_registry(registry)
    assert entries == []
    assert any(
        "active entry carries tombstoned_at" in warning
        for warning in warnings
    )
    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)
    with pytest.raises(FAIL_CLOSED):
        _rtlib.register_project(project, path=registry)
    with pytest.raises(FAIL_CLOSED):
        _rtlib.unregister_project(project, path=registry)
    assert registry.read_bytes() == before


def test_declared_group_cannot_override_and_is_reconciled_to_derived_group(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(project, path=registry)
    document = read_document(registry)
    entry = raw_entry_for(document, project)
    entry["group"] = "attacker-selected-group"
    write_document(registry, document)

    entries, warnings = _rtlib.load_project_registry(registry)

    assert warnings == []
    assert len(entries) == 1
    derived = _rtlib.derive_project_group(project, entry["uuid"])
    assert entries[0]["group"] == "attacker-selected-group"
    assert entries[0]["declared_group"] == "attacker-selected-group"
    assert not entries[0]["group_authoritative"]

    mailbox = _rtlib.resolve_project_mailbox(
        project, registry_path=registry
    )

    assert mailbox.project_uuid == entry["uuid"]
    repaired = raw_entry_for(read_document(registry), project)
    assert repaired["group"] == derived


def test_git_group_derivation_failure_fails_closed_at_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    _rtlib.register_project(project, path=registry)
    before = registry.read_bytes()
    original_run = _rtlib.subprocess.run

    def fail_group_probe(command, *args, **kwargs):
        if "--git-common-dir" in command:
            raise subprocess.TimeoutExpired(command, 5.0)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(_rtlib.subprocess, "run", fail_group_probe)

    with pytest.raises(SystemExit, match="cannot derive Git sibling group"):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)

    assert registry.read_bytes() == before


def test_non_git_group_fallback_remains_authoritative_without_git_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(tmp_path / "project")
    project_uuid = str(uuid.uuid4())

    def missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(_rtlib.subprocess, "run", missing_git)

    assert _rtlib._derive_project_group(project, project_uuid) == (
        project_uuid,
        True,
    )


def test_reused_path_without_identity_cannot_inherit_active_uuid(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "same")
    _rtlib.register_project(original, path=registry)
    before = read_document(registry)
    original_uuid = read_identity(original)["uuid"]
    archived = tmp_path / "original-archive"
    original.rename(archived)
    replacement = write_project(tmp_path / "same")

    with pytest.raises(FAIL_CLOSED):
        _rtlib.register_project(replacement, path=registry)

    assert not _rtlib.project_identity_path(replacement).exists()
    assert read_document(registry) == before
    assert read_identity(archived)["uuid"] == original_uuid


def test_tombstone_cannot_be_resolved_or_reclaimed_from_marker(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(
        project, path=registry, registered_at=REGISTERED_AT
    )
    project_uuid = read_identity(project)["uuid"]

    assert _rtlib.unregister_project(
        project, path=registry, tombstoned_at=TOMBSTONED_AT
    )

    document = read_document(registry)
    tombstone = raw_entry_for(document, project)
    assert tombstone["uuid"] == project_uuid
    assert tombstone["status"] == "tombstoned"
    assert tombstone["tombstoned_at"] == TOMBSTONED_AT
    assert _rtlib.project_identity_path(project).is_file()
    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)


def test_register_after_tombstone_rotates_uuid(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(project, path=registry)
    first_uuid = read_identity(project)["uuid"]
    _rtlib.unregister_project(project, path=registry)

    assert _rtlib.register_project(project, path=registry)

    second_uuid = read_identity(project)["uuid"]
    assert second_uuid != first_uuid
    document = read_document(registry)
    assert {
        (entry["uuid"], entry["status"])
        for entry in document["projects"]
    } == {
        (first_uuid, "tombstoned"),
        (second_uuid, "active"),
    }


def test_missing_registered_root_can_be_explicitly_tombstoned(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(project, path=registry)
    project_uuid = read_identity(project)["uuid"]
    archived = tmp_path / "archived"
    project.rename(archived)

    assert _rtlib.unregister_project(project, path=registry)
    assert not _rtlib.unregister_project(project, path=registry)

    entry = raw_entry_for(read_document(registry), project)
    assert entry["uuid"] == project_uuid
    assert entry["status"] == "tombstoned"


def test_replacement_without_marker_must_vacate_path_before_tombstone(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "same")
    _rtlib.register_project(original, path=registry)
    original_uuid = read_identity(original)["uuid"]
    archived = tmp_path / "archived"
    original.rename(archived)
    replacement = write_project(tmp_path / "same")
    before = registry.read_bytes()

    with pytest.raises(SystemExit, match=original_uuid):
        _rtlib.unregister_project(replacement, path=registry)
    assert registry.read_bytes() == before
    assert not _rtlib.project_identity_path(replacement).exists()
    staged = tmp_path / "replacement-staged"
    replacement.rename(staged)
    assert _rtlib.unregister_project(replacement, path=registry)
    stale = [
        entry
        for entry in read_document(registry)["projects"]
        if entry["uuid"] == original_uuid
    ]
    assert len(stale) == 1
    assert stale[0]["status"] == "tombstoned"

    staged.rename(replacement)
    assert _rtlib.register_project(replacement, path=registry)
    assert read_identity(replacement)["uuid"] != original_uuid


def test_linked_worktree_registration_mints_distinct_uuid_in_same_group(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    primary = write_project(tmp_path / "primary")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    _rtlib.register_project(primary, path=registry)
    subprocess.run(
        ["git", "add", ".roundtable/agents.yaml", ".roundtable/.gitignore"],
        cwd=primary,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Roundtable Test",
            "-c",
            "user.email=roundtable@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        cwd=primary,
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)],
        cwd=primary,
        check=True,
    )

    assert not _rtlib.project_identity_path(linked).exists()
    assert _rtlib.register_project(linked, path=registry)

    first = raw_entry_for(read_document(registry), primary)
    second = raw_entry_for(read_document(registry), linked)
    assert first["uuid"] != second["uuid"]
    assert first["group"] == second["group"]


def test_broken_linked_worktree_sibling_cannot_block_healthy_mailbox(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    primary = write_project(tmp_path / "lab" / "primary")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=primary, check=True)
    _rtlib.register_project(primary, path=registry)
    subprocess.run(
        ["git", "add", ".roundtable/agents.yaml", ".roundtable/.gitignore"],
        cwd=primary,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Roundtable Test",
            "-c",
            "user.email=roundtable@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
        cwd=primary,
        check=True,
    )
    linked = tmp_path / "lab" / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)],
        cwd=primary,
        check=True,
    )
    _rtlib.register_project(linked, path=registry)
    healthy = write_project(tmp_path / "healthy")
    _rtlib.register_project(healthy, path=registry)
    healthy_uuid = read_identity(healthy)["uuid"]

    primary.rename(tmp_path / "lab" / "primary-renamed")

    mailbox = _rtlib.resolve_project_mailbox(
        healthy,
        registry_path=registry,
    )
    assert mailbox.project_uuid == healthy_uuid
    entries, warnings = _rtlib.load_project_registry(registry)
    assert any(entry["uuid"] == healthy_uuid for entry in entries)
    assert any("orphan:" in warning for warning in warnings)


def test_symlink_drift_sibling_cannot_block_healthy_mailbox(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    healthy = write_project(tmp_path / "healthy")
    sibling = write_project(tmp_path / "sibling")
    _rtlib.register_project(healthy, path=registry)
    _rtlib.register_project(sibling, path=registry)
    healthy_uuid = read_identity(healthy)["uuid"]
    sibling_uuid = read_identity(sibling)["uuid"]
    relocated = tmp_path / "sibling-relocated"
    sibling.rename(relocated)
    sibling.symlink_to(relocated, target_is_directory=True)

    mailbox = _rtlib.resolve_project_mailbox(
        healthy,
        registry_path=registry,
    )

    assert mailbox.project_uuid == healthy_uuid
    entries, warnings = _rtlib.load_project_registry(registry)
    assert {entry["uuid"] for entry in entries} == {
        healthy_uuid,
        sibling_uuid,
    }
    assert any(
        "row-runtime:" in warning and sibling_uuid in warning
        for warning in warnings
    )
    before = registry.read_bytes()
    with pytest.raises(FAIL_CLOSED):
        _rtlib.unregister_project(healthy, path=registry)
    with pytest.raises(FAIL_CLOSED):
        _rtlib.register_project(
            write_project(tmp_path / "new-project"),
            path=registry,
        )
    assert registry.read_bytes() == before

    sibling.unlink()
    recovered = _rtlib.resolve_project_mailbox(
        relocated,
        registry_path=registry,
    )
    assert recovered.project_uuid == sibling_uuid
    assert raw_entry_for(
        read_document(registry),
        relocated,
    )["uuid"] == sibling_uuid


def test_exact_resolve_derives_group_only_for_target_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "projects.yaml"
    projects = [
        write_project(tmp_path / name)
        for name in ("target", "sibling-a", "sibling-b")
    ]
    for project in projects:
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=project,
            check=True,
        )
        _rtlib.register_project(project, path=registry)
    calls: list[Path] = []
    original = _rtlib._derive_project_group

    def observe(root, project_uuid):
        calls.append(Path(root))
        return original(root, project_uuid)

    monkeypatch.setattr(_rtlib, "_derive_project_group", observe)

    mailbox = _rtlib.resolve_project_mailbox(
        projects[0],
        registry_path=registry,
    )

    assert mailbox.project_root == projects[0]
    assert calls == [projects[0]]


def test_git_negation_is_repaired_and_later_unignore_fails_closed(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    ignore = project / ".roundtable" / ".gitignore"
    ignore.write_text("project.json\n!*.json\n")

    _rtlib.register_project(project, path=registry)

    assert ignore.read_text().splitlines()[-1] == "project.json"
    ignored = subprocess.run(
        [
            "git",
            "check-ignore",
            "-q",
            "--no-index",
            "--",
            ".roundtable/project.json",
        ],
        cwd=project,
        check=False,
    )
    assert ignored.returncode == 0

    ignore.write_text(ignore.read_text() + "!project.json\n")
    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)


def test_tracked_identity_marker_cannot_register(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    write_identity(project, str(uuid.uuid4()))
    subprocess.run(
        ["git", "add", "-f", ".roundtable/project.json"],
        cwd=project,
        check=True,
    )

    with pytest.raises(FAIL_CLOSED):
        _rtlib.register_project(project, path=registry)

    assert not registry.exists()


def test_git_routing_environment_cannot_hide_a_tracked_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = write_project(tmp_path / "project")
    ignore = project / ".roundtable" / ".gitignore"
    ignore.write_text(ignore.read_text() + "project.json\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    write_identity(project, str(uuid.uuid4()))
    subprocess.run(
        ["git", "add", "-f", ".roundtable/project.json"],
        cwd=project,
        check=True,
    )
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=decoy, check=True)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(project))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))

    assert _rtlib._project_identity_git_status(project) == "tracked"
    with pytest.raises(_rtlib.ProjectRegistryError, match="tracked by Git"):
        _rtlib._read_project_identity(project, required=True)


def test_renamed_project_auto_reindexes_from_identity_marker(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "original")
    _rtlib.register_project(
        original, path=registry, registered_at=REGISTERED_AT
    )
    original_entry = raw_entry_for(read_document(registry), original)
    moved = tmp_path / "renamed"

    original.rename(moved)
    moved = moved.resolve()
    mailbox = _rtlib.resolve_project_mailbox(
        moved, registry_path=registry
    )

    assert mailbox.project_uuid == original_entry["uuid"]
    assert mailbox.project_root == moved
    assert read_identity(moved)["uuid"] == original_entry["uuid"]
    document = read_document(registry)
    assert len(document["projects"]) == 1
    reindexed = raw_entry_for(document, moved)
    assert reindexed["uuid"] == original_entry["uuid"]
    assert reindexed["registered_at"] == original_entry["registered_at"]
    assert reindexed["status"] == "active"
    assert not any(
        entry.get("path") == str(original) for entry in document["projects"]
    )


def test_moved_target_cannot_bypass_current_path_claim_with_bad_sibling(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "original")
    claimed = write_project(tmp_path / "claimed")
    _rtlib.register_project(original, path=registry)
    _rtlib.register_project(claimed, path=registry)
    original_uuid = read_identity(original)["uuid"]
    claimed_uuid = read_identity(claimed)["uuid"]
    claimed_moved = tmp_path / "claimed-moved"
    claimed.rename(claimed_moved)
    current = tmp_path / "claimed"
    original.rename(current)
    document = read_document(registry)
    document["projects"].append(
        v2_entry(
            write_project(tmp_path / "malformed"),
            "not-a-uuid",
        )
    )
    write_document(registry, document)
    before = registry.read_bytes()

    with pytest.raises(SystemExit, match="aliases live project inode"):
        _rtlib.resolve_project_mailbox(
            current,
            registry_path=registry,
        )

    assert registry.read_bytes() == before
    assert read_identity(current)["uuid"] == original_uuid
    assert read_identity(claimed_moved)["uuid"] == claimed_uuid


def test_renamed_project_without_marker_refuses_to_mint_and_names_old_uuid(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "original")
    _rtlib.register_project(original, path=registry)
    project_uuid = read_identity(original)["uuid"]
    before = registry.read_bytes()
    moved = tmp_path / "renamed"
    original.rename(moved)
    marker = _rtlib.project_identity_path(moved)
    marker.unlink()

    with pytest.raises(SystemExit, match=project_uuid):
        _rtlib.resolve_project_mailbox(moved, registry_path=registry)
    with pytest.raises(SystemExit, match=project_uuid) as captured:
        _rtlib.register_project(moved, path=registry)
    assert "rt-projects rm <old-root>; rt-projects add <new-root>" in str(
        captured.value
    )
    with pytest.raises(SystemExit, match=project_uuid):
        _rtlib.unregister_project(moved, path=registry)

    assert registry.read_bytes() == before
    assert not marker.exists()


def test_agents_only_old_path_cannot_be_tombstoned_as_moved_identity(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "original")
    _rtlib.register_project(original, path=registry)
    project_uuid = read_identity(original)["uuid"]
    moved = tmp_path / "moved"
    original.rename(moved)
    write_project(original)
    before = registry.read_bytes()

    with pytest.raises(SystemExit, match=project_uuid):
        _rtlib.unregister_project(original, path=registry)

    assert registry.read_bytes() == before
    shutil.rmtree(original)
    mailbox = _rtlib.resolve_project_mailbox(
        moved,
        registry_path=registry,
    )
    assert mailbox.project_uuid == project_uuid
    assert raw_entry_for(read_document(registry), moved)["uuid"] == project_uuid


def test_case_only_rename_reindexes_same_live_inode(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    case_insensitive = (tmp_path / "caseprobe").exists()
    probe.rmdir()
    if not case_insensitive:
        pytest.skip("case-insensitive filesystem required")

    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "Alpha")
    _rtlib.register_project(original, path=registry)
    project_uuid = read_identity(original)["uuid"]
    renamed = tmp_path / "alpha"
    original.rename(renamed)
    renamed = renamed.resolve()

    mailbox = _rtlib.resolve_project_mailbox(
        renamed, registry_path=registry
    )

    assert mailbox.project_uuid == project_uuid
    entry = raw_entry_for(read_document(registry), renamed)
    assert entry["uuid"] == project_uuid
    assert entry["name"] == "alpha"


def test_active_case_alias_rows_for_same_inode_fail_closed(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    case_insensitive = (tmp_path / "caseprobe").exists()
    probe.rmdir()
    if not case_insensitive:
        pytest.skip("case-insensitive filesystem required")

    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "Alpha")
    _rtlib.register_project(project, path=registry)
    document = read_document(registry)
    alias = tmp_path / "alpha"
    document["projects"].append(
        v2_entry(alias, str(uuid.uuid4()))
    )
    write_document(registry, document)

    with pytest.raises(SystemExit, match="aliases live project inode"):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)


def test_copied_live_identity_fails_closed_without_corrupting_original(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "original")
    _rtlib.register_project(
        original, path=registry, registered_at=REGISTERED_AT
    )
    original_document = read_document(registry)
    project_uuid = read_identity(original)["uuid"]
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)

    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(copied, registry_path=registry)

    assert read_document(registry) == original_document
    mailbox = _rtlib.resolve_project_mailbox(
        original, registry_path=registry
    )
    assert mailbox.project_uuid == project_uuid
    assert mailbox.project_root == original


def test_copied_live_identity_cannot_hide_behind_bad_sibling(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    original = write_project(tmp_path / "original")
    _rtlib.register_project(original, path=registry)
    project_uuid = read_identity(original)["uuid"]
    copied = tmp_path / "copied"
    shutil.copytree(original, copied)
    document = read_document(registry)
    document["projects"].append(
        v2_entry(
            write_project(tmp_path / "malformed"),
            "not-a-uuid",
        )
    )
    write_document(registry, document)
    before = registry.read_bytes()

    with pytest.raises(SystemExit, match="refusing copied identity"):
        _rtlib.resolve_project_mailbox(copied, registry_path=registry)

    assert registry.read_bytes() == before
    assert read_identity(original)["uuid"] == project_uuid


@pytest.mark.parametrize("via_parent", [False, True])
def test_copied_identity_rejects_witness_behind_symlink_drift(
    tmp_path: Path,
    via_parent: bool,
) -> None:
    registry = tmp_path / "projects.yaml"
    parent = tmp_path / "parent" if via_parent else tmp_path
    original = write_project(parent / "original")
    _rtlib.register_project(original, path=registry)
    project_uuid = read_identity(original)["uuid"]
    if via_parent:
        real_parent = tmp_path / "parent-real"
        parent.rename(real_parent)
        parent.symlink_to(real_parent, target_is_directory=True)
        real_original = real_parent / "original"
    else:
        real_original = tmp_path / "original-real"
        original.rename(real_original)
        original.symlink_to(real_original, target_is_directory=True)
    copied = tmp_path / "copied"
    shutil.copytree(real_original, copied)
    before = registry.read_bytes()

    with pytest.raises(
        SystemExit,
        match="refusing copied identity|unverified old-path occupant",
    ):
        _rtlib.resolve_project_mailbox(copied, registry_path=registry)

    assert registry.read_bytes() == before
    actual = _rtlib.resolve_project_mailbox(
        real_original,
        registry_path=registry,
    )
    assert actual.project_uuid == project_uuid


def test_central_mailbox_uses_exact_uuid_path_without_scanning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(
        project, path=registry, registered_at=REGISTERED_AT
    )
    local = _rtlib.resolve_project_mailbox(
        project, registry_path=registry
    )
    document = read_document(registry)
    entry = raw_entry_for(document, project)
    entry["layout"] = "central"
    write_document(registry, document)

    mail_base = registry.parent / "mail"
    (mail_base / "decoy-one").mkdir(parents=True)
    (mail_base / "decoy-two").mkdir()
    central_root = mail_base / entry["uuid"]
    for directory in (
        central_root / "inbox",
        central_root / "messages",
        central_root / "locks",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    write_central_marker(central_root, entry["uuid"])
    original_iterdir = Path.iterdir

    def reject_mail_root_scan(path: Path):
        if path == mail_base:
            raise AssertionError("resolver scanned the central mail root")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", reject_mail_root_scan)
    mailbox = _rtlib.resolve_project_mailbox(
        project, registry_path=registry
    )

    assert mailbox.project_uuid == entry["uuid"]
    assert mailbox.project_root == project
    assert mailbox.layout == "central"
    assert mailbox.state_dir == project / ".roundtable"
    assert mailbox.mail_root == central_root
    # Both layouts retain an inbox container, so agent ids cannot collide with
    # the central messages/ and locks/ directories.
    assert mailbox.inbox_dir == central_root / "inbox"
    assert mailbox.messages_dir == central_root / "messages"
    assert mailbox.locks_dir == central_root / "locks"
    # The layout lock must not move as its protected layout changes.
    assert mailbox.layout_lock == local.layout_lock


def test_local_resolution_creates_no_central_mail_artifacts(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(
        project, path=registry, registered_at=REGISTERED_AT
    )

    mailbox = _rtlib.resolve_project_mailbox(
        project, registry_path=registry
    )

    assert mailbox.layout == "local"
    assert mailbox.mail_root == project / ".roundtable"
    assert not (registry.parent / "mail").exists()


def test_central_mail_root_symlink_fails_closed(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(project, path=registry)
    document = read_document(registry)
    entry = raw_entry_for(document, project)
    entry["layout"] = "central"
    write_document(registry, document)
    mail_parent = registry.parent / "mail"
    mail_parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (mail_parent / entry["uuid"]).symlink_to(
        outside, target_is_directory=True
    )

    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)

    assert list(outside.iterdir()) == []


def test_central_mail_directory_group_write_fails_closed(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    project = write_project(tmp_path / "project")
    _rtlib.register_project(project, path=registry)
    document = read_document(registry)
    entry = raw_entry_for(document, project)
    entry["layout"] = "central"
    write_document(registry, document)
    central_root = registry.parent / "mail" / entry["uuid"]
    for directory in (
        central_root / "inbox",
        central_root / "messages",
        central_root / "locks",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    write_central_marker(central_root, entry["uuid"])
    (central_root / "messages").chmod(0o777)

    with pytest.raises(SystemExit, match="group/other writable"):
        _rtlib.resolve_project_mailbox(project, registry_path=registry)


def test_registry_symlink_is_rejected_without_mutating_target(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry" / "projects.yaml"
    original = write_project(tmp_path / "original")
    _rtlib.register_project(original, path=registry)
    before = registry.read_bytes()
    alias = registry.with_name("projects-link.yaml")
    alias.symlink_to(registry)
    second = write_project(tmp_path / "second")

    with pytest.raises(FAIL_CLOSED):
        _rtlib.resolve_project_mailbox(original, registry_path=alias)
    with pytest.raises(FAIL_CLOSED):
        _rtlib.register_project(second, path=alias)

    assert registry.read_bytes() == before
    assert not _rtlib.project_identity_path(second).exists()


def test_v1_upgrade_rejects_registry_symlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    _write_legacy_registry(registry, [project])
    before = registry.read_bytes()
    alias = tmp_path / "projects-link.yaml"
    alias.symlink_to(registry)

    with pytest.raises(SystemExit, match="upgrade failed"):
        _rtlib.upgrade_project_registry(alias)

    assert registry.read_bytes() == before
    assert not _rtlib.legacy_registry_backup_path(alias).exists()
    assert not _rtlib.project_identity_path(project).exists()


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_registry_nonregular_source_fails_closed_without_blocking(
    tmp_path: Path,
    kind: str,
) -> None:
    registry = tmp_path / "projects.yaml"
    project = write_project(tmp_path / "project")
    if kind == "directory":
        registry.mkdir()
    else:
        os.mkfifo(registry)

    with pytest.raises(SystemExit, match="registration failed"):
        _rtlib.register_project(project, path=registry)

    assert not _rtlib.project_identity_path(project).exists()


def test_registry_compare_and_swap_preserves_concurrent_replacement(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "projects.yaml"
    write_document(registry, _rtlib._empty_projects_doc())
    replacement = {
        "schema": _rtlib.PROJECTS_SCHEMA,
        "projects": [],
        "concurrent": True,
    }

    def mutate(document, _source_payload, _parent_fd):
        document["intended"] = True
        write_document(registry, replacement)
        return True

    with pytest.raises(
        _rtlib.ProjectRegistryError,
        match="changed during update",
    ):
        _rtlib._update_project_registry(mutate, registry)

    assert read_document(registry) == replacement


def _path_expression_parts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return [
            *_path_expression_parts(node.left),
            *_path_expression_parts(node.right),
        ]
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*_path_expression_parts(node.value), node.attr]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [
            part
            for part in node.value.replace("\\", "/").split("/")
            if part
        ]
    if isinstance(node, ast.Call):
        is_path_constructor = (
            isinstance(node.func, ast.Name)
            and node.func.id in {"Path", "PurePath"}
        )
        is_join = (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"join", "joinpath"}
        )
        if is_path_constructor:
            parts = []
        elif is_join:
            parts = _path_expression_parts(node.func.value)
        else:
            return []
        for argument in node.args:
            parts.extend(_path_expression_parts(argument))
        return parts
    return []


def _mailbox_path_violations(source_path: Path) -> list[str]:
    violations: list[str] = []
    layout_parts = {"inbox", "messages", "locks"}
    tree = ast.parse(source_path.read_text(), filename=str(source_path))
    for node in ast.walk(tree):
        parts = _path_expression_parts(node)
        literal_layout = (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(part in layout_parts for part in parts)
        )
        if literal_layout:
            violations.append(
                f"{source_path.name}:{getattr(node, 'lineno', 0)} "
                + " / ".join(parts)
            )
    return violations


@pytest.mark.parametrize(
    "source",
    [
        'value = Path(root, ".roundtable", "inbox")\n',
        'value = state.joinpath("messages")\n',
        'value = os.path.join(root, ".roundtable", "locks")\n',
        'value = ".roundtable/inbox"\n',
        'folder = "messages"\nvalue = arbitrary_alias / folder\n',
    ],
)
def test_mailbox_source_invariant_detects_indirect_construction(
    tmp_path: Path,
    source: str,
) -> None:
    candidate = tmp_path / "consumer.py"
    candidate.write_text(source)
    assert _mailbox_path_violations(candidate)


# The layout is defined in exactly two places, and both must be allowed to
# spell it out. Every other production source is checked, whether or not
# anybody remembered to add it here.
#
# The exemption is by *count*, not by path: removing these files from the check
# entirely would let a genuinely new construction hide behind the old allowed
# ones. Pinning the number means an added construction fails and has to be
# justified by bumping it deliberately.
LAYOUT_DEFINING_SOURCES = {
    "bin/_rtlib.py": (
        6,
        "defines the layout the other sources are forbidden to rebuild",
    ),
    "bin/_rtmigrate.py": (
        12,
        "moves projects between layouts; frozen by decision.md 2026-07-29",
    ),
}

# Sources that resolve a mailbox without the layout lock and then read maildir
# paths for advisory output only. Each is a deliberate unlocked reader, not an
# oversight, and none of them writes mail. Recorded rather than silently
# omitted so the set cannot grow unnoticed.
ADVISORY_UNLOCKED_READERS = {
    "bin/pneu": "counts unread mail for the launcher card",
    "bin/rt-projects": "prints resolved layout metadata for diagnostics",
    "bin/_rtlauncher.py": "derives the Grok seat maildir path for an external monitor",
}


def test_layout_path_invariant_covers_every_production_source() -> None:
    """The derived universe must include the sources a hand list forgot."""

    covered = {facts.relative for facts in consumers.all_facts()}

    assert "integrations/grok/roundtable/__init__.py" in covered
    assert "integrations/openclaw/roundtable/__init__.py" in covered
    assert "integrations/hermes/pneu/__init__.py" in covered
    assert "pneu_packaging/smoke.py" in covered
    assert {"bin/rt-say", "bin/rt-inbox", "bin/rt-ack", "bin/rt-wait-inbox"} <= covered
    # bin/roundtable is a symlink to bin/pneu and must be counted once.
    assert "bin/roundtable" not in covered


# The detector deliberately flags a bare ``"inbox"``/``"messages"``/``"locks"``
# literal anywhere, because that is how it catches a layout path assembled
# through a variable. Widening its reach to every production source surfaced
# one literal that is not a path at all. It is allowed by exact value rather
# than by exempting the file, so any *other* literal in the same source still
# fails.
ALLOWED_NON_LAYOUT_LITERALS = {
    "bin/pneu": {"inbox": 'the "inbox" CLI subcommand alias for rt-inbox'},
}


def _unexplained_violations(facts) -> list[str]:
    allowed = ALLOWED_NON_LAYOUT_LITERALS.get(facts.relative, {})
    return [
        violation
        for violation in _mailbox_path_violations(facts.path)
        if violation.split(" ", 1)[1] not in allowed
    ]


def test_production_sources_do_not_construct_layout_paths() -> None:
    violations = {
        facts.relative: _unexplained_violations(facts)
        for facts in consumers.all_facts()
        if facts.relative not in LAYOUT_DEFINING_SOURCES
        and _unexplained_violations(facts)
    }
    assert violations == {}


def test_layout_defining_exemptions_are_still_earned() -> None:
    """A stale or over-broad exemption is a hidden gap, so it is pinned."""

    known = {facts.relative for facts in consumers.all_facts()}
    for relative, (expected, reason) in LAYOUT_DEFINING_SOURCES.items():
        assert relative in known, f"exemption names a missing source: {relative}"
        assert reason
        observed = _mailbox_path_violations(consumers.ROOT / relative)
        assert len(observed) == expected, (
            f"{relative} now constructs {len(observed)} layout paths, not "
            f"{expected}. A new one is not covered by this exemption: {observed}"
        )


def test_allowed_non_layout_literals_are_still_present() -> None:
    facts_by_source = {facts.relative: facts for facts in consumers.all_facts()}
    for relative, allowed in ALLOWED_NON_LAYOUT_LITERALS.items():
        assert relative in facts_by_source, f"allowance names a missing source: {relative}"
        observed = {
            violation.split(" ", 1)[1]
            for violation in _mailbox_path_violations(facts_by_source[relative].path)
        }
        for literal, reason in allowed.items():
            assert reason
            assert literal in observed, (
                f"{relative} no longer contains the allowed literal {literal!r}; "
                "drop the allowance"
            )


def test_maildir_consumers_use_the_locked_resolver_only() -> None:
    violations = {
        facts.relative
        for facts in consumers.maildir_consumers()
        if facts.relative not in LAYOUT_DEFINING_SOURCES
        and facts.relative not in ADVISORY_UNLOCKED_READERS
        and (facts.calls_raw_resolver or not facts.calls_locked_resolver)
    }
    assert violations == set()


def test_advisory_unlocked_readers_are_still_earned() -> None:
    known = {facts.relative: facts for facts in consumers.all_facts()}
    for relative, reason in ADVISORY_UNLOCKED_READERS.items():
        assert relative in known, f"exemption names a missing source: {relative}"
        assert reason
        facts = known[relative]
        assert facts.touches_maildir and facts.calls_raw_resolver, (
            f"{relative} no longer reads the maildir through a raw resolver; "
            "drop its exemption"
        )


def test_indirect_consumers_do_not_retain_waiter_mailbox_paths() -> None:
    hermes = (
        ROOT / "integrations" / "hermes" / "pneu" / "__init__.py"
    ).read_text()
    smoke = (ROOT / "pneu_packaging" / "smoke.py").read_text()

    assert "--wait-last-wake-drained" in hermes
    assert "_triggered_new_dir" not in hermes
    assert "_generation_is_pending" not in hermes
    assert "locked_project_mailbox_checked" in smoke


def test_rt_refresh_finishes_external_assignment_before_layout_lock() -> None:
    source = BIN / "rt-refresh"
    tree = ast.parse(source.read_text(), filename=str(source))
    assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assign_agents"
    ]
    layout_sections = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "locked_project_mailbox"
            for item in node.items
        )
    ]
    assert len(assignments) == len(layout_sections) == 1
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assign_agents"
        for node in ast.walk(layout_sections[0])
    )
