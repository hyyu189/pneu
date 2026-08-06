from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import stat
import sys
import threading
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlib  # noqa: E402


def write_registered_project(tmp_path: Path) -> tuple[Path, Path]:
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
    (state / "inbox").mkdir()
    (state / "messages").mkdir()
    (state / "locks").mkdir()
    registry = tmp_path / "registry" / "projects.yaml"
    assert _rtlib.register_project(project, path=registry)
    return project.resolve(), registry


def flip_to_central(project: Path, registry: Path) -> None:
    mailbox = _rtlib.resolve_project_mailbox_checked(
        project, registry_path=registry
    )
    central = registry.parent / "mail" / mailbox.project_uuid
    for directory in (
        central,
        central / "inbox",
        central / "messages",
        central / "locks",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    (central / _rtlib.CENTRAL_MAIL_MARKER_NAME).write_text(
        json.dumps(
            {
                "schema": _rtlib.CENTRAL_MAIL_MARKER_SCHEMA,
                "project_uuid": mailbox.project_uuid,
                "operation_id": "00000000-0000-4000-8000-000000000001",
                "manifest": str(registry.parent / "layout-test-manifest.json"),
                "manifest_sha256": "1" * 64,
                "snapshot_digest": "2" * 64,
            }
        )
        + "\n"
    )

    def mutate(document, _source_payload, _parent_fd):
        for entry in document["projects"]:
            if entry.get("uuid") == mailbox.project_uuid:
                entry["layout"] = "central"
                return True
        raise AssertionError("registered UUID disappeared")

    assert _rtlib._update_project_registry(mutate, registry)


def test_shared_and_exclusive_layout_locks_use_one_persistent_private_inode(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)

    with _rtlib.locked_project_mailbox_checked(
        project, registry_path=registry
    ) as first:
        assert first.layout == "local"
        info = first.layout_lock.stat()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 1
        assert stat.S_IMODE(first.layout_lock.parent.stat().st_mode) == 0o700
        first_inode = (info.st_dev, info.st_ino)
        gate_path = first.layout_lock.with_name(
            f"{first.project_uuid}.writer.lock"
        )
        gate_info = gate_path.stat()
        assert stat.S_ISREG(gate_info.st_mode)
        assert stat.S_IMODE(gate_info.st_mode) == 0o600
        assert gate_info.st_nlink == 1
        with _rtlib.locked_project_mailbox_checked(
            project, registry_path=registry
        ) as second:
            assert second == first

    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        exclusive=True,
    ) as exclusive:
        assert exclusive.layout_lock == first.layout_lock
        assert (exclusive.layout_lock.stat().st_dev, exclusive.layout_lock.stat().st_ino) == (
            first_inode
        )


@pytest.mark.parametrize(
    ("held_exclusive", "waiting_exclusive"),
    [(False, True), (True, False)],
)
def test_layout_lock_modes_exclude_conflicting_access(
    tmp_path: Path,
    held_exclusive: bool,
    waiting_exclusive: bool,
) -> None:
    project, registry = write_registered_project(tmp_path)

    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        exclusive=held_exclusive,
    ):
        started = time.monotonic()
        with pytest.raises(
            _rtlib.ProjectRegistryError,
            match="timed out waiting",
        ):
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                exclusive=waiting_exclusive,
                timeout=0.05,
            ):
                raise AssertionError("conflicting lock unexpectedly acquired")
        assert time.monotonic() - started < 1


def test_waiting_writer_turnstile_blocks_later_readers(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    writer_acquired = threading.Event()
    release_writer = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                exclusive=True,
                timeout=2,
            ):
                writer_acquired.set()
                assert release_writer.wait(2)
        except BaseException as error:  # pragma: no cover - diagnostic path
            errors.append(error)

    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
    ) as mailbox:
        worker = threading.Thread(target=writer)
        worker.start()
        gate_path = mailbox.layout_lock.with_name(
            f"{mailbox.project_uuid}.writer.lock"
        )
        deadline = time.monotonic() + 1
        while True:
            gate = os.open(gate_path, os.O_RDWR | os.O_CLOEXEC)
            try:
                try:
                    fcntl.flock(
                        gate,
                        fcntl.LOCK_SH | fcntl.LOCK_NB,
                    )
                except BlockingIOError:
                    break
            finally:
                os.close(gate)
            if time.monotonic() >= deadline:
                pytest.fail("exclusive waiter never closed the writer gate")
            time.sleep(0.01)

        with pytest.raises(_rtlib.ProjectLayoutLockTimeout):
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                timeout=0.05,
            ):
                raise AssertionError("later reader overtook waiting writer")
        assert not writer_acquired.is_set()

    assert writer_acquired.wait(1)
    release_writer.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert errors == []


