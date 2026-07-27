from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
