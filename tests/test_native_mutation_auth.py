"""Native mutation authorization contracts; no visible-harness acceptance claims."""

from contextlib import contextmanager
import fcntl
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

import _kit as kit

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import _rtmail as mail
import _rtnative as native
import _rtruntime as runtime
from _rtlib import register_project


loader = importlib.machinery.SourceFileLoader("native_mutation_auth_cli", str(BIN / "rt-native"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cli = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = cli
loader.exec_module(cli)


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))
    for name in (
        "RT_FROM", "RT_PROJECT_ROOT", "RT_SESSION_ID", "RT_LEASE_REVISION",
        "ROUNDTABLE_PROJECT_DIR", "RT_FALLBACK_PROJECT", "CODEX_THREAD_ID",
        "CODEX_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_TOOL_USE_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "project"
    kit.write_project(project, (kit.CODEX, kit.CLAUDE), project='"."')
    register_project(project)
    monkeypatch.chdir(project)
    return project.resolve()


@pytest.fixture
def mail_core(monkeypatch):
    # Deliberately stub the mail boundary: mail commit/recovery contracts have
    # separate coverage, while these tests exercise real leases and guards.
    core = Mock(return_value={"status": "committed", "msg_id": "synthetic-message"})
    monkeypatch.setattr(mail, "mutate_mail", core)
    return core


@pytest.fixture(params=["send", "ack"])
def operation(request):
    return request.param


def parameters(operation, harness="codex"):
    if operation == "send":
        return {"target": "claude" if harness == "codex" else "codex", "kind": "question", "body": "synthetic body"}
    return {"refs": ["20260918T010203000000Z-claude-to-codex"], "body": "reviewed"}


def bind(project, harness="codex", native_id="native-root"):
    return native.bind_native(project, harness, native_id, os.getpid(), environ={})


def mutate(project, operation, *, harness="codex", native_id="native-root", environ=None, owner_pid=None):
    return native.mutate(
        project, harness, native_id, operation, parameters(operation, harness),
        environ={} if environ is None else environ,
        owner_pid=os.getpid() if owner_pid is None else owner_pid,
    )


def sibling(project):
    other = project.with_name("other-worktree")
    kit.write_project(other, (kit.CODEX, kit.CLAUDE), project='"."')
    register_project(other)
    return other.resolve()


def assert_exclusive_lock_blocked(path):
    with path.open("r+") as lock:
        with pytest.raises(BlockingIOError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_bound_root_calls_mail_core_with_its_configured_seat(host, mail_core, operation, harness):
    binding = bind(host, harness)

    result = mutate(host, operation, harness=harness)

    mail_core.assert_called_once_with(host, harness, operation, parameters(operation, harness))
    assert result["schema"] == "roundtable.native-mutation.v1"
    assert result["operation"] == operation
    assert result["mutation"] == mail_core.return_value
    assert result["binding"] == binding["binding"]
    assert result["lease"] == binding["lease"]
    assert result["native_busy"] == "unknown"


def test_unbound_root_cannot_mutate_or_create_a_lease(host, mail_core, operation):
    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation)

    assert error.value.status == "unbound"
    mail_core.assert_not_called()
    assert not runtime.runtime_root().exists()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_same_cwd_other_root_cannot_mutate(host, mail_core, operation, harness):
    bind(host, harness)

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation, harness=harness, native_id="other-native-root")

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


def test_native_binding_does_not_authorize_a_sibling_project(host, mail_core, operation):
    bind(host)
    other = sibling(host)

    with pytest.raises(native.NativeQueryError) as error:
        mutate(other, operation)

    assert error.value.status == "unbound"
    mail_core.assert_not_called()
    assert runtime.inspect_seat(other, "codex").status == "vacant"


def test_wrong_project_cannot_use_its_different_bound_root(host, mail_core, operation):
    bind(host)
    other = sibling(host)
    bind(other, native_id="other-project-root")

    with pytest.raises(native.NativeQueryError) as error:
        mutate(other, operation)

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


@pytest.mark.parametrize("environment", [
    {"RT_FROM": "claude"},
    {"RT_SESSION_ID": "other-session"},
    {"RT_LEASE_REVISION": "other-revision"},
    {"RT_PROJECT_ROOT": "/different-project"},
    {"ROUNDTABLE_PROJECT_DIR": "/different-project"},
    {"RT_SESSION_ID": ""},
    {"RT_LEASE_REVISION": ""},
])
def test_partial_inherited_conflict_denies_before_mutation(host, mail_core, operation, environment):
    bind(host)

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation, environ=environment)

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


