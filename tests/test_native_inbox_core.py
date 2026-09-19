"""Native query core contracts; synthetic isolated tests, not harness acceptance."""

import fcntl
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import _kit as kit

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import _rtnative as native
import _rtruntime as runtime
from _rtlib import format_mail_envelope, register_project


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    for name in ("RT_FROM", "RT_PROJECT_ROOT", "RT_SESSION_ID", "RT_LEASE_REVISION", "ROUNDTABLE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project"
    kit.write_project(project, (kit.CODEX, kit.CLAUDE), project='"."')
    register_project(project)
    return project.resolve()


def bind(project, harness="codex", native_id="native-root"):
    return native.bind_native(project, harness, native_id, os.getpid(), environ={})


def snapshot(path):
    return {
        str(item.relative_to(path)): (item.read_bytes(), item.stat().st_mtime_ns)
        for item in path.rglob("*") if item.is_file()
    }


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_repeat_queries_preserve_mail_and_runtime(host, harness):
    bind(host, harness)
    new = host / ".roundtable" / "inbox" / harness / "new"
    new.mkdir(parents=True)
    msg_id = f"20260101T000000000000Z-claude-to-{harness}"
    mail = new / f"{msg_id}.md"
    mail.write_text(format_mail_envelope("claude", harness, "question", msg_id, "synthetic pending body"))
    quiet = new / f"ack-20260101T000001000000Z-claude-to-{harness}.md"
    quiet.write_text(format_mail_envelope("claude", harness, "sync-ack", f"20260101T000001000000Z-claude-to-{harness}", "synthetic quiet ack"))
    before_mail = snapshot(host / ".roundtable" / "inbox")
    before_runtime = snapshot(runtime.runtime_root())
    first = native.query(host, harness, "native-root", "inbox", environ={}, owner_pid=os.getpid())
    second = native.query(host, harness, "native-root", "inbox", environ={}, owner_pid=os.getpid())
    status = native.query(host, harness, "native-root", "status", environ={}, owner_pid=os.getpid())
    assert first == second
    assert len(first["messages"]) == 1
    message = first["messages"][0]
    assert message["msg_id"] == msg_id
    assert message["body"] == "synthetic pending body"
    assert message["lifecycle"] == "new"
    assert status["binding"]["status"] == "bound"
    assert status["lease"]["status"] == "active"
    assert status["native_busy"] == "unknown"
    assert "messages" not in status
    assert snapshot(host / ".roundtable" / "inbox") == before_mail
    assert snapshot(runtime.runtime_root()) == before_runtime


def test_missing_binding_is_not_an_empty_inbox(host):
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "root", "inbox", environ={})
    assert error.value.status == "unbound"
    assert not runtime.runtime_root().exists()


@pytest.mark.parametrize("native_id", ["other-root", "fork-root", "subagent-root"])
def test_same_project_other_root_cannot_read(host, monkeypatch, native_id):
    bind(host)
    monkeypatch.setattr(native, "_read_inbox", lambda *_: pytest.fail("unauthorized inbox read"))
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", native_id, "inbox", environ={})
    assert error.value.status == "conflict"


def test_wrong_worktree_and_project_do_not_find_binding(host, tmp_path):
    bind(host)
    other = tmp_path / "sibling"
    kit.write_project(other, (kit.CODEX, kit.CLAUDE), project='"."')
    register_project(other)
    with pytest.raises(native.NativeQueryError) as error:
        native.query(other, "codex", "native-root", "inbox", environ={})
    assert error.value.status == "unbound"


@pytest.mark.parametrize("env", [
    {"RT_FROM": "claude"},
    {"RT_SESSION_ID": "other-session"},
    {"RT_LEASE_REVISION": "other-fence"},
    {"RT_PROJECT_ROOT": "/different-project"},
    {"ROUNDTABLE_PROJECT_DIR": "/different-project"},
    {"RT_SESSION_ID": ""},
])
def test_partial_environment_conflicts_reject_before_read(host, monkeypatch, env):
    bind(host)
    monkeypatch.setattr(native, "_read_inbox", lambda *_: pytest.fail("unauthorized inbox read"))
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "native-root", "inbox", environ=env)
    assert error.value.status == "conflict"


def test_matching_complete_environment_still_requires_native_root(host):
    binding = bind(host)
    env = {"RT_FROM": "codex", "RT_PROJECT_ROOT": str(host), "RT_SESSION_ID": binding["lease"]["sessionId"], "RT_LEASE_REVISION": binding["lease"]["revision"]}
    assert native.query(host, "codex", "native-root", "status", environ=env)["lease"]["status"] == "active"
    with pytest.raises(native.NativeQueryError, match="root does not own"):
        native.query(host, "codex", "wrong-root", "status", environ=env)