def test_gate_and_resource_share_one_acquisition_deadline(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
    ) as mailbox:
        resource_path = mailbox.layout_lock
        gate_path = resource_path.with_name(
            f"{mailbox.project_uuid}.writer.lock"
        )

    gate = os.open(gate_path, os.O_RDWR | os.O_CLOEXEC)
    resource = os.open(resource_path, os.O_RDWR | os.O_CLOEXEC)
    fcntl.flock(gate, fcntl.LOCK_EX)
    fcntl.flock(resource, fcntl.LOCK_EX)
    errors: list[BaseException] = []

    def waiter() -> None:
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                timeout=0.25,
            ):
                raise AssertionError("split deadline unexpectedly reset")
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=waiter)
    started = time.monotonic()
    worker.start()
    time.sleep(0.15)
    fcntl.flock(gate, fcntl.LOCK_UN)
    time.sleep(0.15)
    fcntl.flock(resource, fcntl.LOCK_UN)
    os.close(gate)
    os.close(resource)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], _rtlib.ProjectLayoutLockTimeout)
    assert 0.20 <= time.monotonic() - started < 1


def test_layout_holder_resolves_move_without_registry_reindex(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    moved = project.with_name("moved")
    project.rename(moved)
    moved = moved.resolve()
    before = registry.read_bytes()

    with _rtlib.locked_project_mailbox_checked(
        moved,
        registry_path=registry,
    ) as mailbox:
        assert mailbox.project_root == moved
        assert registry.read_bytes() == before

    direct = _rtlib.resolve_project_mailbox_checked(
        moved,
        registry_path=registry,
    )
    assert direct.project_root == moved
    assert registry.read_bytes() != before


def test_waiting_reader_resolves_layout_only_after_exclusive_flip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = write_registered_project(tmp_path)
    started = threading.Event()
    resolver_entered = threading.Event()
    finished = threading.Event()
    observed: list[tuple[str, Path]] = []
    errors: list[BaseException] = []
    original_resolver = _rtlib.resolve_project_mailbox_checked

    def watched_resolver(*args, **kwargs):
        if threading.current_thread().name == "layout-reader":
            resolver_entered.set()
        return original_resolver(*args, **kwargs)

    monkeypatch.setattr(
        _rtlib, "resolve_project_mailbox_checked", watched_resolver
    )

    def reader() -> None:
        started.set()
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                timeout=2,
            ) as mailbox:
                observed.append((mailbox.layout, mailbox.mail_root))
        except BaseException as error:  # pragma: no cover - diagnostic path
            errors.append(error)
        finally:
            finished.set()

    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        exclusive=True,
    ) as local:
        worker = threading.Thread(target=reader, name="layout-reader")
        worker.start()
        assert started.wait(1)
        time.sleep(0.1)
        assert not finished.is_set()
        assert not resolver_entered.is_set()
        flip_to_central(project, registry)
        central_root = registry.parent / "mail" / local.project_uuid

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert resolver_entered.is_set()
    assert errors == []
    assert observed == [("central", central_root)]


