from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import random
import stat
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
HOOK = BIN / "rt-codex-session-start"
sys.path.insert(0, str(BIN))

import _rtruntime


def load_wake_module():
    name = "rt_codex_auto_bind_wake"
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / "rt-codex-wake"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


wake = load_wake_module()


def load_hook_module():
    name = "rt_codex_auto_bind_hook"
    loader = importlib.machinery.SourceFileLoader(name, str(HOOK))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


hook = load_hook_module()


def write_project(path: Path) -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n"
    )
    return project


def claim_environment(tmp_path: Path, project: Path) -> tuple[dict[str, str], object]:
    runtime = tmp_path / "runtime"
    os.environ["RT_RUNTIME_DIR"] = str(runtime)
    os.environ["RT_CODEX_RUNTIME_DIR"] = str(runtime)
    token = _rtruntime.claim(project, "codex", "codex")
    _rtruntime.arm_codex_launch_intent(token)
    environment = os.environ.copy()
    environment.update(
        {
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": "codex",
            "RT_SESSION_ID": token.session_id,
            "RT_LEASE_REVISION": token.revision,
        }
    )
    return environment, token


def hook_payload(project: Path, thread_id: str = "thread-1", source: str = "startup"):
    return {
        "session_id": thread_id,
        "cwd": str(project),
        "hook_event_name": "SessionStart",
        "source": source,
    }


def run_hook(payload: dict, environment: dict[str, str]):
    return subprocess.run(
        [str(HOOK)],
        input=json.dumps(payload),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )


def run_hook_in_process(payload: dict, environment: dict[str, str]) -> int:
    stdin = type("HookInput", (), {})()
    stdin.buffer = io.BytesIO(json.dumps(payload).encode())
    return hook.run(stdin, environment)


def selected_thread(project: Path, thread_id: str) -> dict:
    return {
        "id": thread_id,
        "sessionId": f"native-{thread_id}",
        "cwd": str(project),
        "source": "vscode",
        "threadSource": "user",
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": "idle"},
    }


