from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlib  # noqa: E402
import _rtmigrate  # noqa: E402


def write_registered_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "work" / "project"
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        """schema: roundtable.agents.v1
project: "."
agents:
  codex:
    harness: codex
    instances:
      - id: codex
  claude:
    harness: claude-code
    instances:
      - id: claude
"""
    )
    (state / ".gitignore").write_text(
        "project.json\nruntime.json\nmessages/\nlocks/\ninbox/\n"
    )
    for name in ("inbox", "messages", "locks"):
        (state / name).mkdir()
    incoming_new = state / "inbox" / "claude" / "new"
    incoming_cur = state / "inbox" / "claude" / "cur"
    incoming_tmp = state / "inbox" / "claude" / "tmp"
    for path in (incoming_new, incoming_cur, incoming_tmp):
        path.mkdir(parents=True, exist_ok=True)
    message = incoming_new / "interrupted.md"
    message.write_bytes(b"durable inbound\n")
    os.link(message, incoming_cur / message.name)
    ledger = state / "messages" / "codex.jsonl"
    ledger.write_bytes(b'{"msg_id":"interrupted"}\n')
    registry = tmp_path / "registry" / "projects.yaml"
    assert _rtlib.register_project(project, path=registry)
    backup_root = tmp_path / "backups"
    return project.resolve(), registry, backup_root


def registry_layout(project: Path, registry: Path) -> str:
    return _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    ).layout


def assert_internal_hardlink(root: Path) -> None:
    new = root / "inbox" / "claude" / "new" / "interrupted.md"
    cur = root / "inbox" / "claude" / "cur" / "interrupted.md"
    assert new.read_bytes() == b"durable inbound\n"
    assert cur.read_bytes() == b"durable inbound\n"
    assert (new.stat().st_dev, new.stat().st_ino) == (
        cur.stat().st_dev,
        cur.stat().st_ino,
    )