@pytest.mark.parametrize("bad_kind", ["symlink-dir", "fifo", "directory"])
def test_layout_lock_rejects_substituted_nonregular_entries(
    tmp_path: Path,
    bad_kind: str,
) -> None:
    project, registry = write_registered_project(tmp_path)
    project_uuid = json.loads(
        _rtlib.project_identity_path(project).read_text()
    )["uuid"]
    lock_dir = registry.parent / "layout-locks"
    lock_path = lock_dir / f"{project_uuid}.lock"
    if bad_kind == "symlink-dir":
        target = tmp_path / "elsewhere"
        target.mkdir()
        lock_dir.symlink_to(target, target_is_directory=True)
    else:
        lock_dir.mkdir(mode=0o700)
        lock_dir.chmod(0o700)
        if bad_kind == "fifo":
            os.mkfifo(lock_path, 0o600)
        else:
            lock_path.mkdir(mode=0o700)

    started = time.monotonic()
    with pytest.raises(_rtlib.ProjectRegistryError):
        with _rtlib.locked_project_mailbox_checked(
            project,
            registry_path=registry,
            timeout=0.05,
        ):
            raise AssertionError("unsafe lock entry unexpectedly accepted")
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "bad_target",
    [
        "directory-mode",
        "file-mode",
        "hard-link",
        "gate-file-mode",
        "gate-hard-link",
    ],
)
def test_layout_lock_rejects_nonprivate_or_linked_namespace(
    tmp_path: Path,
    bad_target: str,
) -> None:
    project, registry = write_registered_project(tmp_path)
    with _rtlib.locked_project_mailbox_checked(
        project, registry_path=registry
    ) as mailbox:
        lock_path = mailbox.layout_lock
        gate_path = lock_path.with_name(
            f"{mailbox.project_uuid}.writer.lock"
        )
    if bad_target == "directory-mode":
        lock_path.parent.chmod(0o755)
    elif bad_target == "file-mode":
        lock_path.chmod(0o644)
    elif bad_target == "hard-link":
        os.link(lock_path, lock_path.with_suffix(".alias"))
    elif bad_target == "gate-file-mode":
        gate_path.chmod(0o644)
    else:
        os.link(gate_path, gate_path.with_suffix(".alias"))

    with pytest.raises(_rtlib.ProjectRegistryError):
        with _rtlib.locked_project_mailbox_checked(
            project, registry_path=registry, timeout=0.05
        ):
            raise AssertionError("unsafe lock namespace unexpectedly accepted")


def test_layout_lock_descriptor_is_close_on_exec(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    guard = _rtlib.locked_project_mailbox_checked(
        project, registry_path=registry
    )
    with guard:
        assert (
            guard.gate_acquired_at_monotonic
            <= guard.acquired_at_monotonic
            <= time.monotonic()
        )
        flags = fcntl.fcntl(guard._descriptor, fcntl.F_GETFD)
        assert flags & fcntl.FD_CLOEXEC
        gate_flags = fcntl.fcntl(
            guard._gate_descriptor,
            fcntl.F_GETFD,
        )
        assert gate_flags & fcntl.FD_CLOEXEC


def test_layout_lock_rejects_free_lock_after_positive_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = write_registered_project(tmp_path)
    original = _rtlib._acquire_flock_before
    calls = 0

    def delayed_attempt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            time.sleep(0.04)
        return original(*args, **kwargs)

    monkeypatch.setattr(
        _rtlib,
        "_acquire_flock_before",
        delayed_attempt,
    )

    with pytest.raises(_rtlib.ProjectLayoutLockTimeout):
        with _rtlib.locked_project_mailbox_checked(
            project,
            registry_path=registry,
            exclusive=True,
            timeout=0.02,
        ):
            raise AssertionError("expired free layout lock was admitted")


def test_layout_lock_rejects_post_flock_deadline_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, registry = write_registered_project(tmp_path)
    original = _rtlib._acquire_flock_before
    calls = 0

    def delayed_return(*args, **kwargs):
        nonlocal calls
        acquired_at = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            time.sleep(0.04)
        return acquired_at

    monkeypatch.setattr(
        _rtlib,
        "_acquire_flock_before",
        delayed_return,
    )

    with pytest.raises(_rtlib.ProjectLayoutLockTimeout):
        with _rtlib.locked_project_mailbox_checked(
            project,
            registry_path=registry,
            exclusive=True,
            timeout=0.02,
        ):
            raise AssertionError("post-flock deadline gap was admitted")

    monkeypatch.setattr(
        _rtlib,
        "_acquire_flock_before",
        original,
    )
    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        timeout=0.2,
    ):
        pass


def test_zero_timeout_keeps_one_free_lock_attempt(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)

    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        timeout=0,
    ):
        pass


def test_registry_lock_wait_is_bounded_inside_exclusive_layout_section(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    lock_path = registry.with_name(f"{registry.name}.lock")
    held = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    fcntl.flock(held, fcntl.LOCK_EX)

    def no_change(_document, _payload, _parent_fd):
        return False

    started = time.monotonic()
    try:
        with pytest.raises(
            _rtlib.ProjectRegistryLockTimeout,
            match="timed out waiting for project registry lock",
        ) as captured:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                exclusive=True,
                timeout=1,
            ):
                _rtlib._update_project_registry(
                    no_change,
                    registry,
                    lock_timeout=0.05,
                )
        assert "another Roundtable seat may hold" in str(captured.value)
        assert "For rt-projects migrate/rollback" in str(captured.value)
        assert time.monotonic() - started < 1
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)

    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        timeout=0.5,
    ):
        pass