class Client:
    def __init__(self, project: Path, thread_ids: list[str]):
        self.threads = {
            thread_id: selected_thread(project, thread_id) for thread_id in thread_ids
        }
        self.calls = []
        self.turn_threads = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": dict(self.threads.get(params["threadId"]) or {})}
        if method == "thread/resume":
            return {"thread": dict(self.threads.get(params["threadId"]) or {})}
        if method == "hooks/list":
            return {
                "data": [
                    {
                        "cwd": next(iter(self.threads.values()))["cwd"],
                        "hooks": [],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        if method == "turn/start":
            self.turn_threads.append(params["threadId"])
            return {"turn": {"id": f"turn-{len(self.turn_threads)}"}}
        raise AssertionError(method)


@pytest.fixture(autouse=True)
def isolate_environment(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(wake, "RUNTIME_DIR", runtime)


def test_session_start_hook_queues_one_private_request(tmp_path):
    project = write_project(tmp_path / "project")
    environment, token = claim_environment(tmp_path, project)

    result = run_hook(hook_payload(project), environment)

    queue = tmp_path / "runtime" / "codex-bind-requests"
    requests = list(queue.glob("*.json"))
    assert result.returncode == 0, result.stderr
    assert len(requests) == 1
    assert stat.S_IMODE(queue.stat().st_mode) == 0o700
    assert stat.S_IMODE(requests[0].stat().st_mode) == 0o600
    intent = (
        tmp_path
        / "runtime"
        / "projects"
        / _rtruntime.project_hash(project)
        / _rtruntime.CODEX_LAUNCH_INTENT_NAME
    )
    assert stat.S_IMODE(intent.stat().st_mode) == 0o600
    assert stat.S_IMODE(intent.parent.stat().st_mode) == 0o700
    lock = tmp_path / "runtime" / _rtruntime.BIND_REQUEST_LOCK_NAME
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    payload = json.loads(requests[0].read_text())
    assert payload["threadId"] == "thread-1"
    assert payload["projectRoot"] == str(project)
    assert payload["roundtableSessionId"] == token.session_id
    assert payload["leaseRevision"] == token.revision


@pytest.mark.parametrize(
    ("change", "value"),
    [
        ("source", "compact"),
        ("hook_event_name", "Stop"),
        ("cwd", "other"),
    ],
)
def test_session_start_hook_noops_for_irrelevant_input(
    tmp_path,
    change,
    value,
):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    payload = hook_payload(project)
    payload[change] = str(tmp_path / "other") if change == "cwd" else value

    result = run_hook(payload, environment)

    assert result.returncode == 0
    assert not (tmp_path / "runtime" / "codex-bind-requests").exists()


def test_session_start_hook_uses_runtime_intent_without_lease_environment(tmp_path):
    project = write_project(tmp_path / "project")
    environment, token = claim_environment(tmp_path, project)
    for name in (
        "RT_PROJECT_ROOT",
        "RT_FROM",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
    ):
        environment.pop(name, None)

    result = run_hook(hook_payload(project), environment)

    assert result.returncode == 0
    request = next((tmp_path / "runtime" / "codex-bind-requests").glob("*.json"))
    queued = json.loads(request.read_text())
    assert queued["roundtableSessionId"] == token.session_id
    assert queued["leaseRevision"] == token.revision


def test_session_start_hook_noops_without_launcher_intent(tmp_path):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update(
        {
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
        }
    )

    result = run_hook(hook_payload(project), environment)

    assert result.returncode == 0
    assert not (runtime / "codex-bind-requests").exists()


def test_session_start_hook_noops_for_expired_launcher_intent(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    intent = (
        tmp_path
        / "runtime"
        / "projects"
        / _rtruntime.project_hash(project)
        / _rtruntime.CODEX_LAUNCH_INTENT_NAME
    )
    payload = json.loads(intent.read_text())
    payload["armedAt"] = "2000-01-01T00:00:00Z"
    intent.write_text(json.dumps(payload))
    intent.chmod(0o600)

    result = run_hook(hook_payload(project), environment)

    assert result.returncode == 0
    assert not (tmp_path / "runtime" / "codex-bind-requests").exists()


def test_session_start_hook_noops_when_launcher_owner_died(
    tmp_path,
    monkeypatch,
):
    project = write_project(tmp_path / "project")
    environment, token = claim_environment(tmp_path, project)
    observed_pid_state = _rtruntime._pid_state
    monkeypatch.setattr(
        _rtruntime,
        "_pid_state",
        lambda pid: "dead" if pid == token.owner_pid else observed_pid_state(pid),
    )

    result = run_hook_in_process(hook_payload(project), environment)

    assert result == 0
    assert not (tmp_path / "runtime" / "codex-bind-requests").exists()


def test_session_start_hook_noops_for_non_utf8_scalar_text(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    payload = hook_payload(project)
    payload["session_id"] = "\ud800"

    result = run_hook(payload, environment)

    assert result.returncode == 0
    assert not (tmp_path / "runtime" / "codex-bind-requests").exists()


def test_session_start_hook_refuses_symlink_request_directory(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    runtime = tmp_path / "runtime"
    target = tmp_path / "outside"
    target.mkdir()
    (runtime / "codex-bind-requests").symlink_to(target, target_is_directory=True)

    result = run_hook(hook_payload(project), environment)

    assert result.returncode == 1
    assert "is a symlink" in result.stderr
    assert list(target.iterdir()) == []


def test_session_start_hook_refuses_symlink_request_target(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    payload = hook_payload(project)
    assert run_hook(payload, environment).returncode == 0
    queue = tmp_path / "runtime" / "codex-bind-requests"
    request = next(queue.glob("*.json"))
    request.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("do not replace")
    request.symlink_to(outside)

    result = run_hook(payload, environment)

    assert result.returncode == 1
    assert "not a regular file" in result.stderr
    assert request.is_symlink()
    assert outside.read_text() == "do not replace"


def test_bridge_validates_and_auto_binds_current_fenced_request(tmp_path):
    project = write_project(tmp_path / "project")
    environment, token = claim_environment(tmp_path, project)
    assert run_hook(hook_payload(project), environment).returncode == 0
    store = wake.StateStore(tmp_path / "wake-state.json")

    changed = wake.drain_bind_requests(
        Client(project, ["thread-1"]),
        store,
        [project],
        requests_dir=tmp_path / "runtime" / "codex-bind-requests",
    )

    binding = store.bindings[str(project)]
    assert changed == {str(project)}
    assert binding["threadId"] == "thread-1"
    assert binding["roundtableSessionId"] == token.session_id
    assert binding["leaseRevision"] == token.revision
    assert list((tmp_path / "runtime" / "codex-bind-requests").iterdir()) == []
    inspection = _rtruntime.inspect_seat(project, "codex")
    assert inspection.token.native_session_id == "thread-1"


def test_malformed_thread_read_is_rejected_without_crashing_bridge(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    assert run_hook(hook_payload(project), environment).returncode == 0
    queue = tmp_path / "runtime" / "codex-bind-requests"
    store = wake.StateStore(tmp_path / "wake-state.json")

    class MalformedThreadClient:
        def request(self, method, params):
            assert method == "thread/read"
            return {"thread": []}

    changed = wake.drain_bind_requests(
        MalformedThreadClient(),
        store,
        [project],
        requests_dir=queue,
    )

    assert changed == set()
    assert store.bindings == {}
    assert list(queue.iterdir()) == []


def test_auto_bind_replay_is_idempotent(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    queue = tmp_path / "runtime" / "codex-bind-requests"
    store = wake.StateStore(tmp_path / "wake-state.json")
    client = Client(project, ["thread-1"])
    assert run_hook(hook_payload(project), environment).returncode == 0
    assert wake.drain_bind_requests(
        client, store, [project], requests_dir=queue
    ) == {str(project)}
    revision = store.bindings[str(project)]["bindingRevision"]

    assert run_hook(hook_payload(project), environment).returncode == 0
    assert wake.drain_bind_requests(client, store, [project], requests_dir=queue) == set()
    assert store.bindings[str(project)]["bindingRevision"] == revision


@pytest.mark.parametrize("nested_source", ["startup", "resume"])
def test_nested_codex_cannot_replace_first_launcher_request(
    tmp_path,
    nested_source,
):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    queue = tmp_path / "runtime" / "codex-bind-requests"
    assert run_hook(
        hook_payload(project, "launcher-thread", "startup"), environment
    ).returncode == 0

    def nested(index):
        return run_hook(
            hook_payload(project, f"nested-thread-{index}", nested_source),
            environment,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(nested, range(16)))

    requests = list(queue.glob("*.json"))
    assert all(result.returncode == 0 for result in results)
    assert len(requests) == 1
    assert json.loads(requests[0].read_text())["threadId"] == "launcher-thread"
    assert not list(queue.glob(".*.tmp.*"))


def test_later_startup_cannot_claim_an_established_launch_intent(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    queue = tmp_path / "runtime" / "codex-bind-requests"

    assert run_hook(
        hook_payload(project, "launcher-thread", "startup"), environment
    ).returncode == 0
    first = next(queue.glob("*.json"))
    first_payload = json.loads(first.read_text())
    first.unlink()

    assert run_hook(
        hook_payload(project, "unrelated-thread", "startup"), environment
    ).returncode == 0

    assert list(queue.glob("*.json")) == []
    assert first_payload["threadId"] == "launcher-thread"


def test_clear_publication_waits_for_shared_consume_guard(tmp_path):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    runtime = tmp_path / "runtime"
    queue = runtime / "codex-bind-requests"
    assert run_hook(
        hook_payload(project, "thread-before-clear", "startup"), environment
    ).returncode == 0

    with ThreadPoolExecutor(max_workers=1) as executor:
        with _rtruntime.bind_request_guard(runtime):
            future = executor.submit(
                run_hook,
                hook_payload(project, "thread-after-clear", "clear"),
                environment,
            )
            time.sleep(0.1)
            assert not future.done()
            request = next(queue.glob("*.json"))
            assert json.loads(request.read_text())["threadId"] == "thread-before-clear"
        result = future.result(timeout=5)

    assert result.returncode == 0, result.stderr
    request = next(queue.glob("*.json"))
    assert json.loads(request.read_text())["threadId"] == "thread-after-clear"
    assert not list(queue.glob(".*.tmp.*"))


def test_clear_event_coalesces_then_rebinds_same_launcher_lease(tmp_path):
    project = write_project(tmp_path / "project")
    environment, token = claim_environment(tmp_path, project)
    queue = tmp_path / "runtime" / "codex-bind-requests"
    store = wake.StateStore(tmp_path / "wake-state.json")
    client = Client(project, ["thread-before-clear", "thread-after-clear"])

    assert run_hook(
        hook_payload(project, "thread-before-clear", "startup"), environment
    ).returncode == 0
    assert run_hook(
        hook_payload(project, "thread-after-clear", "clear"), environment
    ).returncode == 0
    assert run_hook(
        hook_payload(project, "nested-after-clear", "startup"), environment
    ).returncode == 0
    requests = list(queue.glob("*.json"))
    assert len(requests) == 1
    assert json.loads(requests[0].read_text())["threadId"] == "thread-after-clear"
    assert wake.drain_bind_requests(client, store, [project], requests_dir=queue) == {
        str(project)
    }
    first_revision = store.bindings[str(project)]["bindingRevision"]
    assert store.bindings[str(project)]["threadId"] == "thread-after-clear"

    assert run_hook(
        hook_payload(project, "thread-before-clear", "clear"), environment
    ).returncode == 0
    assert wake.drain_bind_requests(client, store, [project], requests_dir=queue) == {
        str(project)
    }
    binding = store.bindings[str(project)]
    assert binding["threadId"] == "thread-before-clear"
    assert binding["roundtableSessionId"] == token.session_id
    assert binding["bindingRevision"] != first_revision


def test_concurrent_clear_intent_and_queue_finish_on_same_thread(
    tmp_path,
    monkeypatch,
):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    runtime = tmp_path / "runtime"
    queue = runtime / "codex-bind-requests"
    assert run_hook(
        hook_payload(project, "launcher-thread", "startup"), environment
    ).returncode == 0
    next(queue.glob("*.json")).unlink()

    entered_a = threading.Event()
    release_a = threading.Event()
    publish = hook._atomic_request_locked

    def controlled_publish(path, payload):
        if payload["threadId"] == "clear-a":
            entered_a.set()
            assert release_a.wait(5)
        return publish(path, payload)

    monkeypatch.setattr(hook, "_atomic_request_locked", controlled_publish)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            run_hook_in_process,
            hook_payload(project, "clear-a", "clear"),
            environment,
        )
        assert entered_a.wait(5)
        second = executor.submit(
            run_hook_in_process,
            hook_payload(project, "clear-b", "clear"),
            environment,
        )
        time.sleep(0.1)
        assert not second.done()
        release_a.set()
        assert first.result(timeout=5) == 0
        assert second.result(timeout=5) == 0

    request = json.loads(next(queue.glob("*.json")).read_text())
    intent_path = (
        runtime
        / "projects"
        / _rtruntime.project_hash(project)
        / _rtruntime.CODEX_LAUNCH_INTENT_NAME
    )
    intent = json.loads(intent_path.read_text())
    assert request["threadId"] == "clear-b"
    assert intent["activeNativeSessionId"] == "clear-b"


def test_clear_replacing_request_during_drain_never_wakes_old_thread(
    tmp_path,
    monkeypatch,
):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    queue = tmp_path / "runtime" / "codex-bind-requests"
    assert run_hook(
        hook_payload(project, "thread-before-clear", "startup"), environment
    ).returncode == 0
    message_id = "20260720T120000Z-claude-to-codex-clear-race"
    inbox = project / ".roundtable" / "inbox" / "codex" / "new"
    inbox.mkdir(parents=True)
    (inbox / f"{message_id}.md").write_text(
        f"[CLAUDE→CODEX directive id={message_id}] test\n"
    )

    client = Client(project, ["thread-before-clear", "thread-after-clear"])
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], store)
    consume = wake._consume_bind_request
    replaced = False

    def replace_with_clear_before_consume(path, identity):
        nonlocal replaced
        if not replaced:
            replaced = True
            result = run_hook(
                hook_payload(project, "thread-after-clear", "clear"),
                environment,
            )
            assert result.returncode == 0, result.stderr
        return consume(path, identity)

    monkeypatch.setattr(wake, "_consume_bind_request", replace_with_clear_before_consume)

    first = bridge.step()[0]

    assert not first.ok and "quarantined" in first.detail
    assert client.turn_threads == []
    persisted = wake.StateStore(store.path)
    assert str(project) not in persisted.bindings
    assert persisted.project_state(project)["phase"] == "BIND_QUARANTINED"
    request = next(queue.glob("*.json"))
    assert json.loads(request.read_text())["threadId"] == "thread-after-clear"

    second = bridge.step()[0]

    assert second.ok and second.detail == "wake started"
    assert client.turn_threads == ["thread-after-clear"]
    assert store.bindings[str(project)]["threadId"] == "thread-after-clear"
    assert list(queue.iterdir()) == []


def test_stale_hook_request_cannot_override_newer_lease_binding(tmp_path):
    project = write_project(tmp_path / "project")
    old_environment, old = claim_environment(tmp_path, project)
    assert run_hook(hook_payload(project, "thread-old"), old_environment).returncode == 0
    assert _rtruntime.release(old)
    new_environment, fresh = claim_environment(tmp_path, project)
    assert run_hook(hook_payload(project, "thread-new"), new_environment).returncode == 0
    store = wake.StateStore(tmp_path / "wake-state.json")
    queue = tmp_path / "runtime" / "codex-bind-requests"

    changed = wake.drain_bind_requests(
        Client(project, ["thread-old", "thread-new"]),
        store,
        [project],
        requests_dir=queue,
    )

    binding = store.bindings[str(project)]
    assert changed == {str(project)}
    assert binding["threadId"] == "thread-new"
    assert binding["roundtableSessionId"] == fresh.session_id
    assert binding["roundtableSessionId"] != old.session_id
    assert list(queue.iterdir()) == []


def test_malformed_request_is_consumed_but_symlink_is_never_followed(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    queue = runtime / "codex-bind-requests"
    queue.mkdir(mode=0o700)
    malformed = queue / "malformed.json"
    malformed.write_text("not json")
    malformed.chmod(0o600)
    outside = tmp_path / "outside.json"
    outside.write_text("do not read")
    unsafe = queue / "unsafe.json"
    unsafe.symlink_to(outside)
    store = wake.StateStore(tmp_path / "wake-state.json")

    changed = wake.drain_bind_requests(
        Client(tmp_path, []), store, [], requests_dir=queue
    )

    assert changed == set()
    assert not malformed.exists()
    assert unsafe.is_symlink()
    assert outside.read_text() == "do not read"
    assert store.bindings == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", []),
        ("source", "\ud800"),
        ("projectRoot", "~roundtable-user-that-does-not-exist/project"),
    ],
)
def test_malformed_private_request_fields_are_consumed_without_crashing(
    tmp_path,
    field,
    value,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    queue = runtime / "codex-bind-requests"
    queue.mkdir(mode=0o700)
    request = queue / "malformed-field.json"
    payload = {
        "schema": wake.BIND_REQUEST_SCHEMA,
        "hookEventName": "SessionStart",
        "source": "startup",
        "projectRoot": str(tmp_path / "project"),
        "agentId": "codex",
        "roundtableSessionId": "session",
        "leaseRevision": "revision",
        "threadId": "thread",
        "createdAt": "2026-07-20T00:00:00Z",
    }
    payload[field] = value
    request.write_text(json.dumps(payload))
    request.chmod(0o600)
    store = wake.StateStore(tmp_path / "wake-state.json")

    changed = wake.drain_bind_requests(
        Client(tmp_path, []),
        store,
        [],
        requests_dir=queue,
    )

    assert changed == set()
    assert not request.exists()
    assert store.bindings == {}


def test_project_replaced_by_symlink_loop_rejects_request_without_crashing_bridge(
    tmp_path,
):
    project = write_project(tmp_path / "project")
    environment, _token = claim_environment(tmp_path, project)
    queue = tmp_path / "runtime" / "codex-bind-requests"
    assert run_hook(hook_payload(project), environment).returncode == 0
    request = next(queue.glob("*.json"))

    moved = tmp_path / "project-before-loop"
    project.rename(moved)
    project.symlink_to(project, target_is_directory=True)
    store = wake.StateStore(tmp_path / "wake-state.json")

    changed = wake.drain_bind_requests(
        Client(moved, ["thread-1"]),
        store,
        [project],
        requests_dir=queue,
    )

    assert changed == set()
    assert not request.exists()
    assert store.bindings == {}


def test_canonical_project_wraps_symlink_loop_as_identity_error(tmp_path):
    loop = tmp_path / "project-loop"
    loop.symlink_to(loop, target_is_directory=True)

    with pytest.raises(wake.IdentityError, match="cannot resolve project root"):
        wake.canonical_project(loop)


def test_hook_trust_gate_ignores_unresolvable_cwd_instead_of_crashing(tmp_path):
    project = write_project(tmp_path / "project")

    class InvalidCwdClient:
        def request(self, method, params):
            assert method == "hooks/list"
            return {
                "data": [
                    {
                        "cwd": "~roundtable-user-that-does-not-exist/project",
                        "hooks": [],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }

    bridge = wake.WakeBridge(
        InvalidCwdClient(),
        [project],
        wake.StateStore(tmp_path / "wake-state.json"),
    )

    with pytest.raises(wake.IdentityError, match="found 0"):
        bridge._hook_trust_gate(project)


# --- intent-fenced remote-thread discovery ---------------------------------
#
# Codex 0.145.0 does not dispatch SessionStart hooks on the `--remote`
# app-server path, so a Roundtable-managed seat never self-registers. The
# bridge instead selects a remote thread by proving it unique inside the
# launcher's own launch-intent creation window, then claims that single-use
# intent. These tests pin the fences that make that safe.


def uuid7(instant: datetime) -> str:
    """Build a UUIDv7 whose embedded creation time is ``instant``."""
    milliseconds = int(instant.timestamp() * 1000)
    value = ((milliseconds & ((1 << 48) - 1)) << 80) | random.getrandbits(80)
    value &= ~(0xF << 76)
    value |= 7 << 76
    value &= ~(0b11 << 62)
    value |= 0b10 << 62
    return str(uuid.UUID(int=value))


def intent_path(tmp_path: Path, project: Path) -> Path:
    return (
        tmp_path
        / "runtime"
        / "projects"
        / _rtruntime.project_hash(project)
        / _rtruntime.CODEX_LAUNCH_INTENT_NAME
    )


def read_intent(tmp_path: Path, project: Path) -> dict:
    return json.loads(intent_path(tmp_path, project).read_text())


def write_intent(tmp_path: Path, project: Path, payload: dict) -> None:
    path = intent_path(tmp_path, project)
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def armed_at(tmp_path: Path, project: Path) -> datetime:
    return datetime.fromisoformat(
        read_intent(tmp_path, project)["armedAt"].replace("Z", "+00:00")
    )


def rearm_at(tmp_path: Path, project: Path, instant: datetime) -> None:
    payload = read_intent(tmp_path, project)
    payload["armedAt"] = instant.isoformat().replace("+00:00", "Z")
    write_intent(tmp_path, project, payload)


def local_thread(project: Path, thread_id: str) -> dict:
    thread = selected_thread(project, thread_id)
    thread["source"] = "cli"
    return thread


class DiscoveryClient(Client):
    """A Client that also answers the discovery listing."""

    def request(self, method, params):
        if method == "thread/loaded/list":
            self.calls.append((method, params))
            return {"data": list(self.threads)}
        return super().request(method, params)


def logged_events(tmp_path: Path, event: str) -> list[dict]:
    log = tmp_path / "runtime" / "rt-codex-wake.log.jsonl"
    if not log.exists():
        return []
    return [
        record
        for record in (json.loads(line) for line in log.read_text().splitlines() if line)
        if record["event"] == event
    ]


def add_mail(project: Path, message_id: str = "20260726T000000Z-claude-to-codex-x") -> None:
    inbox = project / ".roundtable" / "inbox" / "codex" / "new"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{message_id}.md").write_text(
        f"[CLAUDE→CODEX directive id={message_id}] test\n"
    )


def test_in_window_remote_thread_is_discovered_and_bound(tmp_path):
    project = write_project(tmp_path / "project")
    _environment, token = claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    client = DiscoveryClient(project, [thread_id])
    store = wake.StateStore(tmp_path / "wake-state.json")

    selected = wake.WakeBridge(client, [project], store)._thread_for(project)

    binding = store.bindings[str(project)]
    assert selected["id"] == thread_id
    assert binding["threadId"] == thread_id
    assert binding["roundtableSessionId"] == token.session_id
    assert binding["leaseRevision"] == token.revision
    assert read_intent(tmp_path, project)["activeNativeSessionId"] == thread_id
    bound = logged_events(tmp_path, "intent_discovery_bound")
    assert len(bound) == 1
    assert bound[0]["remote_candidates"] == 1 and bound[0]["cli_candidates"] == 0


def test_remote_thread_created_after_the_window_is_refused(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=31))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="found 0"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_remote_thread_created_before_the_skew_is_refused(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) - timedelta(seconds=2))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="found 0"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_legacy_local_cli_discovery_is_unchanged_without_an_intent(tmp_path):
    project = write_project(tmp_path / "project")
    client = DiscoveryClient(project, ["thread-1"])
    client.threads["thread-1"] = local_thread(project, "thread-1")
    store = wake.StateStore(tmp_path / "wake-state.json")

    selected = wake.WakeBridge(
        client, [project], store, auto_discover=True
    )._thread_for(project)

    assert selected["id"] == "thread-1"
    assert store.bindings[str(project)]["threadId"] == "thread-1"
    assert "roundtableSessionId" not in store.bindings[str(project)]
    assert logged_events(tmp_path, "intent_discovery_bound") == []


def test_eager_step_binds_a_seat_whose_inbox_is_still_empty(tmp_path):
    project = write_project(tmp_path / "project")
    _environment, token = claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=1.2))
    client = DiscoveryClient(project, [thread_id])
    store = wake.StateStore(tmp_path / "wake-state.json")

    results = wake.WakeBridge(client, [project], store).step()

    assert results[0].ok and results[0].detail == "empty"
    assert store.bindings[str(project)]["threadId"] == thread_id
    assert store.bindings[str(project)]["roundtableSessionId"] == token.session_id
    assert client.turn_threads == []


def test_non_uuidv7_thread_id_never_falls_back_to_cwd_and_source(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, ["thread-1"]), [project], store)

    with pytest.raises(wake.IdentityError, match="found 0"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_foreign_same_cwd_thread_outside_the_window_is_never_bound(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    foreign = uuid7(armed_at(tmp_path, project) - timedelta(minutes=10))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [foreign]), [project], store)

    with pytest.raises(wake.IdentityError, match="found 0"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_two_in_window_remote_threads_refuse_instead_of_tie_breaking(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    base = armed_at(tmp_path, project)
    first = uuid7(base + timedelta(seconds=0.4))
    second = uuid7(base + timedelta(seconds=2.0))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(
        DiscoveryClient(project, [first, second]), [project], store
    )

    with pytest.raises(wake.IdentityError) as raised:
        bridge._thread_for(project)

    message = str(raised.value)
    assert "found 2" in message and first in message and second in message
    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_superseded_seat_lease_cannot_be_claimed_by_discovery(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    payload = read_intent(tmp_path, project)
    payload["leaseRevision"] = uuid.uuid4().hex
    write_intent(tmp_path, project, payload)
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="could not be claimed"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_dead_seat_owner_cannot_be_claimed_by_discovery(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    _environment, token = claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    observed = _rtruntime._pid_state
    monkeypatch.setattr(
        _rtruntime,
        "_pid_state",
        lambda pid: "dead" if pid == token.owner_pid else observed(pid),
    )
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="could not be claimed"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_ambiguous_seat_owner_never_becomes_a_binding(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    _environment, token = claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    observed = _rtruntime._pid_state
    monkeypatch.setattr(
        _rtruntime,
        "_pid_state",
        lambda pid: "ambiguous" if pid == token.owner_pid else observed(pid),
    )
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="cannot be inspected"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_relaunch_between_selection_and_claim_invalidates_the_snapshot(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    stale = _rtruntime.read_codex_launch_intent(project)
    thread_id = uuid7(stale.armed_at + timedelta(seconds=0.5))

    # A relaunch republishes the intent under a new lease while the bridge is
    # still holding the snapshot it selected the thread against.
    assert _rtruntime.release(_rtruntime.load_validated_lease(
        project, stale.agent_id, stale.session_id, stale.revision
    ))
    fresh_token = _rtruntime.claim(project, "codex", "codex")
    _rtruntime.arm_codex_launch_intent(fresh_token)

    assert _rtruntime.resolve_codex_launch_intent(
        project, thread_id, "discover", expect=stale
    ) is None
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_second_thread_cannot_steal_an_established_intent_through_discover(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    first = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.2))
    second = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.4))

    assert _rtruntime.resolve_codex_launch_intent(project, first, "discover") is not None
    assert _rtruntime.resolve_codex_launch_intent(project, second, "discover") is None
    assert read_intent(tmp_path, project)["activeNativeSessionId"] == first


def test_discover_never_holds_the_clear_privilege(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    first = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.2))
    replacement = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.4))
    assert _rtruntime.resolve_codex_launch_intent(project, first, "discover") is not None

    assert (
        _rtruntime.resolve_codex_launch_intent(project, replacement, "discover") is None
    )
    assert read_intent(tmp_path, project)["activeNativeSessionId"] == first
    # `clear` is the only source that may move an established intent.
    assert (
        _rtruntime.resolve_codex_launch_intent(project, replacement, "clear") is not None
    )
    assert read_intent(tmp_path, project)["activeNativeSessionId"] == replacement


def test_thread_bound_to_another_project_is_refused_in_window(tmp_path):
    other = write_project(tmp_path / "other")
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    store = wake.StateStore(tmp_path / "wake-state.json")
    store.bind(other, selected_thread(other, thread_id))
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="already bound to"):
        bridge._thread_for(project)

    assert str(project) not in store.bindings


def test_expired_launch_intent_is_not_claimable_by_discovery(tmp_path):
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    expired = datetime.now(timezone.utc) - timedelta(seconds=301)
    rearm_at(tmp_path, project, expired)
    thread_id = uuid7(expired + timedelta(seconds=0.5))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)

    with pytest.raises(wake.IdentityError, match="could not be claimed"):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_stable_discovery_refusal_logs_once_per_distinct_outcome(tmp_path, monkeypatch):
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(wake.time, "monotonic", lambda: clock["now"])
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    base = armed_at(tmp_path, project)
    outside = uuid7(base - timedelta(minutes=10))
    client = DiscoveryClient(project, [outside])
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], store)

    for _ in range(3):
        bridge.step()

    rejected = logged_events(tmp_path, "intent_discovery_rejected")
    assert len(rejected) == 1
    assert rejected[0]["reason"] == "no_candidates"
    assert rejected[0]["candidate_ids"] == []

    clock["now"] += wake.CODEX_DISCOVERY_RETRY_SECONDS + 1
    first = uuid7(base + timedelta(seconds=0.4))
    second = uuid7(base + timedelta(seconds=0.6))
    client.threads = {
        thread_id: selected_thread(project, thread_id)
        for thread_id in (first, second)
    }
    bridge.step()
    clock["now"] += wake.CODEX_DISCOVERY_RETRY_SECONDS + 1
    bridge.step()

    rejected = logged_events(tmp_path, "intent_discovery_rejected")
    assert len(rejected) == 2
    assert rejected[1]["reason"] == "ambiguous"
    assert rejected[1]["candidate_ids"] == sorted([first, second])
    assert store.bindings == {}