def test_forward_and_rollback_preserve_bytes_links_and_post_cutover_mail(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)

    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert migrated["status"] == "migrated"
    assert migrated["committed"] is True
    assert migrated["files"] == 3
    assert migrated["bytes"] > 0
    assert migrated["exclusive_hold_ms"] > 0
    assert migrated["lock_wait_ms"] >= 0
    manifest = Path(migrated["manifest"])
    assert manifest.is_file()
    mailbox = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert mailbox.layout == "central"
    assert mailbox.mail_root == registry.parent / "mail" / mailbox.project_uuid
    assert_internal_hardlink(mailbox.mail_root)
    assert not (project / ".roundtable" / "inbox").exists()
    assert not (project / ".roundtable" / "messages").exists()
    assert not (project / ".roundtable" / "locks").exists()
    assert (project / ".roundtable" / "mail").is_symlink()
    assert os.readlink(project / ".roundtable" / "mail") == str(
        mailbox.mail_root
    )
    assert "mail" in (project / ".roundtable" / ".gitignore").read_text().splitlines()

    post_cutover = (
        mailbox.inbox_dir
        / "claude"
        / "new"
        / "post-cutover.md"
    )
    post_cutover.write_bytes(b"arrived after central cutover\n")

    rolled_back = _rtmigrate.rollback_project(
        project,
        manifest,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert rolled_back["status"] == "rolled back"
    assert rolled_back["committed"] is True
    assert Path(rolled_back["rollback_manifest"]).is_file()
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert local.layout == "local"
    assert local.mail_root == project / ".roundtable"
    assert_internal_hardlink(local.mail_root)
    assert (
        local.inbox_dir
        / "claude"
        / "new"
        / "post-cutover.md"
    ).read_bytes() == b"arrived after central cutover\n"
    assert not (project / ".roundtable" / "mail").exists()
    assert not mailbox.mail_root.exists()


def test_migrate_and_rollback_are_idempotent_for_exact_generation(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    first = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    second = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert second["status"] == "already central"
    assert second["manifest"] == first["manifest"]

    first_rollback = _rtmigrate.rollback_project(
        project,
        first["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    second_rollback = _rtmigrate.rollback_project(
        project,
        first["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    assert second_rollback["status"] == "already local"
    assert second_rollback["rollback_manifest"] == first_rollback[
        "rollback_manifest"
    ]


def archive_bundle_for(record: Path) -> Path:
    document = json.loads(record.read_text())
    return Path(document["archive_manifest"]).parent


def restore_legacy_archive_marker(
    project: Path,
    registry: Path,
    recovery_record: Path,
) -> Path:
    record = json.loads(recovery_record.read_text())
    archive_manifest = Path(record["archive_manifest"])
    marker_path = (
        _rtlib.resolve_project_mailbox_checked(
            project,
            registry_path=registry,
        ).mail_root
        / _rtlib.CENTRAL_MAIL_MARKER_NAME
    )
    marker = json.loads(marker_path.read_text())
    marker.update(
        {
            "manifest": str(archive_manifest),
            "manifest_sha256": record["archive_manifest_sha256"],
        }
    )
    marker_path.write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n"
    )
    shutil.rmtree(registry.parent / _rtmigrate.RECOVERY_RECORDS_DIRECTORY)
    return archive_manifest


def test_legacy_central_marker_import_is_crash_retryable_and_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    original_record = Path(migrated["manifest"])
    legacy_manifest = restore_legacy_archive_marker(
        project,
        registry,
        original_record,
    )
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    late = central.inbox_dir / "claude" / "new" / "legacy-import-late.md"
    late.write_bytes(b"preserve after legacy import\n")
    original_rebind = _rtmigrate._rebind_central_marker

    def interrupt_rebind(*_args, **_kwargs):
        raise _rtmigrate.MailMigrationError(
            "simulated marker rebind interruption"
        )

    monkeypatch.setattr(
        _rtmigrate,
        "_rebind_central_marker",
        interrupt_rebind,
    )
    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="simulated marker rebind interruption",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    canonical = _rtmigrate._recovery_record_path(
        registry,
        central.project_uuid,
        json.loads(legacy_manifest.read_text())["operation_id"],
    )
    assert canonical.is_file()
    monkeypatch.setattr(
        _rtmigrate,
        "_rebind_central_marker",
        original_rebind,
    )
    repaired = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert repaired["status"] == "already central"
    assert Path(repaired["manifest"]) == canonical
    marker = _rtlib.validate_central_mail_marker(
        central.mail_root,
        central.project_uuid,
    )
    assert Path(marker["manifest"]) == canonical

    shutil.rmtree(legacy_manifest.parent)
    rolled_back = _rtmigrate.rollback_project(
        project,
        canonical,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert rolled_back["status"] == "rolled back"
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert (
        local.inbox_dir / "claude" / "new" / "legacy-import-late.md"
    ).read_bytes() == b"preserve after legacy import\n"


def test_rollback_accepts_active_legacy_manifest_as_one_time_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    legacy_manifest = restore_legacy_archive_marker(
        project,
        registry,
        Path(migrated["manifest"]),
    )
    original_load = _rtmigrate.load_manifest
    legacy_loads = 0

    def remove_archive_after_import(*args, **kwargs):
        nonlocal legacy_loads
        loaded = original_load(*args, **kwargs)
        if Path(args[0]) == legacy_manifest:
            legacy_loads += 1
            shutil.rmtree(legacy_manifest.parent)
        return loaded

    monkeypatch.setattr(
        _rtmigrate,
        "load_manifest",
        remove_archive_after_import,
    )

    rolled_back = _rtmigrate.rollback_project(
        project,
        legacy_manifest,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert rolled_back["status"] == "rolled back"
    assert legacy_loads == 1
    assert not legacy_manifest.exists()
    assert Path(rolled_back["manifest"]) != legacy_manifest
    assert Path(rolled_back["manifest"]).is_file()
    assert registry_layout(project, registry) == "local"


def test_post_cutover_repair_survives_deleted_archive_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "after_registry_flip",
    )
    with pytest.raises(_rtmigrate.MailMigrationCommittedError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )
    mailbox = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    marker = _rtlib.validate_central_mail_marker(
        mailbox.mail_root,
        mailbox.project_uuid,
    )
    record = Path(marker["manifest"])
    shutil.rmtree(archive_bundle_for(record))

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repaired = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert repaired["status"] == "already central"
    assert Path(repaired["manifest"]) == record
    assert (project / ".roundtable" / "mail").is_symlink()
    assert not (project / ".roundtable" / "inbox").exists()


def test_rollback_preserves_postcutover_mail_after_forward_archive_loss(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    record = Path(migrated["manifest"])
    shutil.rmtree(archive_bundle_for(record))
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    late = central.inbox_dir / "claude" / "new" / "after-archive-loss.md"
    late.write_bytes(b"still recoverable\n")

    rolled_back = _rtmigrate.rollback_project(
        project,
        record,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert rolled_back["status"] == "rolled back"
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert (
        local.inbox_dir / "claude" / "new" / "after-archive-loss.md"
    ).read_bytes() == b"still recoverable\n"


def test_rollback_repair_survives_both_deleted_archive_bundles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    forward_record = Path(migrated["manifest"])
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "rollback_after_registry_flip",
    )
    with pytest.raises(_rtmigrate.MailMigrationCommittedError):
        _rtmigrate.rollback_project(
            project,
            forward_record,
            registry_path=registry,
            backup_root=backup_root,
        )
    marker = json.loads(
        (project / ".roundtable" / _rtmigrate.ROLLBACK_MARKER_NAME).read_text()
    )
    rollback_record = Path(marker["rollback_manifest"])
    shutil.rmtree(archive_bundle_for(forward_record))
    shutil.rmtree(archive_bundle_for(rollback_record))
    fresh = (
        project
        / ".roundtable"
        / "inbox"
        / "claude"
        / "new"
        / "after-rollback-cutover.md"
    )
    fresh.write_bytes(b"new local authority\n")

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repaired = _rtmigrate.rollback_project(
        project,
        forward_record,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert repaired["status"] == "already local"
    assert Path(repaired["rollback_manifest"]) == rollback_record
    assert fresh.read_bytes() == b"new local authority\n"


def test_pre_cutover_published_candidate_is_rebuilt_after_local_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "after_central_publish",
    )

    with pytest.raises(
        _rtmigrate.InjectedMigrationFailure,
        match="after_central_publish",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    stale = registry.parent / "mail" / project_uuid
    assert stale.is_dir()
    late = (
        project
        / ".roundtable"
        / "inbox"
        / "claude"
        / "new"
        / "late-local.md"
    )
    late.write_bytes(b"local stayed authoritative\n")

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert result["committed"] is True
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    ).mail_root
    assert (
        central / "inbox" / "claude" / "new" / "late-local.md"
    ).read_bytes() == b"local stayed authoritative\n"
    assert list((registry.parent / "mail").glob(f".{project_uuid}.stale.*"))


def test_post_cutover_failure_reports_committed_and_retry_repairs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "after_registry_flip",
    )

    with pytest.raises(
        _rtmigrate.MailMigrationCommittedError,
        match="cutover committed",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    assert (project / ".roundtable" / "inbox").is_dir()
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repaired = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert repaired["status"] == "already central"
    assert not (project / ".roundtable" / "inbox").exists()
    assert (project / ".roundtable" / "mail").is_symlink()


@pytest.mark.parametrize(
    "phase",
    [
        "after_backup_staging",
        "after_backup",
        "before_central_publish",
        "after_central_publish",
        "before_registry_flip",
    ],
)
def test_forward_precommit_failpoints_remain_local_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.InjectedMigrationFailure, match=phase):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert_internal_hardlink(project / ".roundtable")
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    retried = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert retried["committed"] is True
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert central.layout == "central"
    assert_internal_hardlink(central.mail_root)


@pytest.mark.parametrize(
    "phase",
    [
        "after_registry_flip",
        "after_local_cleanup",
        "after_bookmark",
    ],
)
def test_forward_postcommit_failpoints_report_commit_and_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.MailMigrationCommittedError, match="committed"):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repaired = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert repaired["status"] == "already central"
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert_internal_hardlink(central.mail_root)
    assert (project / ".roundtable" / "mail").is_symlink()


def test_partial_post_cutover_cleanup_resumes_without_stale_active_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "after_registry_flip",
    )
    with pytest.raises(_rtmigrate.MailMigrationCommittedError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    ).mail_root
    marker = _rtlib.validate_central_mail_marker(
        central,
        json.loads(_rtlib.project_identity_path(project).read_text())["uuid"],
    )
    state = project / ".roundtable"
    partial = state / f".central-mail-retired.{marker['operation_id']}"
    partial.mkdir()
    (state / "inbox").rename(partial / "inbox")

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repaired = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert repaired["status"] == "already central"
    assert repaired["warnings"]
    assert not (state / "inbox").exists()
    assert not (state / "messages").exists()
    assert not (state / "locks").exists()
    assert partial.is_dir()


def test_rollback_retry_before_flip_resnapshots_new_central_mail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "rollback_after_local_install",
    )
    with pytest.raises(_rtmigrate.InjectedMigrationFailure):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    late = central.inbox_dir / "claude" / "new" / "after-failed-rollback.md"
    late.write_bytes(b"still central authority\n")

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    result = _rtmigrate.rollback_project(
        project,
        migrated["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )

    assert result["status"] == "rolled back"
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert (
        local.inbox_dir
        / "claude"
        / "new"
        / "after-failed-rollback.md"
    ).read_bytes() == b"still central authority\n"


def test_rollback_retry_after_flip_never_overwrites_new_local_mail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "rollback_after_registry_flip",
    )
    with pytest.raises(_rtmigrate.MailMigrationCommittedError):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    fresh = local.inbox_dir / "claude" / "new" / "new-local.md"
    fresh.write_bytes(b"local after rollback cutover\n")

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    result = _rtmigrate.rollback_project(
        project,
        migrated["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )

    assert result["status"] == "already local"
    assert fresh.read_bytes() == b"local after rollback cutover\n"


@pytest.mark.parametrize(
    "phase",
    [
        "rollback_after_backup",
        "rollback_after_local_install",
        "rollback_before_registry_flip",
    ],
)
def test_rollback_precommit_failpoints_remain_central_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.InjectedMigrationFailure, match=phase):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    retried = _rtmigrate.rollback_project(
        project,
        migrated["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    assert retried["committed"] is True
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert local.layout == "local"
    assert_internal_hardlink(local.mail_root)


@pytest.mark.parametrize(
    "phase",
    [
        "rollback_after_registry_flip",
        "rollback_after_central_retire",
    ],
)
def test_rollback_postcommit_failpoints_report_commit_and_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.MailMigrationCommittedError, match="committed"):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repaired = _rtmigrate.rollback_project(
        project,
        migrated["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    assert repaired["status"] == "already local"
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert_internal_hardlink(local.mail_root)


def test_old_manifest_cannot_rollback_a_later_central_generation(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    first = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    _rtmigrate.rollback_project(
        project,
        first["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    second = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert second["manifest"] != first["manifest"]

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="not requested manifest",
    ):
        _rtmigrate.rollback_project(
            project,
            first["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"


def test_foreign_central_collision_is_left_untouched(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    collision = registry.parent / "mail" / project_uuid
    collision.mkdir(parents=True)
    foreign = collision / "foreign.txt"
    foreign.write_bytes(b"do not replace\n")

    with pytest.raises(_rtlib.ProjectRegistryError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert foreign.read_bytes() == b"do not replace\n"
    assert "Roundtable central mail" not in (
        project / ".roundtable" / ".gitignore"
    ).read_text()


def test_rollback_rejects_tampered_manifest_without_changing_central(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    manifest = Path(migrated["manifest"])
    document = json.loads(manifest.read_text())
    document["project_uuid"] = str(uuid_for_test())
    manifest.write_text(json.dumps(document) + "\n")
    manifest.chmod(0o600)

    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.rollback_project(
            project,
            manifest,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"


def uuid_for_test():
    import uuid

    return uuid.uuid4()


def test_external_hardlink_is_rejected_before_backup_or_cutover(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    source = (
        project
        / ".roundtable"
        / "inbox"
        / "claude"
        / "new"
        / "interrupted.md"
    )
    os.link(source, tmp_path / "outside-link.md")

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="hard links outside",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert not backup_root.exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo", "group-writable"])
def test_unsafe_source_entry_fails_before_backup(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    path = project / ".roundtable" / "inbox" / "unsafe"
    if unsafe_kind == "symlink":
        path.symlink_to(project / ".roundtable" / "messages")
    elif unsafe_kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"unsafe mode\n")
        path.chmod(0o666)

    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert not backup_root.exists()


def test_registry_lock_timeout_leaves_local_authoritative_and_is_retryable(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    lock = os.open(
        registry.with_name(f"{registry.name}.lock"),
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with pytest.raises(_rtlib.ProjectRegistryLockTimeout):
            _rtmigrate.migrate_project(
                project,
                registry_path=registry,
                backup_root=backup_root,
                registry_lock_timeout=0.05,
            )
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)

    assert registry_layout(project, registry) == "local"
    retried = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert retried["committed"] is True


def test_exclusive_hold_metric_covers_copy_delay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original = _rtmigrate.copy_snapshot

    def delayed_copy(*args, **kwargs):
        time.sleep(0.04)
        return original(*args, **kwargs)

    monkeypatch.setattr(_rtmigrate, "copy_snapshot", delayed_copy)
    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert result["exclusive_hold_ms"] >= 80
    assert result["lock_wait_ms"] < result["exclusive_hold_ms"]


def test_exclusive_hold_metric_starts_at_resource_acquisition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original = _rtlib.resolve_project_mailbox_checked
    calls = 0

    def delayed_resolve(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(0.08)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        _rtlib,
        "resolve_project_mailbox_checked",
        delayed_resolve,
    )
    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert calls >= 2
    assert result["exclusive_hold_ms"] >= 75


def test_exclusive_hold_includes_post_flock_scheduling_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original = _rtlib._acquire_flock_before
    calls = 0

    def delayed_return(*args, **kwargs):
        nonlocal calls
        acquired_at = original(*args, **kwargs)
        calls += 1
        if calls == 4:
            time.sleep(0.08)
        return acquired_at

    monkeypatch.setattr(
        _rtlib,
        "_acquire_flock_before",
        delayed_return,
    )
    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert calls >= 4
    assert result["exclusive_hold_ms"] >= 75


def test_unconfirmed_migration_composes_layout_and_registry_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    budget = 2.0
    observed_layout = []
    observed_registry = []
    original_locked = _rtmigrate.locked_project_mailbox_checked
    original_flip = _rtmigrate._flip_layout

    def capture_locked(*args, **kwargs):
        observed_layout.append(
            (kwargs.get("exclusive", False), kwargs["timeout"])
        )
        return original_locked(*args, **kwargs)

    def capture_flip(*args, **kwargs):
        observed_registry.append(kwargs["lock_timeout"])
        return original_flip(*args, **kwargs)

    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_BUDGET_SECONDS",
        budget,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "locked_project_mailbox_checked",
        capture_locked,
    )
    monkeypatch.setattr(_rtmigrate, "_flip_layout", capture_flip)

    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
        layout_lock_timeout=10.0,
        registry_lock_timeout=10.0,
    )

    assert observed_layout == [(False, 10.0), (True, budget)]
    assert len(observed_registry) == 1
    assert 0 < observed_registry[0] < budget
    assert result["registry_wait_cap_ms"] == pytest.approx(
        observed_registry[0] * 1000,
        abs=0.001,
    )


def test_exhausted_hold_budget_refuses_before_registry_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original_gitignore = _rtmigrate._ensure_gitignore

    def delayed_gitignore(*args, **kwargs):
        result = original_gitignore(*args, **kwargs)
        time.sleep(0.08)
        return result

    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_BASE_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_ENTRY_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_MIB_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_BUDGET_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "_ensure_gitignore",
        delayed_gitignore,
    )

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="exhausted its safety budget",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
            registry_lock_timeout=1.0,
        )

    assert registry_layout(project, registry) == "local"


def test_confirmed_quiescence_bypasses_normal_hold_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original_gitignore = _rtmigrate._ensure_gitignore

    def delayed_gitignore(*args, **kwargs):
        result = original_gitignore(*args, **kwargs)
        time.sleep(0.08)
        return result

    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_BASE_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_ENTRY_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_MIB_SECONDS",
        0.0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "MIGRATION_PREFLIGHT_BUDGET_SECONDS",
        0.05,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "_ensure_gitignore",
        delayed_gitignore,
    )

    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
        registry_lock_timeout=0.7,
        confirm_quiesced=True,
    )

    assert result["committed"] is True
    assert result["registry_wait_cap_ms"] == pytest.approx(700.0)


def test_quiescence_override_requires_an_explicit_boolean(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="explicit boolean",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
            confirm_quiesced="false",
        )

    assert registry_layout(project, registry) == "local"
    assert not backup_root.exists()


@pytest.mark.parametrize(
    "location",
    ["local-locks", "local-inbox", "central-parent"],
)
def test_forward_rejects_backup_roots_overlapping_managed_state(
    tmp_path: Path,
    location: str,
) -> None:
    project, registry, _backup_root = write_registered_project(tmp_path)
    state = project / ".roundtable"
    locations = {
        "local-locks": state / "locks" / "backup",
        "local-inbox": state / "inbox" / "claude" / "new" / "backup",
        "central-parent": registry.parent / "mail",
    }
    backup_root = locations[location]
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="overlaps protected",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert not backup_root.exists()
    assert not _rtlib.central_mail_root(registry, project_uuid).exists()
    assert_internal_hardlink(state)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="case-folded filesystem alias is a macOS path threat",
)
@pytest.mark.parametrize("location", ["local-locks", "central-parent"])
def test_forward_rejects_case_only_backup_aliases(
    tmp_path: Path,
    location: str,
) -> None:
    project, registry, _backup_root = write_registered_project(tmp_path)
    project_alias = project.parent.parent / "WORK" / "PROJECT"
    try:
        same_project = project_alias.samefile(project)
    except OSError:
        same_project = False
    if not same_project:
        pytest.skip("test volume is case-sensitive")
    locations = {
        "local-locks": (
            project_alias / ".roundtable" / "locks" / "backup"
        ),
        "central-parent": registry.parent / "MAIL",
    }
    selected = locations[location]

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="overlaps protected",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=selected,
        )

    assert registry_layout(project, registry) == "local"
    assert not selected.exists()
    assert_internal_hardlink(project / ".roundtable")


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="macOS path aliases use Unicode normalization",
)
def test_overlap_comparison_normalizes_case_and_unicode() -> None:
    assert _rtmigrate._paths_overlap(
        Path("/tmp/Caf\u00e9/project/state"),
        Path("/tmp/cafe\u0301/PROJECT"),
    )


@pytest.mark.parametrize("location", ["local-state", "forward-bundle"])
def test_rollback_rejects_backup_roots_overlapping_recovery_state(
    tmp_path: Path,
    location: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    manifest = Path(migrated["manifest"])
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    late = central.inbox_dir / "claude" / "new" / "late.md"
    late.write_bytes(b"central remains authoritative\n")
    archive_manifest = Path(
        json.loads(manifest.read_text())["archive_manifest"]
    )
    locations = {
        "local-state": project / ".roundtable" / "locks" / "backup",
        "forward-bundle": (
            archive_manifest.parent / "payload" / "inbox" / "backup"
        ),
    }
    selected = locations[location]
    manifest_before = manifest.read_bytes()

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="overlaps protected",
    ):
        _rtmigrate.rollback_project(
            project,
            manifest,
            registry_path=registry,
            backup_root=selected,
        )

    assert registry_layout(project, registry) == "central"
    assert late.read_bytes() == b"central remains authoritative\n"
    assert not selected.exists()
    assert manifest.read_bytes() == manifest_before


def test_backup_uuid_directory_fsync_failure_blocks_cutover_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    backup_root.mkdir(mode=0o700)
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    expected_uuid_root = backup_root / project_uuid
    original = _rtmigrate._sync_new_private_directory

    def fail_uuid_sync(parent_fd, child_fd, path, metrics):
        if path == expected_uuid_root:
            raise OSError(errno.EIO, "injected directory fsync failure")
        return original(parent_fd, child_fd, path, metrics)

    monkeypatch.setattr(
        _rtmigrate,
        "_sync_new_private_directory",
        fail_uuid_sync,
    )
    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="cannot persist project migration backup root",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert not _rtlib.central_mail_root(registry, project_uuid).exists()
    assert_internal_hardlink(project / ".roundtable")

    monkeypatch.setattr(
        _rtmigrate,
        "_sync_new_private_directory",
        original,
    )
    retried = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    assert retried["committed"] is True


def test_private_directory_creation_syncs_each_new_level_in_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "one" / "two" / "three"
    observed: list[Path] = []
    original = _rtmigrate._sync_new_private_directory

    def record(parent_fd, child_fd, path, metrics):
        observed.append(path)
        return original(parent_fd, child_fd, path, metrics)

    monkeypatch.setattr(
        _rtmigrate,
        "_sync_new_private_directory",
        record,
    )
    result = _rtmigrate._ensure_private_directory(
        target,
        "test private directory",
        _rtmigrate.MigrationMetrics(),
    )

    assert result == target
    assert observed == [
        tmp_path,
        tmp_path / "one",
        tmp_path / "one" / "two",
        target,
    ]
    assert all(
        path.stat().st_mode & 0o777 == 0o700
        for path in observed
    )


def test_private_directory_creation_syncs_eexist_race_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "one" / "two"
    observed: list[Path] = []
    original_mkdir = os.mkdir
    original_sync = _rtmigrate._sync_new_private_directory
    injected = False

    def raced_mkdir(path, mode=0o777, *, dir_fd=None):
        nonlocal injected
        if path == "one" and not injected:
            injected = True
            original_mkdir(path, mode, dir_fd=dir_fd)
            raise FileExistsError(errno.EEXIST, "simulated creator race", path)
        return original_mkdir(path, mode, dir_fd=dir_fd)

    def record_sync(parent_fd, child_fd, path, metrics):
        observed.append(path)
        return original_sync(parent_fd, child_fd, path, metrics)

    monkeypatch.setattr(os, "mkdir", raced_mkdir)
    monkeypatch.setattr(
        _rtmigrate,
        "_sync_new_private_directory",
        record_sync,
    )
    _rtmigrate._ensure_private_directory(
        target,
        "test raced private directory",
        _rtmigrate.MigrationMetrics(),
    )

    assert injected is True
    assert observed == [tmp_path, tmp_path / "one", target]


def test_existing_private_directory_gets_child_parent_sync_barrier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "existing"
    target.mkdir(mode=0o700)
    observed: list[Path] = []
    original_sync = _rtmigrate._sync_new_private_directory

    def record_sync(parent_fd, child_fd, path, metrics):
        observed.append(path)
        return original_sync(parent_fd, child_fd, path, metrics)

    monkeypatch.setattr(
        _rtmigrate,
        "_sync_new_private_directory",
        record_sync,
    )
    _rtmigrate._ensure_private_directory(
        target,
        "test existing private directory",
        _rtmigrate.MigrationMetrics(),
    )

    assert observed == [target]


def test_syntactic_marker_without_verified_manifest_is_never_moved(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    collision = _rtlib.central_mail_root(registry, project_uuid)
    collision.mkdir(parents=True)
    foreign = collision / "foreign.txt"
    foreign.write_bytes(b"foreign collision\n")
    marker = collision / _rtlib.CENTRAL_MAIL_MARKER_NAME
    marker.write_text(
        json.dumps(
            {
                "schema": _rtlib.CENTRAL_MAIL_MARKER_SCHEMA,
                "project_uuid": project_uuid,
                "operation_id": str(uuid_for_test()),
                "manifest": str(tmp_path / "missing-manifest.json"),
                "manifest_sha256": "a" * 64,
                "snapshot_digest": "b" * 64,
            }
        )
        + "\n"
    )
    marker.chmod(0o600)
    gitignore_before = (
        project / ".roundtable" / ".gitignore"
    ).read_bytes()

    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert foreign.read_bytes() == b"foreign collision\n"
    assert not list(collision.parent.glob(f".{project_uuid}.stale.*"))
    assert not backup_root.exists()
    assert (
        project / ".roundtable" / ".gitignore"
    ).read_bytes() == gitignore_before


def test_tampered_pre_cutover_candidate_is_never_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "after_central_publish",
    )
    with pytest.raises(_rtmigrate.InjectedMigrationFailure):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    candidate = _rtlib.central_mail_root(registry, project_uuid)
    tampered = candidate / "inbox" / "claude" / "new" / "tampered.md"
    tampered.write_bytes(b"not in the candidate manifest\n")

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert tampered.read_bytes() == b"not in the candidate manifest\n"
    assert not list(candidate.parent.glob(f".{project_uuid}.stale.*"))


def test_forward_source_is_revalidated_inside_registry_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    late = (
        project
        / ".roundtable"
        / "inbox"
        / "claude"
        / "new"
        / "late-cas.md"
    )
    original = _rtmigrate._update_project_registry

    def inject_late_write(*args, **kwargs):
        late.write_bytes(b"arrived during registry wait\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        _rtmigrate,
        "_update_project_registry",
        inject_late_write,
    )
    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="local mailbox changed during cutover",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert late.read_bytes() == b"arrived during registry wait\n"
    monkeypatch.setattr(
        _rtmigrate,
        "_update_project_registry",
        original,
    )
    retried = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert retried["committed"] is True
    assert (
        central.inbox_dir / "claude" / "new" / "late-cas.md"
    ).read_bytes() == b"arrived during registry wait\n"


def test_rollback_source_is_revalidated_inside_registry_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    late = central.inbox_dir / "claude" / "new" / "late-cas.md"
    original = _rtmigrate._update_project_registry

    def inject_late_write(*args, **kwargs):
        late.write_bytes(b"arrived during rollback registry wait\n")
        return original(*args, **kwargs)

    monkeypatch.setattr(
        _rtmigrate,
        "_update_project_registry",
        inject_late_write,
    )
    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="central mailbox changed during cutover",
    ):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    assert late.read_bytes() == b"arrived during rollback registry wait\n"
    monkeypatch.setattr(
        _rtmigrate,
        "_update_project_registry",
        original,
    )
    retried = _rtmigrate.rollback_project(
        project,
        migrated["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert retried["committed"] is True
    assert (
        local.inbox_dir / "claude" / "new" / "late-cas.md"
    ).read_bytes() == b"arrived during rollback registry wait\n"


def test_stale_initial_layout_never_classifies_precommit_failure_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    actual = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "resolve_project_mailbox_checked",
        lambda *args, **kwargs: replace(actual, layout="central"),
    )
    unsafe = project / ".roundtable" / "inbox" / "unsafe.md"
    unsafe.write_bytes(b"unsafe mode\n")
    unsafe.chmod(0o666)

    with pytest.raises(_rtmigrate.MailMigrationError) as captured:
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert not isinstance(
        captured.value,
        _rtmigrate.MailMigrationCommittedError,
    )
    assert registry_layout(project, registry) == "local"


def test_completed_rollback_leaves_later_pre_cutover_candidate_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    first = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    rolled_back = _rtmigrate.rollback_project(
        project,
        first["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "after_central_publish",
    )
    with pytest.raises(_rtmigrate.InjectedMigrationFailure):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    candidate = _rtlib.central_mail_root(registry, project_uuid)
    candidate_inode = (candidate.stat().st_dev, candidate.stat().st_ino)
    marker_before = (
        candidate / _rtlib.CENTRAL_MAIL_MARKER_NAME
    ).read_bytes()

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    repeated = _rtmigrate.rollback_project(
        project,
        first["manifest"],
        registry_path=registry,
        backup_root=backup_root,
    )

    assert repeated["status"] == "already local"
    assert repeated["rollback_manifest"] == rolled_back["rollback_manifest"]
    assert repeated["warnings"]
    assert (candidate.stat().st_dev, candidate.stat().st_ino) == candidate_inode
    assert (
        candidate / _rtlib.CENTRAL_MAIL_MARKER_NAME
    ).read_bytes() == marker_before
    assert registry_layout(project, registry) == "local"


def test_forward_reports_unknown_when_commit_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original_flip = _rtmigrate._flip_layout

    def commit_then_lose_status(*args, **kwargs):
        original_flip(*args, **kwargs)
        raise OSError(errno.EIO, "post-replace status lost")

    monkeypatch.setattr(_rtmigrate, "_flip_layout", commit_then_lose_status)
    monkeypatch.setattr(
        _rtmigrate,
        "_current_layout",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _rtlib.ProjectRegistryError("registry unreadable")
        ),
    )

    with pytest.raises(
        _rtmigrate.MailMigrationCommitUnknownError,
        match="outcome is unknown",
    ) as captured:
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert "do not assume success" in str(captured.value)
    assert _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    ).layout == "central"


def test_rollback_reports_unknown_when_commit_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    original_flip = _rtmigrate._flip_layout

    def commit_then_lose_status(*args, **kwargs):
        original_flip(*args, **kwargs)
        raise OSError(errno.EIO, "post-replace status lost")

    monkeypatch.setattr(_rtmigrate, "_flip_layout", commit_then_lose_status)
    monkeypatch.setattr(
        _rtmigrate,
        "_current_layout",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _rtlib.ProjectRegistryError("registry unreadable")
        ),
    )

    with pytest.raises(
        _rtmigrate.MailMigrationCommitUnknownError,
        match="outcome is unknown",
    ) as captured:
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert "do not assume success" in str(captured.value)
    assert _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    ).layout == "local"


@pytest.mark.parametrize("tamper", ["direction", "central-root"])
def test_active_generation_rejects_self_consistent_manifest_tampering(
    tmp_path: Path,
    tamper: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    manifest = Path(migrated["manifest"])
    document = json.loads(manifest.read_text())
    if tamper == "direction":
        document["direction"] = "central-to-local"
        document["source_layout"] = "central"
        document["target_layout"] = "local"
    else:
        document["central_root"] = str(tmp_path / "wrong-central")
    payload = (
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    ).encode()
    manifest.write_bytes(payload)
    manifest.chmod(0o600)
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    marker_path = central.mail_root / _rtlib.CENTRAL_MAIL_MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["manifest_sha256"] = hashlib.sha256(payload).hexdigest()
    marker_path.write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n"
    )
    marker_path.chmod(0o600)

    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"


@pytest.mark.parametrize("field", ["operation_id", "snapshot_digest"])
def test_tampered_rollback_marker_never_retires_central_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "_TEST_FAILPOINT",
        "rollback_after_registry_flip",
    )
    with pytest.raises(_rtmigrate.MailMigrationCommittedError):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )
    marker_path = project / ".roundtable" / _rtmigrate.ROLLBACK_MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker[field] = (
        str(uuid_for_test()) if field == "operation_id" else "f" * 64
    )
    marker_path.write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n"
    )
    marker_path.chmod(0o600)
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    central = _rtlib.central_mail_root(registry, project_uuid)
    central_inode = (central.stat().st_dev, central.stat().st_ino)

    monkeypatch.setattr(_rtmigrate, "_TEST_FAILPOINT", None)
    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert (central.stat().st_dev, central.stat().st_ino) == central_inode
    assert registry_layout(project, registry) == "local"


def test_hold_projection_is_conservative_for_entries_and_large_bytes() -> None:
    many_entries = _rtmigrate.MailPreflight(
        files=844,
        directories=2,
        bytes=0,
    )
    huge_payload = _rtmigrate.MailPreflight(
        files=1,
        directories=2,
        bytes=700 * 1024 * 1024,
    )

    assert (
        many_entries.projected_seconds
        > _rtmigrate.MIGRATION_PREFLIGHT_BUDGET_SECONDS
    )
    assert (
        huge_payload.projected_seconds
        > _rtmigrate.MIGRATION_PREFLIGHT_BUDGET_SECONDS
    )


def test_large_hold_preflight_refuses_before_backup_or_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    oversized = _rtmigrate.MailPreflight(
        files=900,
        directories=2,
        bytes=0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "count_mail_tree",
        lambda _root, **_kwargs: oversized,
    )

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="--confirm-quiesced",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert not backup_root.exists()
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    assert not _rtlib.central_mail_root(registry, project_uuid).exists()


def test_exclusive_recheck_closes_preflight_growth_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    counts = iter(
        (
            _rtmigrate.MailPreflight(files=3, directories=8, bytes=64),
            _rtmigrate.MailPreflight(files=900, directories=2, bytes=64),
        )
    )
    monkeypatch.setattr(
        _rtmigrate,
        "count_mail_tree",
        lambda _root, **_kwargs: next(counts),
    )

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="hold preflight refused",
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert not backup_root.exists()


def test_confirmed_quiescence_allows_large_projected_hold_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    oversized = _rtmigrate.MailPreflight(
        files=900,
        directories=2,
        bytes=0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "count_mail_tree",
        lambda _root, **_kwargs: oversized,
    )

    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
        confirm_quiesced=True,
    )

    assert result["committed"] is True
    assert result["preflight_files"] == 900
    assert result["projected_exclusive_hold_ms"] > 5000
    assert any(
        "operator confirmed project quiescence" in warning
        for warning in result["warnings"]
    )


def test_large_rollback_preflight_refuses_before_backup_or_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    before_manifests = set(backup_root.rglob("manifest.json"))
    oversized = _rtmigrate.MailPreflight(
        files=900,
        directories=2,
        bytes=0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "count_mail_tree",
        lambda _root, **_kwargs: oversized,
    )

    with pytest.raises(
        _rtmigrate.MailMigrationError,
        match="--confirm-quiesced",
    ):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    assert set(backup_root.rglob("manifest.json")) == before_manifests
    assert not (project / ".roundtable" / "inbox").exists()


def test_confirmed_quiescence_allows_large_rollback_with_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    oversized = _rtmigrate.MailPreflight(
        files=900,
        directories=2,
        bytes=0,
    )
    monkeypatch.setattr(
        _rtmigrate,
        "count_mail_tree",
        lambda _root, **_kwargs: oversized,
    )

    result = _rtmigrate.rollback_project(
        project,
        migrated["manifest"],
        registry_path=registry,
        backup_root=backup_root,
        confirm_quiesced=True,
    )

    assert result["committed"] is True
    assert result["preflight_files"] == 900
    assert result["projected_exclusive_hold_ms"] > 5000
    assert result["registry_wait_cap_ms"] == pytest.approx(10000.0)
    assert any(
        "operator confirmed project quiescence" in warning
        for warning in result["warnings"]
    )
    assert registry_layout(project, registry) == "local"


def test_detached_forward_cleanup_cannot_delete_concurrent_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original_purge = _rtmigrate._purge_retired_local_mail
    observed = []

    def rollback_before_purge(quarantine, state_dir, metrics):
        central = _rtlib.resolve_project_mailbox_checked(
            project,
            registry_path=registry,
        )
        assert central.layout == "central"
        marker = _rtlib.validate_central_mail_marker(
            central.mail_root,
            central.project_uuid,
        )
        rolled_back = _rtmigrate.rollback_project(
            project,
            marker["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )
        observed.append(rolled_back["status"])
        return original_purge(quarantine, state_dir, metrics)

    monkeypatch.setattr(
        _rtmigrate,
        "_purge_retired_local_mail",
        rollback_before_purge,
    )
    _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    local = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert observed == ["rolled back"]
    assert local.layout == "local"
    assert_internal_hardlink(local.mail_root)
    assert not (project / ".roundtable" / "mail").exists()


def test_detached_cleanup_time_is_outside_exclusive_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original = _rtmigrate._purge_retired_local_mail

    def delayed_purge(*args, **kwargs):
        time.sleep(0.1)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        _rtmigrate,
        "_purge_retired_local_mail",
        delayed_purge,
    )
    started = time.monotonic()
    result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )
    elapsed_ms = (time.monotonic() - started) * 1000

    assert elapsed_ms - result["exclusive_hold_ms"] >= 90


def test_partial_detached_purge_is_preserved_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    original = _rtmigrate.shutil.rmtree
    interrupted = False

    def partial_rmtree(path, *args, **kwargs):
        nonlocal interrupted
        selected = Path(path)
        if (
            not interrupted
            and selected.name.startswith(".central-mail-retired.")
        ):
            interrupted = True
            original(selected / "inbox")
            raise OSError("simulated partial purge")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(_rtmigrate.shutil, "rmtree", partial_rmtree)
    migrated = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert migrated["committed"] is True
    assert any("could not purge detached" in item for item in migrated["warnings"])
    retired = list(
        (project / ".roundtable").glob(".central-mail-retired.*")
    )
    assert len(retired) == 1

    repaired = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=backup_root,
    )

    assert repaired["status"] == "already central"
    assert any("preserved incomplete" in item for item in repaired["warnings"])
    assert retired[0].is_dir()
    central = _rtlib.resolve_project_mailbox_checked(
        project,
        registry_path=registry,
    )
    assert central.layout == "central"
    assert_internal_hardlink(central.mail_root)