def test_registry_lock_absolute_deadline_is_not_rebased(
    tmp_path: Path,
) -> None:
    _project, registry = write_registered_project(tmp_path)
    lock_path = registry.with_name(f"{registry.name}.lock")
    held = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    fcntl.flock(held, fcntl.LOCK_EX)

    def no_change(_document, _payload, _parent_fd):
        return False

    try:
        expired = time.monotonic() - 0.01
        started = time.monotonic()
        with pytest.raises(_rtlib.ProjectRegistryLockTimeout):
            _rtlib._update_project_registry(
                no_change,
                registry,
                lock_timeout=1.0,
                lock_deadline=expired,
            )
        assert time.monotonic() - started < 0.1
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


def test_expired_registry_deadline_does_not_mutate_when_lock_is_free(
    tmp_path: Path,
) -> None:
    _project, registry = write_registered_project(tmp_path)
    called = False

    def unexpected_mutation(_document, _payload, _parent_fd):
        nonlocal called
        called = True
        return True

    before = registry.read_bytes()
    with pytest.raises(_rtlib.ProjectRegistryLockTimeout):
        _rtlib._update_project_registry(
            unexpected_mutation,
            registry,
            lock_timeout=1.0,
            lock_deadline=time.monotonic() - 0.01,
        )

    assert called is False
    assert registry.read_bytes() == before


def test_zero_registry_timeout_tries_once_without_waiting(
    tmp_path: Path,
) -> None:
    _project, registry = write_registered_project(tmp_path)

    def no_change(_document, _payload, _parent_fd):
        return False

    assert (
        _rtlib._update_project_registry(
            no_change,
            registry,
            lock_timeout=0,
        )
        is False
    )

    lock_path = registry.with_name(f"{registry.name}.lock")
    held = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    fcntl.flock(held, fcntl.LOCK_EX)
    started = time.monotonic()
    try:
        with pytest.raises(_rtlib.ProjectRegistryLockTimeout):
            _rtlib._update_project_registry(
                no_change,
                registry,
                lock_timeout=0,
            )
        assert time.monotonic() - started < 0.1
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


@pytest.mark.parametrize(
    "timeout",
    [
        True,
        False,
        "1",
        -1,
        pytest.param(10**10000, id="huge-int"),
        float("nan"),
        float("inf"),
    ],
)
def test_layout_lock_rejects_invalid_timeout(
    tmp_path: Path,
    timeout: object,
) -> None:
    project, registry = write_registered_project(tmp_path)

    with pytest.raises(
        _rtlib.ProjectRegistryError,
        match="finite non-negative",
    ):
        _rtlib.locked_project_mailbox_checked(
            project,
            registry_path=registry,
            timeout=timeout,
        )


def test_layout_lock_rejects_unsafe_registry_parent(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    registry.parent.chmod(0o777)

    with pytest.raises(
        _rtlib.ProjectRegistryError,
        match="group/other writable",
    ):
        with _rtlib.locked_project_mailbox_checked(
            project,
            registry_path=registry,
        ):
            raise AssertionError("unsafe registry parent was accepted")


def test_sigkill_releases_kernel_lock_without_replacing_inode(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    script = """
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import _rtlib
with _rtlib.locked_project_mailbox_checked(
    Path(sys.argv[2]),
    registry_path=Path(sys.argv[3]),
    exclusive=True,
    timeout=2,
) as mailbox:
    print(mailbox.layout_lock, flush=True)
    time.sleep(60)
"""
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(BIN),
            str(project),
            str(registry),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        lock_path = Path(child.stdout.readline().strip())
        assert lock_path.is_file()
        before = lock_path.stat()
        child.kill()
        child.wait(timeout=5)

        with _rtlib.locked_project_mailbox_checked(
            project,
            registry_path=registry,
            exclusive=True,
            timeout=2,
        ) as mailbox:
            after = mailbox.layout_lock.stat()
            assert mailbox.layout_lock == lock_path
            assert (after.st_dev, after.st_ino) == (
                before.st_dev,
                before.st_ino,
            )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_waiter_rejects_lock_inode_replaced_while_contended(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    with _rtlib.locked_project_mailbox_checked(
        project, registry_path=registry
    ) as mailbox:
        lock_path = mailbox.layout_lock

    held = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    fcntl.flock(held, fcntl.LOCK_EX)
    started = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def acquire_old_inode() -> None:
        started.set()
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                timeout=2,
            ):
                raise AssertionError("replaced lock inode was accepted")
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=acquire_old_inode)
    worker.start()
    assert started.wait(1)
    time.sleep(0.1)
    assert not finished.is_set()
    replacement = lock_path.with_suffix(".replacement")
    descriptor = os.open(
        replacement,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    os.close(descriptor)
    os.replace(replacement, lock_path)
    fcntl.flock(held, fcntl.LOCK_UN)
    os.close(held)

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], _rtlib.ProjectRegistryError)
    assert "changed during acquisition" in str(errors[0])


