from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtruntime


@pytest.fixture
def runtime(tmp_path, monkeypatch) -> Path:
    selected = tmp_path / "host-runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(selected))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(selected))
    return selected


@pytest.fixture
def process_table(monkeypatch):
    starts: dict[int, str | Exception | None] = {
        101: "start-owner-101",
        102: "start-owner-102",
        201: "start-watcher-201",
        202: "start-watcher-202",
    }

    def inspect(pid: int) -> str | None:
        value = starts.get(pid)
        if isinstance(value, Exception):
            raise value
        return value

    def pid_state(pid: int) -> str:
        value = starts.get(pid)
        if isinstance(value, Exception):
            return "ambiguous"
        return "dead" if value is None else "live"

    monkeypatch.setattr(_rtruntime, "process_start_fingerprint", inspect)
    monkeypatch.setattr(_rtruntime, "_pid_state", pid_state)
    return starts


def project(tmp_path, name: str = "project") -> Path:
    selected = tmp_path / name
    selected.mkdir()
    return selected.resolve()


def test_relative_runtime_root_fails_closed_across_cwd_changes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RT_RUNTIME_DIR", ".runtime")

    with pytest.raises(_rtruntime.RuntimeStateError, match="absolute path"):
        _rtruntime.runtime_root()


def test_conflicting_runtime_environment_roots_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "generic"))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(tmp_path / "legacy"))

    with pytest.raises(_rtruntime.RuntimeStateError, match="different runtime roots"):
        _rtruntime.runtime_root()


def test_ps_process_fingerprint_uses_absolute_binary_and_stable_locale(
    monkeypatch,
):
    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="Sun Jul 19 18:55:36 2026\n")

    monkeypatch.setattr(_rtruntime.subprocess, "run", run)
    monkeypatch.setattr(
        _rtruntime.Path,
        "read_text",
        lambda _path: (_ for _ in ()).throw(OSError("no procfs")),
    )
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")

    fingerprint = _rtruntime.process_start_fingerprint(os.getpid())

    assert fingerprint == "ps:Sun Jul 19 18:55:36 2026"
    assert observed["command"][0] == "/bin/ps"
    assert observed["environment"]["LC_ALL"] == "C"
    assert observed["environment"]["LANG"] == "C"


def test_runtime_path_hashes_canonical_project_and_enforces_private_modes(
    tmp_path, runtime, process_table
):
    real = project(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    token = _rtruntime.claim(alias, "claude/review", "claude", owner_pid=101)
    paths = _rtruntime.seat_paths(real, "claude/review")

    assert token.project_root == real
    assert token.project_hash == _rtruntime.project_hash(alias)
    assert paths.runtime_root == runtime
    assert paths.lease.is_file()
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.project_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.lease.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.state_lock.stat().st_mode) == 0o600