def test_matching_partial_fence_does_not_need_a_legacy_wake_binding(host, mail_core, operation):
    binding = bind(host)
    capability = runtime.read_seat_capability(host, "codex")
    assert capability.get("threadId") is None

    result = mutate(host, operation, environ={"RT_LEASE_REVISION": binding["lease"]["revision"]})

    assert result["mutation"]["status"] == "committed"
    mail_core.assert_called_once()


def test_legacy_wake_binding_alone_does_not_authorize_native_mutation(host, mail_core, operation):
    token = runtime.claim(host, "codex", "codex")
    runtime.record_seat_capability(
        host, "codex", "codex", session_id=token.session_id,
        revision=token.revision, thread_id="native-root", binding_revision="legacy-wake",
    )

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation)

    assert error.value.status == "unbound"
    mail_core.assert_not_called()


def test_complete_matching_fence_cannot_impersonate_another_root(host, mail_core, operation):
    binding = bind(host)
    environment = {
        "RT_FROM": "codex", "RT_PROJECT_ROOT": str(host),
        "RT_SESSION_ID": binding["lease"]["sessionId"],
        "RT_LEASE_REVISION": binding["lease"]["revision"],
    }

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation, native_id="other-root", environ=environment)

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


@pytest.mark.parametrize("field", ["threadId", "wake.nativeSessionId"])
def test_conflicting_legacy_native_identity_denies_mutation(host, mail_core, operation, field):
    binding = bind(host)
    lease = binding["lease"]
    if field == "threadId":
        runtime.record_seat_capability(
            host, "codex", "codex", session_id=lease["sessionId"],
            revision=lease["revision"], thread_id="other-native-root",
        )
    else:
        runtime.update_wake(host, "codex", lease["sessionId"], lease["revision"], native_session_id="other-native-root")

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation)

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


def test_replaced_lease_denies_old_mutation(host, mail_core, operation):
    binding = bind(host)
    lease = binding["lease"]
    runtime.claim(host, "codex", "codex", replace_fence=(lease["sessionId"], lease["revision"]))

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation)

    assert error.value.status == "stale"
    mail_core.assert_not_called()


@pytest.mark.parametrize("change", ["dead", "generation"])
def test_dead_or_reused_owner_denies_mutation(host, mail_core, operation, monkeypatch, change):
    bind(host)
    if change == "dead":
        monkeypatch.setattr(runtime, "_pid_state", lambda _pid: "dead")
    else:
        monkeypatch.setattr(runtime, "process_start_fingerprint", lambda _pid: "reused-owner-generation")

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation)

    assert error.value.status == "stale"
    mail_core.assert_not_called()


def test_different_live_calling_owner_cannot_mutate(host, mail_core, operation):
    bind(host)

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation, owner_pid=os.getpid() + 1)

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