def test_replaced_lease_invalidates_binding(host):
    binding = bind(host)
    old = binding["lease"]
    replacement = runtime.claim(host, "codex", "codex", replace_fence=(old["sessionId"], old["revision"]))
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "native-root", "inbox", environ={})
    assert error.value.status == "stale"
    runtime.record_seat_capability(host, "codex", "codex", session_id=replacement.session_id, revision=replacement.revision, surface=None)
    assert runtime.read_seat_capability(host, "codex")["nativeQuery"] is None


def test_dead_owner_invalidates_binding(host, monkeypatch):
    bind(host)
    monkeypatch.setattr(runtime, "_pid_state", lambda pid: "dead")
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "native-root", "inbox", environ={})
    assert error.value.status == "stale"


def test_owner_generation_must_match(host, monkeypatch):
    bind(host)
    monkeypatch.setattr(runtime, "process_start_fingerprint", lambda pid: "different-start")
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "native-root", "status", environ={})
    assert error.value.status == "stale"


def test_calling_owner_pid_must_match(host):
    bind(host)
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "native-root", "status", environ={}, owner_pid=os.getpid() + 1)
    assert error.value.status == "conflict"


def test_read_holds_project_claim_lock(host, monkeypatch):
    bind(host)
    def check_guard(*args):
        with runtime.seat_paths(host, "codex").claim_lock.open("a+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return []
    monkeypatch.setattr(native, "_read_inbox", check_guard)
    assert native.query(host, "codex", "native-root", "inbox", environ={})["messages"] == []


def test_legacy_wake_capability_does_not_authorize_native_query(host):
    token = runtime.claim(host, "codex", "codex")
    runtime.record_seat_capability(host, "codex", "codex", session_id=token.session_id, revision=token.revision, thread_id="native-root", binding_revision="legacy-wake")
    for action in (lambda: native.query(host, "codex", "native-root", "inbox", environ={}), lambda: bind(host)):
        with pytest.raises(native.NativeQueryError) as error:
            action()
        assert error.value.status == "unbound"


@pytest.mark.parametrize("field", ["threadId", "wake.nativeSessionId"])
def test_existing_native_identity_fields_must_agree(host, monkeypatch, field):
    bind(host)
    record = runtime.read_seat_capability(host, "codex")
    def set_native_id(value):
        if field == "threadId":
            runtime.record_seat_capability(host, "codex", "codex", session_id=record["roundtableSessionId"], revision=record["leaseRevision"], thread_id=value)
        else:
            runtime.update_wake(host, "codex", record["roundtableSessionId"], record["leaseRevision"], native_session_id=value)
    set_native_id("native-root")
    assert native.query(host, "codex", "native-root", "status", environ={})["binding"]["status"] == "bound"
    set_native_id("other-native-root")
    monkeypatch.setattr(native, "_read_inbox", lambda *_: pytest.fail("conflicting native identities must not read mail"))
    for operation in ("status", "inbox"):
        with pytest.raises(native.NativeQueryError) as error:
            native.query(host, "codex", "native-root", operation, environ={})
        assert error.value.status == "conflict"
    with pytest.raises(native.NativeQueryError) as error:
        bind(host)
    assert error.value.status == "conflict"


def test_bind_is_idempotent_but_cannot_take_an_active_seat(host):
    first = bind(host)
    assert bind(host) == first
    before = snapshot(runtime.runtime_root())
    with pytest.raises(native.NativeQueryError) as error:
        bind(host, native_id="other-root")
    assert error.value.status == "conflict"
    assert snapshot(runtime.runtime_root()) == before


def test_bind_conflicting_environment_does_not_claim(host):
    with pytest.raises(native.NativeQueryError) as error:
        native.bind_native(host, "codex", "native-root", os.getpid(), environ={"RT_SESSION_ID": "stale-session"})
    assert error.value.status == "conflict"
    assert runtime.inspect_seat(host, "codex").status == "vacant"
    assert runtime.read_seat_capability(host, "codex") is None


def test_query_rechecks_native_binding_after_read(host, monkeypatch):
    bind(host)
    def replace_binding(*args):
        record = runtime.read_seat_capability(host, "codex")
        changed = dict(record["nativeQuery"], nativeSessionId="replacement-root")
        runtime.record_seat_capability(host, "codex", "codex", session_id=record["roundtableSessionId"], revision=record["leaseRevision"], native_query=changed, claim_lock_held=True)
        return [{"body": "must not escape"}]
    monkeypatch.setattr(native, "_read_inbox", replace_binding)
    with pytest.raises(native.NativeQueryError):
        native.query(host, "codex", "native-root", "inbox", environ={})


def test_capability_surface_refresh_preserves_native_binding(host):
    bind(host)
    record = runtime.read_seat_capability(host, "codex")
    runtime.record_seat_capability(host, "codex", "codex", session_id=record["roundtableSessionId"], revision=record["leaseRevision"], surface={"kind": "tmux", "target": "%1"})
    assert runtime.read_seat_capability(host, "codex")["nativeQuery"] == record["nativeQuery"]


@pytest.mark.parametrize("harness,source,wrong_source", [
    ("claude", "session-start", "native-shell"),
    ("codex", "native-shell", "session-start"),
])
def test_native_binding_source_is_harness_specific(host, harness, source, wrong_source):
    bind(host, harness)
    record = runtime.read_seat_capability(host, harness)
    assert record["nativeQuery"]["source"] == source
    changed = dict(record["nativeQuery"], source=wrong_source)
    with pytest.raises(runtime.RuntimeStateError, match="binding source is invalid"):
        runtime.record_seat_capability(host, harness, harness, session_id=record["roundtableSessionId"], revision=record["leaseRevision"], native_query=changed)


@pytest.mark.parametrize("change", [{"ownerPid": True}, {"source": "environment"}, {"secret": "no"}, {"harness": "other"}])
def test_native_binding_fields_are_validated(host, change):
    bind(host)
    record = runtime.read_seat_capability(host, "codex")
    malformed = dict(record["nativeQuery"], **change)
    with pytest.raises(runtime.RuntimeStateError):
        runtime.record_seat_capability(host, "codex", "codex", session_id=record["roundtableSessionId"], revision=record["leaseRevision"], native_query=malformed)


def sibling_project(host):
    project = host.with_name("sibling-worktree")
    kit.write_project(project, (kit.CODEX, kit.CLAUDE), project='"."')
    register_project(project)
    return project.resolve()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_native_root_cannot_bind_another_worktree(host, harness):
    bind(host, harness)
    other = sibling_project(host)
    with pytest.raises(native.NativeQueryError) as error:
        bind(other, harness)
    assert error.value.status == "conflict"
    assert runtime.inspect_seat(other, harness).status == "vacant"


def test_equal_native_id_with_another_live_owner_cannot_bind(host):
    bind(host)
    other = sibling_project(host)
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as owner:
        try:
            with pytest.raises(native.NativeQueryError) as error:
                native.bind_native(other, "codex", "native-root", owner.pid, environ={})
            assert error.value.status == "conflict"
            assert runtime.inspect_seat(other, "codex").status == "vacant"
        finally:
            owner.terminate()


def test_stale_other_project_binding_has_no_authority(host):
    with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as owner:
        try:
            native.bind_native(host, "codex", "native-root", owner.pid, environ={})
        finally:
            owner.terminate()
    assert runtime.inspect_seat(host, "codex").status == "stale"
    other = sibling_project(host)
    assert bind(other)["binding"]["status"] == "bound"
    assert native.query(other, "codex", "native-root", "status", environ={})["lease"]["status"] == "active"
    with pytest.raises(native.NativeQueryError) as error:
        native.query(host, "codex", "native-root", "status", environ={})
    assert error.value.status == "stale"


def test_query_rejects_preexisting_duplicate_native_binding(host, monkeypatch):
    bind(host)
    other = sibling_project(host)
    # Simulate a pre-fix duplicate through the existing authority writer.
    token = runtime.claim(other, "codex", "codex")
    native_binding = runtime.read_seat_capability(host, "codex")["nativeQuery"]
    runtime.record_seat_capability(other, "codex", "codex", session_id=token.session_id, revision=token.revision, native_query=native_binding)
    monkeypatch.setattr(native, "_read_inbox", lambda *_: pytest.fail("ambiguous native root must not read mail"))
    for project in (host, other):
        with pytest.raises(native.NativeQueryError) as error:
            native.query(project, "codex", "native-root", "inbox", environ={})
        assert error.value.status == "conflict"


def test_concurrent_worktree_binds_have_exactly_one_winner(host):
    other = sibling_project(host)
    ready = threading.Barrier(2)
    def attempt(project):
        ready.wait(timeout=5)
        try:
            return bind(project)["binding"]["status"]
        except native.NativeQueryError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(attempt, host)
        second = pool.submit(attempt, other)
        assert sorted((first.result(timeout=10), second.result(timeout=10))) == ["bound", "conflict"]


def test_inbox_read_holds_host_binding_lock(host, monkeypatch):
    bind(host)
    def check_guard(*args):
        with (runtime.runtime_root() / native.HOST_BIND_LOCK).open("a+") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return []
    monkeypatch.setattr(native, "_read_inbox", check_guard)
    assert native.query(host, "codex", "native-root", "inbox", environ={})["messages"] == []