def test_canonical_project_wraps_symlink_loop_as_runtime_error(tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to("loop", target_is_directory=True)

    with pytest.raises(_rtruntime.RuntimeStateError, match="cannot resolve project root"):
        _rtruntime.canonical_project(loop / "project")


def test_claim_is_active_unhealthy_until_wake_heartbeat(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)

    token = _rtruntime.claim(root, "codex", "codex", owner_pid=101)
    starting = _rtruntime.inspect_seat(root, "codex")
    ready = _rtruntime.update_wake(
        root,
        "codex",
        token.session_id,
        token.revision,
        native_session_id="thread-1",
    )
    healthy = _rtruntime.inspect_seat(root, "codex")

    assert starting.status == "active_unhealthy"
    assert not starting.wake_healthy
    assert ready.native_session_id == "thread-1"
    assert healthy.status == "active_healthy"
    assert healthy.token == ready


def test_active_same_harness_blocks_other_agent_but_other_harness_is_allowed(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    first = _rtruntime.claim(root, "claude-build", "claude", owner_pid=101)

    with pytest.raises(_rtruntime.SeatOccupied) as captured:
        _rtruntime.claim(root, "claude-review", "claude", owner_pid=102)

    assert captured.value.inspection.token == first
    assert captured.value.inspection.status == "active_unhealthy"
    hermes = _rtruntime.claim(root, "hermes", "hermes", owner_pid=102)
    assert hermes.harness == "hermes"


def test_host_harness_scan_finds_unregistered_projects_and_filters_harness(
    tmp_path, runtime, process_table
):
    first = project(tmp_path, "first")
    second = project(tmp_path, "second")
    codex = _rtruntime.claim(first, "codex-build", "codex", owner_pid=101)
    _rtruntime.claim(second, "claude", "claude", owner_pid=102)

    inspections = _rtruntime.inspect_host_harness_seats("codex")

    assert len(inspections) == 1
    assert inspections[0].status == "active_unhealthy"
    assert inspections[0].token == codex
    assert stat.S_IMODE((runtime / "projects").stat().st_mode) == 0o700


def test_host_harness_scan_rejects_unsafe_runtime_project_container(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    _rtruntime.claim(root, "codex", "codex", owner_pid=101)
    (runtime / "projects").chmod(0o755)

    with pytest.raises(
        _rtruntime.RuntimeStateError,
        match="exposes group/other permissions",
    ):
        _rtruntime.inspect_host_harness_seats("codex")


def test_stale_owner_is_reclaimed_fresh_with_incremented_revision(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    old = _rtruntime.claim(
        root, "codex", "codex", owner_pid=101, session_id="old-session"
    )
    process_table[101] = None

    assert _rtruntime.inspect_seat(root, "codex").status == "stale"
    fresh = _rtruntime.claim(
        root, "codex", "codex", owner_pid=102, session_id="fresh-session"
    )

    assert fresh.session_id == "fresh-session"
    assert fresh.revision != old.revision
    assert fresh.owner_pid == 102


def test_shared_guard_blocks_reclaim_until_routing_side_effect_finishes(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    old = _rtruntime.claim(
        root,
        "codex",
        "codex",
        owner_pid=101,
        session_id="old-session",
    )
    started = threading.Event()
    finished = threading.Event()
    replacements = []

    def replace_stale_owner():
        started.set()
        replacements.append(
            _rtruntime.claim(
                root,
                "codex",
                "codex",
                owner_pid=102,
                session_id="fresh-session",
            )
        )
        finished.set()

    with _rtruntime.seat_shared_guard(
        root,
        old.agent_id,
        old.session_id,
        old.revision,
    ) as guarded:
        assert guarded.session_id == old.session_id
        process_table[101] = None
        worker = threading.Thread(target=replace_stale_owner)
        worker.start()
        assert started.wait(1)
        assert not finished.wait(0.1)

    assert finished.wait(2)
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert replacements[0].session_id == "fresh-session"
    assert replacements[0].revision != old.revision


def test_pid_reuse_is_stale_but_uninspectable_owner_is_ambiguous(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    _rtruntime.claim(root, "claude", "claude", owner_pid=101)
    process_table[101] = "different-process-start"

    assert _rtruntime.inspect_seat(root, "claude").status == "stale"

    process_table[101] = _rtruntime.RuntimeStateError("permission denied")
    ambiguous = _rtruntime.inspect_seat(root, "claude")
    assert ambiguous.status == "ambiguous"
    assert "cannot be inspected" in ambiguous.detail
    with pytest.raises(_rtruntime.RuntimeStateError):
        _rtruntime.claim(root, "claude-review", "claude", owner_pid=102)


def test_stale_token_cannot_update_or_release_replacement(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    old = _rtruntime.claim(root, "codex", "codex", owner_pid=101)
    process_table[101] = None
    fresh = _rtruntime.claim(root, "codex", "codex", owner_pid=102)

    with pytest.raises(_rtruntime.FenceRejected):
        _rtruntime.update_activity(
            root, "codex", old.session_id, old.revision
        )
    assert not _rtruntime.release(old)

    current = _rtruntime.load_validated_lease(
        root, "codex", fresh.session_id, fresh.revision
    )
    assert current == fresh


def test_old_watcher_cannot_clear_or_update_replacement_watcher(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    lease = _rtruntime.claim(root, "claude", "claude", owner_pid=101)
    old = _rtruntime.update_wake(
        root,
        "claude",
        lease.session_id,
        lease.revision,
        watcher_pid=201,
    )
    process_table[201] = None
    replacement = _rtruntime.update_wake(
        root,
        "claude",
        lease.session_id,
        lease.revision,
        watcher_pid=202,
        expected_watcher_pid=201,
    )

    assert old.watcher_pid == 201
    assert replacement.watcher_pid == 202
    assert _rtruntime.watcher_is_live(replacement)
    with pytest.raises(_rtruntime.FenceRejected):
        _rtruntime.clear_wake(
            root,
            "claude",
            lease.session_id,
            lease.revision,
            expected_watcher_pid=201,
        )
    assert (
        _rtruntime.inspect_seat(root, "claude").token.watcher_pid == 202
    )


def test_activity_and_legacy_empty_count_share_the_fenced_record(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    lease = _rtruntime.claim(root, "claude", "claude", owner_pid=101)

    active = _rtruntime.update_activity(
        root, "claude", lease.session_id, lease.revision
    )
    backed_off = _rtruntime.update_wake(
        root,
        "claude",
        lease.session_id,
        lease.revision,
        watcher_pid=201,
        empty_beats=6,
    )

    assert active.activity_revision == 1
    assert active.activity_at is not None
    assert backed_off.activity_revision == 1
    assert backed_off.empty_beats == 6


def test_reclaim_project_runtime_removes_only_all_stale_leases(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    token = _rtruntime.claim(root, "claude", "claude", owner_pid=101)
    process_table[101] = None

    result = _rtruntime.reclaim_project_runtime(root)

    assert result.removed
    assert not result.blockers
    assert not result.path.exists()
    assert not _rtruntime.seat_paths(root, "claude").project_dir.exists()


def test_reclaim_project_runtime_preserves_live_or_ambiguous_leases(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    token = _rtruntime.claim(root, "claude", "claude", owner_pid=101)

    live = _rtruntime.reclaim_project_runtime(root)
    assert not live.removed
    assert live.blockers and "owner pid 101 is running" in live.blockers[0]
    assert live.path.exists()

    process_table[101] = RuntimeError("cannot inspect")
    ambiguous = _rtruntime.reclaim_project_runtime(root)
    assert not ambiguous.removed
    assert ambiguous.blockers and "cannot be inspected" in ambiguous.blockers[0]
    assert ambiguous.path.exists()

    process_table[101] = "start-owner-101"
    assert _rtruntime.release(token)


def test_reply_expectation_duration_and_ack_clear_are_fenced(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    lease = _rtruntime.claim(root, "codex", "codex", owner_pid=101)
    sent_at = "2026-08-07T05:00:00.000Z"

    item = _rtruntime.add_reply_expectation(
        root,
        "codex",
        lease.session_id,
        lease.revision,
        msg_id="20260807T050000Z-codex-to-claude-1",
        peer="claude",
        duration="30m",
        sent_at=sent_at,
    )

    assert item.deadline == "2026-08-07T05:30:00.000Z"
    assert _rtruntime.list_reply_expectations(root, "codex") == (item,)
    cleared, fired = _rtruntime.reconcile_reply_expectations(
        root,
        "codex",
        lease.session_id,
        lease.revision,
        {item.msg_id},
        now=datetime(2026, 8, 7, 5, 1, tzinfo=timezone.utc),
    )
    assert cleared == (item,)
    assert fired == ()
    assert _rtruntime.list_reply_expectations(root, "codex") == ()


def test_reply_expectation_fires_once_and_rejects_invalid_duration(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    lease = _rtruntime.claim(root, "codex", "codex", owner_pid=101)

    assert _rtruntime.parse_reply_duration("90s") == ("90s", 90)
    assert _rtruntime.parse_reply_duration("2H") == ("2h", 7200)
    for invalid in ("0s", "-1m", "soon", "30", "1d"):
        with pytest.raises(_rtruntime.RuntimeStateError):
            _rtruntime.parse_reply_duration(invalid)

    item = _rtruntime.add_reply_expectation(
        root,
        "codex",
        lease.session_id,
        lease.revision,
        msg_id="20260807T050001Z-codex-to-hermes-1",
        peer="hermes",
        duration="1s",
        sent_at="2026-08-07T05:00:00.000Z",
    )
    due = datetime(2026, 8, 7, 5, 0, 1, tzinfo=timezone.utc)
    cleared, fired = _rtruntime.reconcile_reply_expectations(
        root,
        "codex",
        lease.session_id,
        lease.revision,
        now=due,
    )
    assert cleared == ()
    assert fired == (item,)
    assert _rtruntime.list_reply_expectations(root, "codex") == ()
    again = _rtruntime.reconcile_reply_expectations(
        root,
        "codex",
        lease.session_id,
        lease.revision,
        now=due + timedelta(seconds=10),
    )
    assert again == ((), ())


def test_stale_heartbeat_never_makes_a_live_owner_claimable(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    lease = _rtruntime.claim(root, "codex", "codex", owner_pid=101)
    _rtruntime.update_wake(
        root, "codex", lease.session_id, lease.revision
    )

    inspection = _rtruntime.inspect_seat(
        root, "codex", heartbeat_ttl=-1.0
    )
    assert inspection.status == "active_unhealthy"
    with pytest.raises(_rtruntime.SeatOccupied):
        _rtruntime.claim(root, "codex-review", "codex", owner_pid=102)


def test_release_is_stale_and_second_release_is_fenced(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    lease = _rtruntime.claim(root, "hermes", "hermes", owner_pid=101)

    _rtruntime.release(lease)

    assert _rtruntime.inspect_seat(root, "hermes").status == "vacant"
    assert not _rtruntime.release(lease)


def test_corrupt_state_is_ambiguous_and_claim_fails_closed(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    paths = _rtruntime.seat_paths(root, "claude")
    _rtruntime.claim(root, "claude", "claude", owner_pid=101)
    paths.lease.write_text("{not-json")
    paths.lease.chmod(0o600)

    inspection = _rtruntime.inspect_seat(root, "claude")

    assert inspection.status == "ambiguous"
    assert "cannot read runtime JSON" in inspection.detail
    with pytest.raises(_rtruntime.RuntimeStateError):
        _rtruntime.claim(root, "claude", "claude", owner_pid=101)


@pytest.mark.parametrize(
    ("field", "value", "detail"),
    [
        ("ownerPid", 2**63, "ownerPid"),
        ("ownerStart", None, "ownerStart"),
        ("activityRevision", -1, "activityRevision"),
    ],
)
def test_invalid_lease_fields_are_ambiguous_instead_of_crashing(
    tmp_path, runtime, process_table, field, value, detail
):
    root = project(tmp_path)
    paths = _rtruntime.seat_paths(root, "claude")
    _rtruntime.claim(root, "claude", "claude", owner_pid=101)
    record = json.loads(paths.lease.read_text())
    record[field] = value
    paths.lease.write_text(json.dumps(record))
    paths.lease.chmod(0o600)

    inspection = _rtruntime.inspect_seat(root, "claude")

    assert inspection.status == "ambiguous"
    assert detail in inspection.detail


def test_exposed_or_symlinked_runtime_state_fails_closed(
    tmp_path, runtime, process_table
):
    root = project(tmp_path)
    paths = _rtruntime.seat_paths(root, "claude")
    token = _rtruntime.claim(root, "claude", "claude", owner_pid=101)

    paths.lease.chmod(0o644)
    exposed = _rtruntime.inspect_seat(root, "claude")
    assert exposed.status == "ambiguous"
    assert "group/other permissions" in exposed.detail

    paths.lease.chmod(0o600)
    target = tmp_path / "foreign-lock"
    target.write_text("unchanged")
    paths.state_lock.unlink()
    paths.state_lock.symlink_to(target)
    with pytest.raises(_rtruntime.RuntimeStateError, match="lock is a symlink"):
        _rtruntime.update_activity(
            root,
            "claude",
            token.session_id,
            token.revision,
        )
    assert target.read_text() == "unchanged"