def test_waiter_rejects_lock_directory_replaced_while_contended(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    with _rtlib.locked_project_mailbox_checked(
        project, registry_path=registry
    ) as mailbox:
        lock_path = mailbox.layout_lock

    held = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    fcntl.flock(held, fcntl.LOCK_EX)
    started = threading.Event()
    errors: list[BaseException] = []

    def acquire_detached_directory() -> None:
        started.set()
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                timeout=2,
            ):
                raise AssertionError("detached lock directory was accepted")
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=acquire_detached_directory)
    worker.start()
    assert started.wait(1)
    time.sleep(0.1)
    detached = lock_path.parent.with_name("layout-locks.detached")
    lock_path.parent.rename(detached)
    lock_path.parent.mkdir(mode=0o700)
    replacement = lock_path
    descriptor = os.open(
        replacement,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
        0o600,
    )
    os.close(descriptor)
    fcntl.flock(held, fcntl.LOCK_UN)
    os.close(held)

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], _rtlib.ProjectRegistryError)
    assert "directory changed during acquisition" in str(errors[0])


@pytest.mark.parametrize("target", ["lock-directory", "registry-parent"])
def test_waiter_revalidates_directory_modes_after_contention(
    tmp_path: Path,
    target: str,
) -> None:
    project, registry = write_registered_project(tmp_path)
    with _rtlib.locked_project_mailbox_checked(
        project, registry_path=registry
    ) as mailbox:
        lock_path = mailbox.layout_lock

    held = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    fcntl.flock(held, fcntl.LOCK_EX)
    started = threading.Event()
    errors: list[BaseException] = []

    def acquire_after_chmod() -> None:
        started.set()
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                timeout=2,
            ):
                raise AssertionError("unsafe directory mode was accepted")
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=acquire_after_chmod)
    worker.start()
    assert started.wait(1)
    time.sleep(0.1)
    changed = lock_path.parent if target == "lock-directory" else registry.parent
    changed.chmod(0o777)
    fcntl.flock(held, fcntl.LOCK_UN)
    os.close(held)

    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], _rtlib.ProjectRegistryError)
    assert "writable" in str(errors[0]) or "mode must be 0700" in str(
        errors[0]
    )


