from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
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


def test_pre_cutover_published_candidate_is_rebuilt_after_local_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry, backup_root = write_registered_project(tmp_path)
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", "after_central_publish")

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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", "after_registry_flip")

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
    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.InjectedMigrationFailure, match=phase):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    assert_internal_hardlink(project / ".roundtable")
    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.MailMigrationCommittedError, match="committed"):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", "after_registry_flip")
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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv(
        "RT_MIGRATION_FAILPOINT",
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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv(
        "RT_MIGRATION_FAILPOINT",
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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.InjectedMigrationFailure, match=phase):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "central"
    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", phase)

    with pytest.raises(_rtmigrate.MailMigrationCommittedError, match="committed"):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert registry_layout(project, registry) == "local"
    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    locations = {
        "local-state": project / ".roundtable" / "locks" / "backup",
        "forward-bundle": manifest.parent / "payload" / "inbox" / "backup",
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", "after_central_publish")
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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    monkeypatch.setenv("RT_MIGRATION_FAILPOINT", "after_central_publish")
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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
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
    ):
        _rtmigrate.migrate_project(
            project,
            registry_path=registry,
            backup_root=backup_root,
        )

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
    ):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

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
    monkeypatch.setenv(
        "RT_MIGRATION_FAILPOINT",
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

    monkeypatch.delenv("RT_MIGRATION_FAILPOINT")
    with pytest.raises(_rtmigrate.MailMigrationError):
        _rtmigrate.rollback_project(
            project,
            migrated["manifest"],
            registry_path=registry,
            backup_root=backup_root,
        )

    assert (central.stat().st_dev, central.stat().st_ino) == central_inode
    assert registry_layout(project, registry) == "local"


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
    assert json.loads(rollback.stdout)["operation"] == "rollback"
    help_result = subprocess.run(
        [sys.executable, str(BIN / "rt-projects"), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    assert "{list,upgrade,add,resolve,rm,migrate,rollback}" in help_result.stdout


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