def test_intent_does_not_grant_the_cwd_only_local_cli_privilege(tmp_path):
    """An armed intent must not smuggle in the --auto-discover privilege.

    The local CLI class is fenced only by the project cwd, which is why it is
    opt-in.  A project that armed an intent has proven something about one
    remote thread, not about every local thread sharing its directory.
    """

    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    client = DiscoveryClient(project, ["thread-1"])
    client.threads["thread-1"] = local_thread(project, "thread-1")
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=False)

    with pytest.raises(wake.DiscoveryRefused):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_armed_intent_preempts_the_local_class_even_under_auto_discover(tmp_path):
    """An anchor beats the cwd-only class rather than competing with it.

    The local class has no window and no intent requirement, so if it could win
    while an intent is armed it would capture the seat's mail permanently and
    the remote thread the launcher actually meant would never be bound.
    """

    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    client = DiscoveryClient(project, ["thread-1"])
    client.threads["thread-1"] = local_thread(project, "thread-1")
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)

    with pytest.raises(wake.DiscoveryRefused):
        bridge._thread_for(project)

    assert store.bindings == {}
    assert read_intent(tmp_path, project)["activeNativeSessionId"] is None


def test_relaunch_supersedes_the_previous_binding_and_rebinds(tmp_path):
    """A relaunch is the documented repair, so it must actually rebind.

    Nothing else evicts a binding whose lease has been replaced, so without
    supersession the seat stays pinned to the dead thread forever and every
    poll re-fails the same identity check.
    """

    project = write_project(tmp_path / "project")
    _environment, first = claim_environment(tmp_path, project)
    store = wake.StateStore(tmp_path / "wake-state.json")

    # A binding from an earlier launch, whose thread predates the relaunch.
    stale_id = uuid7(datetime.now(timezone.utc) - timedelta(hours=1))
    store.bind(project, selected_thread(project, stale_id), lease=first)
    # Age it on disk: StateStore.save() re-reads the file and replaces the
    # bindings map wholesale, so mutating the in-memory dict would be silently
    # discarded and the test would only prove a sub-millisecond gap.
    aged = json.loads((tmp_path / "wake-state.json").read_text())
    aged["bindings"][str(project)]["boundAt"] = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    (tmp_path / "wake-state.json").write_text(json.dumps(aged))

    # Relaunch: the previous TUI exited, so its recorded owner is gone and the
    # lease is reclaimable.  Mark the stored record dead rather than the pid,
    # because both claims legitimately run under this test process.
    lease_path = _rtruntime.seat_paths(project, "codex").lease
    record = json.loads(lease_path.read_text())
    record["ownerPid"] = 2**31 - 1
    lease_path.write_text(json.dumps(record))
    _environment, second = claim_environment(tmp_path, project)
    assert second.revision != first.revision

    fresh_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    client = DiscoveryClient(project, [stale_id, fresh_id])
    reloaded = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], reloaded)

    assert bridge._thread_for(project)["id"] == fresh_id
    rebound = reloaded.bindings[str(project)]
    assert rebound["threadId"] == fresh_id
    assert rebound["leaseRevision"] == second.revision
    assert len(logged_events(tmp_path, "binding_superseded")) == 1