def test_rt_say_waits_for_cutover_and_writes_only_new_layout(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {
            "RT_PROJECTS_FILE": str(registry),
            "RT_FROM": "codex",
            "CODEX_THREAD_ID": "",
            "CMUX_SURFACE_ID": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        exclusive=True,
    ) as local:
        process = subprocess.Popen(
            [
                sys.executable,
                str(BIN / "rt-say"),
                "claude",
                "question",
                "after cutover",
            ],
            cwd=project,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(0.15)
        assert process.poll() is None
        assert not (local.inbox_dir / "claude" / "new").exists()
        flip_to_central(project, registry)
        central_root = registry.parent / "mail" / local.project_uuid

    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "sent maildir-only" in stdout
    delivered = list(
        (central_root / "inbox" / "claude" / "new").glob("*.md")
    )
    assert len(delivered) == 1
    assert "after cutover" in delivered[0].read_text()
    assert not (project / ".roundtable" / "inbox" / "claude").exists()


def test_rt_ack_allows_queued_cutover_between_delivery_and_archive(
    tmp_path: Path,
) -> None:
    project, registry = write_registered_project(tmp_path)
    state = project / ".roundtable"
    incoming_new = state / "inbox" / "claude" / "new"
    incoming_new.mkdir(parents=True)
    msg_id = "20260729T120000Z-codex-to-claude-layout"
    incoming = incoming_new / f"{msg_id}.md"
    incoming.write_text(
        f"[CODEX→CLAUDE question id={msg_id}] hold the boundary\n"
    )
    ledger_lock = state / "locks" / "ledger.lock"
    ledger_lock.mkdir()
    environment = os.environ.copy()
    environment.update(
        {
            "RT_PROJECTS_FILE": str(registry),
            "RT_FROM": "",
            "CODEX_THREAD_ID": "",
            "CMUX_SURFACE_ID": "",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    process = subprocess.Popen(
        [sys.executable, str(BIN / "rt-ack"), msg_id],
        cwd=project,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ack_dir = state / "inbox" / "codex" / "new"
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if list(ack_dir.glob("ack-*.md")):
            break
        time.sleep(0.02)
    else:
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        pytest.fail(f"rt-ack did not publish before timeout: {stdout} {stderr}")

    writer_acquired = threading.Event()
    release_writer = threading.Event()
    writer_errors: list[BaseException] = []
    central_root: list[Path] = []

    def migrate_between_ack_phases() -> None:
        try:
            with _rtlib.locked_project_mailbox_checked(
                project,
                registry_path=registry,
                exclusive=True,
                timeout=3,
            ) as local:
                central = registry.parent / "mail" / local.project_uuid
                central.mkdir(parents=True, mode=0o700)
                for name in ("inbox", "messages"):
                    shutil.copytree(
                        local.mail_root / name,
                        central / name,
                    )
                (central / "locks").mkdir(mode=0o700)
                (central / _rtlib.CENTRAL_MAIL_MARKER_NAME).write_text(
                    json.dumps(
                        {
                            "schema": _rtlib.CENTRAL_MAIL_MARKER_SCHEMA,
                            "project_uuid": local.project_uuid,
                            "operation_id": (
                                "00000000-0000-4000-8000-000000000002"
                            ),
                            "manifest": str(
                                registry.parent / "ack-test-manifest.json"
                            ),
                            "manifest_sha256": "1" * 64,
                            "snapshot_digest": "2" * 64,
                        }
                    )
                    + "\n"
                )

                def mutate(document, _source_payload, _parent_fd):
                    for entry in document["projects"]:
                        if entry.get("uuid") == local.project_uuid:
                            entry["layout"] = "central"
                            return True
                    raise AssertionError("registered UUID disappeared")

                assert _rtlib._update_project_registry(mutate, registry)
                for name in ("inbox", "messages", "locks"):
                    shutil.rmtree(local.mail_root / name)
                central_root.append(central)
                writer_acquired.set()
                assert release_writer.wait(3)
        except BaseException as error:  # pragma: no cover - diagnostic path
            writer_errors.append(error)

    writer = threading.Thread(target=migrate_between_ack_phases)
    writer.start()
    gate_path = (
        registry.parent
        / "layout-locks"
        / (
            json.loads(
                _rtlib.project_identity_path(project).read_text()
            )["uuid"]
            + ".writer.lock"
        )
    )
    deadline = time.monotonic() + 2
    while True:
        gate = os.open(gate_path, os.O_RDWR | os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(gate, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                break
        finally:
            os.close(gate)
        if time.monotonic() >= deadline:
            pytest.fail("migration never queued behind acknowledgement send")
        time.sleep(0.01)

    # Let the rt-say child finish its ledger append. The queued writer owns
    # admission, so it cuts over before rt-ack's fresh archive section.
    ledger_lock.rmdir()
    assert writer_acquired.wait(3)
    assert process.poll() is None
    assert incoming.is_file() is False
    assert len(central_root) == 1
    central = central_root[0]
    copied_incoming = (
        central
        / "inbox"
        / "claude"
        / "new"
        / incoming.name
    )
    assert copied_incoming.is_file()
    copied_acks = list(
        (central / "inbox" / "codex" / "new").glob("ack-*.md")
    )
    assert len(copied_acks) == 1

    release_writer.set()
    writer.join(timeout=3)
    assert not writer.is_alive()
    assert writer_errors == []
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert "sent maildir-only" in stdout
    assert not copied_incoming.exists()
    assert (
        central
        / "inbox"
        / "claude"
        / "cur"
        / incoming.name
    ).is_file()

    # Restoring the old parent-wide shared section would deadlock behind the
    # writer turnstile and make this interleaving impossible.
    with _rtlib.locked_project_mailbox_checked(
        project,
        registry_path=registry,
        exclusive=True,
        timeout=0.5,
    ):
        pass