def test_backup_root_defaults_to_registry_and_honors_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, _backup_root = write_registered_project(
        tmp_path / "default"
    )
    default_result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
    )
    default_archive = Path(
        json.loads(Path(default_result["manifest"]).read_text())[
            "archive_manifest"
        ]
    )
    assert default_archive.is_relative_to(
        registry.parent / "backups" / "roundtable-central-mail"
    )

    configured = (tmp_path / "configured-backups").resolve()
    explicit = (tmp_path / "explicit-backups").resolve()
    monkeypatch.setenv(_rtmigrate.MAIL_BACKUP_ENV, str(configured))
    project, registry, _backup_root = write_registered_project(
        tmp_path / "explicit"
    )
    explicit_result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
        backup_root=explicit,
    )
    explicit_archive = Path(
        json.loads(Path(explicit_result["manifest"]).read_text())[
            "archive_manifest"
        ]
    )
    assert explicit_archive.is_relative_to(explicit)
    assert not configured.exists()

    project, registry, _backup_root = write_registered_project(
        tmp_path / "environment"
    )
    environment_result = _rtmigrate.migrate_project(
        project,
        registry_path=registry,
    )
    environment_archive = Path(
        json.loads(Path(environment_result["manifest"]).read_text())[
            "archive_manifest"
        ]
    )
    assert environment_archive.is_relative_to(configured)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("path", "/different/project", "project path changed before cutover"),
        ("layout", "central", "project layout changed before cutover"),
    ],
)
def test_flip_layout_rejects_registry_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
    message: str,
) -> None:
    project, registry, _backup_root = write_registered_project(tmp_path)
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    document, _payload = _rtlib._read_projects_snapshot(registry)
    document = json.loads(json.dumps(document))
    document["projects"][0][field] = value

    def inject_document(mutator, *_args, **_kwargs):
        return mutator(document, b"", None)

    monkeypatch.setattr(
        _rtmigrate,
        "_update_project_registry",
        inject_document,
    )

    with pytest.raises(_rtmigrate.MailMigrationError, match=message):
        _rtmigrate._flip_layout(
            registry,
            project_uuid,
            project,
            "local",
            "central",
            guard=lambda: None,
            lock_timeout=1.0,
            metrics=_rtmigrate.MigrationMetrics(),
        )