def test_a_binding_is_not_superseded_by_its_own_intent(tmp_path):
    """Supersession must require an intent strictly newer than the binding."""

    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)
    bridge._thread_for(project)

    # Re-reading the same binding must not evict it, however many polls run.
    for _ in range(3):
        assert bridge._thread_for(project)["id"] == thread_id
    assert store.bindings[str(project)]["threadId"] == thread_id
    assert logged_events(tmp_path, "binding_superseded") == []


def test_unrepresentable_uuidv7_creation_time_is_an_identity_error(tmp_path):
    """A 48-bit timestamp outside the date range is unusable, not bridge-fatal."""

    overflowing = str(uuid.UUID(int=(((1 << 48) - 1) << 80) | (7 << 76) | (0b10 << 62)))
    with pytest.raises(wake.IdentityError, match="unrepresentable creation time"):
        wake._thread_creation_instant(overflowing)


def test_sweep_backs_off_before_re_spending_discovery_rpcs(tmp_path, monkeypatch):
    """A refusing project must not re-list every loaded thread on every poll.

    Bridge iterations are paced by notification bursts rather than by the poll
    interval, so an unbacked-off sweep amplifies one unbindable seat into
    unbounded app-server traffic.
    """

    clock = {"now": time.monotonic()}
    monkeypatch.setattr(wake.time, "monotonic", lambda: clock["now"])
    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    outside = uuid7(armed_at(tmp_path, project) - timedelta(minutes=10))
    client = DiscoveryClient(project, [outside])
    bridge = wake.WakeBridge(
        client, [project], wake.StateStore(tmp_path / "wake-state.json")
    )

    bridge.step()
    listings = [call for call in client.calls if call[0] == "thread/loaded/list"]
    assert len(listings) == 1

    for _ in range(5):
        bridge.step()
    assert [call for call in client.calls if call[0] == "thread/loaded/list"] == listings

    clock["now"] += wake.CODEX_DISCOVERY_RETRY_SECONDS + 1
    bridge.step()
    assert len([c for c in client.calls if c[0] == "thread/loaded/list"]) == 2