def test_mutation_rejects_duplicate_native_binding_in_another_project(host, mail_core, operation):
    bind(host)
    other = sibling(host)
    token = runtime.claim(other, "codex", "codex")
    root_binding = runtime.read_seat_capability(host, "codex")["nativeQuery"]
    runtime.record_seat_capability(
        other, "codex", "codex", session_id=token.session_id,
        revision=token.revision, native_query=root_binding,
    )

    with pytest.raises(native.NativeQueryError) as error:
        mutate(host, operation)

    assert error.value.status == "conflict"
    mail_core.assert_not_called()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_host_then_claim_guards_cover_entire_mail_mutation(host, mail_core, operation, monkeypatch, harness):
    bind(host, harness)
    host_lock = runtime.runtime_root() / native.HOST_BIND_LOCK
    claim_lock = runtime.seat_paths(host, harness).claim_lock
    real_claim_guard = native.seat_shared_guard
    stages = []

    @contextmanager
    def checked_claim_guard(*args, **kwargs):
        assert_exclusive_lock_blocked(host_lock)
        stages.append("host-before-claim")
        with real_claim_guard(*args, **kwargs) as token:
            yield token

    def guarded_write(*args):
        assert_exclusive_lock_blocked(host_lock)
        assert_exclusive_lock_blocked(claim_lock)
        stages.append("guarded-mail")
        return {"status": "committed", "msg_id": "guarded-message"}

    monkeypatch.setattr(native, "seat_shared_guard", checked_claim_guard)
    mail_core.side_effect = guarded_write

    result = mutate(host, operation, harness=harness)

    assert stages == ["host-before-claim", "guarded-mail"]
    assert result["mutation"]["msg_id"] == "guarded-message"
    # Both locks are released only after the operation has returned.
    for path in (host_lock, claim_lock):
        with path.open("r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("outcome_status", ["committed", "partial", "unknown"])
@pytest.mark.parametrize("failure", ["native", "runtime", "os", "value"])
def test_known_mutation_outcome_survives_post_validation_failure(
    host, mail_core, operation, monkeypatch, outcome_status, failure
):
    bind(host)
    real_bound = native._bound_capability
    core_finished = False
    outcome = {"status": outcome_status, "msg_id": "known-message", "refs": ["known-ref"], "receipt_ids": ["known-receipt"]}

    def finish_mail(*args):
        nonlocal core_finished
        core_finished = True
        return outcome

    def check_binding(*args, **kwargs):
        if core_finished:
            assert_exclusive_lock_blocked(runtime.runtime_root() / native.HOST_BIND_LOCK)
            assert_exclusive_lock_blocked(runtime.seat_paths(host, "codex").claim_lock)
            if failure == "native":
                raise native.NativeQueryError("stale", "owner exited after mail commit")
            if failure == "runtime":
                raise runtime.FenceRejected("owner exited after mail commit")
            if failure == "value":
                raise ValueError("binding became malformed after mail commit")
            raise OSError("binding could not be read after mail commit")
        return real_bound(*args, **kwargs)

    mail_core.side_effect = finish_mail
    monkeypatch.setattr(native, "_bound_capability", check_binding)

    result = mutate(host, operation)

    assert result["mutation"] == outcome
    assert result["binding"]["status"] == "stale"
    assert result["binding"]["error"]
    mail_core.assert_called_once()


@pytest.mark.parametrize("outcome_status", ["committed", "partial", "unknown"])
@pytest.mark.parametrize("failure", [OSError, ValueError, runtime.RuntimeStateError])
def test_known_mutation_outcome_survives_guard_exit_failure(
    host, mail_core, operation, monkeypatch, outcome_status, failure
):
    bind(host)
    real_claim_guard = native.seat_shared_guard
    outcome = {"status": outcome_status, "msg_id": "known-message", "refs": ["known-ref"], "receipt_ids": ["known-receipt"]}
    mail_core.return_value = outcome

    @contextmanager
    def failing_claim_guard(*args, **kwargs):
        with real_claim_guard(*args, **kwargs) as token:
            yield token
        raise failure("guard cleanup failed after mail commit")

    monkeypatch.setattr(native, "seat_shared_guard", failing_claim_guard)

    result = mutate(host, operation)

    assert result["mutation"] == outcome
    assert result["binding"]["status"] == "stale"
    assert result["binding"]["error"]
    mail_core.assert_called_once()


@pytest.mark.parametrize("native_environment", [
    {"CODEX_THREAD_ID": "child-thread", "CODEX_SESSION_ID": "native-root"},
    {"CODEX_THREAD_ID": "native-root"},
    {"CODEX_SESSION_ID": "native-root"},
])
def test_cli_child_or_missing_native_identity_cannot_mutate(
    host, mail_core, operation, monkeypatch, capsys, native_environment
):
    bind(host)
    for name, value in native_environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "native_owner", lambda _harness: os.getpid())
    arguments = ["claude", "question", "body"] if operation == "send" else ["exact-original-ref"]
    monkeypatch.setattr(sys, "argv", ["rt-native", operation, *arguments])

    code = cli.main()
    result = json.loads(capsys.readouterr().out)

    assert code == 2
    assert result["binding"]["status"] == "unsupported"
    assert result["mutation"]["status"] == "not_committed"
    mail_core.assert_not_called()


def test_claude_child_hook_denies_mutation_before_minting_ticket(
    host, mail_core, operation, monkeypatch
):
    bind(host, "claude")
    monkeypatch.setattr(cli, "native_owner", lambda _harness: os.getpid())
    monkeypatch.setattr(cli, "require_hook_transport", lambda *_args: None)
    command = "rt-native send codex question 'body'" if operation == "send" else "rt-native ack exact-original-ref"
    payload = {
        "hook_event_name": "PreToolUse", "session_id": "native-root", "cwd": str(host),
        "agent_id": "native-child", "tool_name": "Bash", "tool_use_id": "child-call",
        "tool_input": {"command": command},
    }

    with pytest.raises(native.NativeQueryError) as error:
        cli.run_hook("claude", host, payload)

    assert error.value.status == "unsupported"
    mail_core.assert_not_called()
    assert not cli.ticket_directory().exists()