def test_rt_projects_cli_exposes_only_project_scoped_migration(
    tmp_path: Path,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "rt-projects"),
            "--registry",
            str(registry),
            "migrate",
            str(project),
            "--backup-dir",
            str(backup_root),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "migrated:" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["operation"] == "migrate"
    assert payload["committed"] is True
    rollback = subprocess.run(
        [
            sys.executable,
            str(BIN / "rt-projects"),
            "--registry",
            str(registry),
            "rollback",
            str(project),
            "--manifest",
            payload["manifest"],
            "--backup-dir",
            str(backup_root),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert rollback.returncode == 0, rollback.stderr
    assert "rolled back:" in rollback.stderr
    assert json.loads(rollback.stdout)["operation"] == "rollback"
    help_result = subprocess.run(
        [sys.executable, str(BIN / "rt-projects"), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert "{list,upgrade,add,resolve,rm,migrate,rollback}" in help_result.stdout
    migrate_help = subprocess.run(
        [sys.executable, str(BIN / "rt-projects"), "migrate", "--help"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert "--confirm-quiesced" in migrate_help.stdout
    assert "capped at five seconds" in " ".join(
        migrate_help.stdout.split()
    )
    rollback_help = subprocess.run(
        [
            sys.executable,
            str(BIN / "rt-projects"),
            "rollback",
            "--help",
        ],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert "--confirm-quiesced" in rollback_help.stdout
    assert "remaining five-second hold budget" in " ".join(
        rollback_help.stdout.split()
    )


def test_rt_projects_cli_reports_registry_errors_without_traceback(
    tmp_path: Path,
) -> None:
    project = tmp_path / "unregistered"
    project.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            str(BIN / "rt-projects"),
            "--registry",
            str(tmp_path / "missing-registry.yaml"),
            "migrate",
            str(project),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode != 0
    assert "rt-projects: migrate failed:" in result.stderr
    assert "Traceback" not in result.stderr