def test_relaunch_is_not_delayed_by_the_previous_launch_backoff(tmp_path):
    """Backoff is scoped to one launch, so a relaunch binds on the next poll."""

    project = write_project(tmp_path / "project")
    _environment, first = claim_environment(tmp_path, project)
    outside = uuid7(armed_at(tmp_path, project) - timedelta(minutes=10))
    client = DiscoveryClient(project, [outside])
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], store)

    bridge.step()
    assert store.bindings == {}

    lease_path = _rtruntime.seat_paths(project, "codex").lease
    record = json.loads(lease_path.read_text())
    record["ownerPid"] = 2**31 - 1
    lease_path.write_text(json.dumps(record))
    _environment, second = claim_environment(tmp_path, project)
    assert second.revision != first.revision

    fresh = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    client.threads = {
        thread_id: selected_thread(project, thread_id) for thread_id in (outside, fresh)
    }
    bridge.step()

    assert store.bindings[str(project)]["threadId"] == fresh


def test_eager_sweep_rebinds_a_relaunched_seat_through_step(tmp_path):
    """Cover the sweep's own supersession guard, not just _thread_for.

    The guard lives on a different code path from the mail-driven bind, so
    without this the whole relaunch repair can be deleted with a green suite.
    """

    project = write_project(tmp_path / "project")
    _environment, first = claim_environment(tmp_path, project)
    store = wake.StateStore(tmp_path / "wake-state.json")
    stale_id = uuid7(datetime.now(timezone.utc) - timedelta(hours=1))
    store.bind(project, selected_thread(project, stale_id), lease=first)

    aged = json.loads((tmp_path / "wake-state.json").read_text())
    aged["bindings"][str(project)]["boundAt"] = (
        (datetime.now(timezone.utc) - timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z")
    )
    (tmp_path / "wake-state.json").write_text(json.dumps(aged))

    lease_path = _rtruntime.seat_paths(project, "codex").lease
    record = json.loads(lease_path.read_text())
    record["ownerPid"] = 2**31 - 1
    lease_path.write_text(json.dumps(record))
    _environment, second = claim_environment(tmp_path, project)

    fresh_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    client = DiscoveryClient(project, [stale_id, fresh_id])
    reloaded = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(client, [project], reloaded)

    # No mail anywhere: only the eager sweep can produce this binding.
    bridge.step()

    rebound = reloaded.bindings[str(project)]
    assert rebound["threadId"] == fresh_id
    assert rebound["leaseRevision"] == second.revision


def test_discovery_rebind_clears_the_bind_quarantine_latch(tmp_path):
    """The latch waits for a replacement SessionStart request that never comes.

    On the remote Codex path no hook fires, so if only a consumed bind request
    could release the latch, a seat quarantined once would keep answering
    "binding quarantined" forever even after discovery rebound it.
    """

    project = write_project(tmp_path / "project")
    claim_environment(tmp_path, project)
    thread_id = uuid7(armed_at(tmp_path, project) + timedelta(seconds=0.5))
    store = wake.StateStore(tmp_path / "wake-state.json")
    bridge = wake.WakeBridge(DiscoveryClient(project, [thread_id]), [project], store)
    bridge.bind_quarantined.add(str(project))

    results = bridge.step()

    assert str(project) not in bridge.bind_quarantined
    assert store.bindings[str(project)]["threadId"] == thread_id
    assert not any("quarantined" in (result.detail or "") for result in results)
