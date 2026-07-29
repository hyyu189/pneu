from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtruntime
from _rtlib import register_project, resolve_project_mailbox


def load_script(name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(BIN / name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module


wake = load_script("rt-codex-wake", "rt_codex_lease_wake")


@pytest.fixture(autouse=True)
def isolate_project_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))


def write_project(path: Path, *, agent_id: str = "codex") -> Path:
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
        f"      - id: {agent_id}\n"
    )
    register_project(project)
    return project


def claim_codex(
    monkeypatch,
    tmp_path: Path,
    project: Path,
    *,
    agent_id: str = "codex",
):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    token = _rtruntime.claim(project, agent_id, "codex")
    monkeypatch.setenv("RT_PROJECT_ROOT", str(project))
    monkeypatch.setenv("RT_FROM", agent_id)
    monkeypatch.setenv("RT_SESSION_ID", token.session_id)
    monkeypatch.setenv("RT_LEASE_REVISION", token.revision)
    return token


def thread(project: Path, thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "sessionId": "native-session-1",
        "cwd": str(project),
        "source": "cli",
        "threadSource": None,
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": "idle"},
    }


class BridgeClient:
    def __init__(self, selected_thread: dict):
        self.selected_thread = selected_thread
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": [self.selected_thread["id"]]}
        if method in {"thread/read", "thread/resume"}:
            return {"thread": dict(self.selected_thread)}
        if method == "hooks/list":
            return {
                "data": [
                    {
                        "cwd": self.selected_thread["cwd"],
                        "hooks": [],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        if method == "turn/start":
            return {"turn": {"id": "wake-turn-1"}}
        raise AssertionError(method)


def test_state_store_persists_and_validates_roundtable_lease(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    token = claim_codex(monkeypatch, tmp_path, project)
    store = wake.StateStore(tmp_path / "wake-state.json")

    store.bind(project, thread(project), lease=token)

    binding = wake.StateStore(store.path).bindings[str(project)]
    assert binding["agent"] == "codex"
    assert binding["roundtableSessionId"] == token.session_id
    assert binding["leaseRevision"] == token.revision
    assert wake._binding_lease(project, binding).session_id == token.session_id


def test_legacy_binding_is_rejected_after_any_seat_claim(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    store = wake.StateStore(tmp_path / "wake-state.json")
    store.bind(project, thread(project))
    binding = store.bindings[str(project)]

    assert wake._binding_lease(project, binding) is None
    _rtruntime.claim(project, "codex", "codex")

    with pytest.raises(wake.IdentityError, match="legacy binding is superseded"):
        wake._binding_lease(project, binding)


def test_legacy_bind_is_rejected_by_active_custom_codex_seat(
    tmp_path, monkeypatch
):
    project = write_project(
        tmp_path / "project",
        agent_id="codex-review",
    )
    claim_codex(
        monkeypatch,
        tmp_path,
        project,
        agent_id="codex-review",
    )
    for name in (
        "RT_PROJECT_ROOT",
        "RT_FROM",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(wake.IdentityError, match="legacy bind is superseded"):
        wake._lease_environment(project)


def test_bind_environment_must_match_current_fenced_seat(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    token = claim_codex(monkeypatch, tmp_path, project)

    assert wake._lease_environment(project).session_id == token.session_id
    monkeypatch.setenv("RT_SESSION_ID", "old-session")

    with pytest.raises(wake.IdentityError, match="lease validation failed"):
        wake._lease_environment(project)


def test_successful_bridge_iteration_refreshes_only_current_seat(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    token = claim_codex(monkeypatch, tmp_path, project)
    store = wake.StateStore(tmp_path / "wake-state.json")
    store.bind(project, thread(project), lease=token)

    assert _rtruntime.inspect_seat(project, "codex").status == "active_unhealthy"
    wake.heartbeat_bound_seats(
        store,
        [project],
        [wake.ProjectResult(True, "empty")],
    )

    inspection = _rtruntime.inspect_seat(project, "codex")
    assert inspection.status == "active_healthy"
    assert inspection.token.native_session_id == "thread-1"

    paths = _rtruntime.seat_paths(project, "codex")
    before = paths.lease.read_bytes()
    wake.heartbeat_bound_seats(
        store,
        [project],
        [wake.ProjectResult(False, "identity error")],
    )
    assert paths.lease.read_bytes() == before

    updates = []
    monkeypatch.setattr(
        wake,
        "update_wake",
        lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    wake.heartbeat_bound_seats(
        store,
        [project],
        [wake.ProjectResult(False, "mailbox layout busy", heartbeat=True)],
    )
    assert len(updates) == 1
    assert updates[0][0][:4] == (
        project,
        "codex",
        token.session_id,
        token.revision,
    )


def test_bind_command_records_current_lease_and_native_thread(
    tmp_path, monkeypatch, capsys
):
    project = write_project(tmp_path / "project")
    token = claim_codex(monkeypatch, tmp_path, project)
    state_file = tmp_path / "wake-state.json"
    selected_thread = thread(project)

    class Client:
        def close(self):
            pass

    monkeypatch.setattr(
        wake, "require_supported_version", lambda: (0, 144, 6)
    )
    monkeypatch.setattr(
        wake,
        "ensure_daemon",
        lambda _socket: pytest.fail("bind must not repair or reload the daemon"),
        raising=False,
    )
    monkeypatch.setattr(wake, "require_supported_daemon", lambda _socket: None)
    monkeypatch.setattr(wake, "AppServerClient", lambda _socket: Client())
    monkeypatch.setattr(
        wake,
        "read_thread",
        lambda _client, thread_id: (
            selected_thread if thread_id == selected_thread["id"] else None
        ),
    )

    result = wake.bind_command(
        SimpleNamespace(
            project=str(project),
            thread_id=selected_thread["id"],
            socket=tmp_path / "app.sock",
            state_file=state_file,
        )
    )

    assert result == 0
    binding = wake.StateStore(state_file).bindings[str(project)]
    assert binding["roundtableSessionId"] == token.session_id
    assert binding["leaseRevision"] == token.revision
    inspection = _rtruntime.inspect_seat(project, "codex")
    assert inspection.status == "active_healthy"
    assert inspection.token.native_session_id == selected_thread["id"]
    assert "bound project=" in capsys.readouterr().out


def test_auto_discovery_persists_binding_inside_seat_guard(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    claim_codex(monkeypatch, tmp_path, project)
    store = wake.StateStore(tmp_path / "wake-state.json")
    selected_thread = thread(project)
    client = BridgeClient(selected_thread)
    real_guard = wake.seat_shared_guard
    guard_active = []
    observed_bind = store.bind_if_absent

    @contextmanager
    def tracking_guard(*args, **kwargs):
        with real_guard(*args, **kwargs) as current:
            guard_active.append(True)
            try:
                yield current
            finally:
                guard_active.pop()

    def guarded_bind(*args, **kwargs):
        assert guard_active
        return observed_bind(*args, **kwargs)

    monkeypatch.setattr(wake, "seat_shared_guard", tracking_guard)
    monkeypatch.setattr(store, "bind_if_absent", guarded_bind)

    selected = wake.WakeBridge(
        client,
        [project],
        store,
        auto_discover=True,
    )._thread_for(project)

    assert selected["id"] == selected_thread["id"]
    assert store.bindings[str(project)]["roundtableSessionId"]


def test_wake_turn_start_finishes_before_stale_seat_reclaim(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(wake, "RUNTIME_DIR", tmp_path / "wake-runtime")
    starts = {101: "owner-101", 102: "owner-102"}
    monkeypatch.setattr(
        _rtruntime,
        "process_start_fingerprint",
        lambda pid: starts.get(pid),
    )
    monkeypatch.setattr(
        _rtruntime,
        "_pid_state",
        lambda pid: "dead" if starts.get(pid) is None else "live",
    )
    old = _rtruntime.claim(project, "codex", "codex", owner_pid=101)
    store = wake.StateStore(tmp_path / "wake-state.json")
    selected_thread = thread(project)
    store.bind(project, selected_thread, lease=old)
    binding = store.bindings[str(project)]
    reclaim_started = threading.Event()
    reclaim_finished = threading.Event()
    replacement = []
    failures = []

    def reclaim():
        reclaim_started.set()
        try:
            replacement.append(
                _rtruntime.claim(
                    project,
                    "codex",
                    "codex",
                    owner_pid=102,
                )
            )
        except Exception as error:  # pragma: no cover - failure is asserted below
            failures.append(error)
        finally:
            reclaim_finished.set()

    class RacingClient(BridgeClient):
        def request(self, method, params):
            if method == "turn/start":
                self.calls.append((method, params))
                starts[101] = None
                worker = threading.Thread(target=reclaim)
                worker.start()
                assert reclaim_started.wait(1)
                assert not reclaim_finished.wait(0.1)
                self.worker = worker
                return {"turn": {"id": "wake-turn-1"}}
            return super().request(method, params)

    client = RacingClient(selected_thread)
    started = wake.WakeBridge(client, [project], store)._wake(
        project,
        selected_thread["id"],
        "generation-1",
        1,
        expected_binding_revision=binding["bindingRevision"],
    )

    assert started
    assert reclaim_finished.wait(2)
    client.worker.join(timeout=2)
    assert failures == []
    assert replacement and replacement[0].session_id != old.session_id


def test_legacy_wake_finishes_before_first_unified_seat_claim(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setattr(wake, "RUNTIME_DIR", tmp_path / "wake-runtime")
    store = wake.StateStore(tmp_path / "wake-state.json")
    selected_thread = thread(project)
    store.bind(project, selected_thread)
    binding = store.bindings[str(project)]
    claim_started = threading.Event()
    claim_finished = threading.Event()
    claimed = []
    failures = []

    def first_claim():
        claim_started.set()
        try:
            claimed.append(_rtruntime.claim(project, "codex", "codex"))
        except Exception as error:  # pragma: no cover - failure is asserted below
            failures.append(error)
        finally:
            claim_finished.set()

    class RacingClient(BridgeClient):
        def request(self, method, params):
            if method == "turn/start":
                self.calls.append((method, params))
                worker = threading.Thread(target=first_claim)
                worker.start()
                assert claim_started.wait(1)
                assert not claim_finished.wait(0.1)
                self.worker = worker
                return {"turn": {"id": "wake-turn-1"}}
            return super().request(method, params)

    client = RacingClient(selected_thread)
    started = wake.WakeBridge(client, [project], store)._wake(
        project,
        selected_thread["id"],
        "generation-1",
        1,
        expected_binding_revision=binding["bindingRevision"],
    )

    assert started
    assert claim_finished.wait(2)
    client.worker.join(timeout=2)
    assert failures == []
    assert claimed and claimed[0].agent_id == "codex"


def test_custom_codex_instance_scans_and_wakes_its_own_mailbox(
    tmp_path, monkeypatch
):
    agent_id = "codex-review"
    project = write_project(tmp_path / "project", agent_id=agent_id)
    token = claim_codex(
        monkeypatch,
        tmp_path,
        project,
        agent_id=agent_id,
    )
    selected_thread = thread(project)
    store = wake.StateStore(tmp_path / "wake-state.json")
    store.bind(project, selected_thread, lease=token)
    inbox = resolve_project_mailbox(project).inbox_dir / agent_id / "new"
    inbox.mkdir(parents=True)
    message_id = "20260719T120000Z-claude-to-codex-review-1"
    (inbox / f"{message_id}.md").write_text(
        f"[CLAUDE→CODEX-REVIEW question id={message_id}] test\n"
    )
    client = BridgeClient(selected_thread)

    result = wake.WakeBridge(client, [project], store).step()[0]

    starts = [params for method, params in client.calls if method == "turn/start"]
    assert result.ok and result.detail == "wake started"
    assert len(starts) == 1
    wake_text = starts[0]["input"][0]["text"]
    assert "drain the Roundtable inbox for codex-review in this project" in wake_text
    assert str(inbox) not in wake_text


def test_binding_agent_change_after_scan_requires_rescan_without_wake(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    store = wake.StateStore(tmp_path / "wake-state.json")
    old_thread = thread(project)
    store.bind(project, old_thread)
    inbox = resolve_project_mailbox(project).inbox_dir / "codex" / "new"
    inbox.mkdir(parents=True)
    message_id = "20260719T120001Z-claude-to-codex-1"
    (inbox / f"{message_id}.md").write_text(
        f"[CLAUDE→CODEX question id={message_id}] test\n"
    )
    client = BridgeClient(old_thread)
    bridge = wake.WakeBridge(client, [project], store)
    new_thread = thread(project, "thread-2")

    def rebind_during_scan(_project):
        store.bind(
            project,
            new_thread,
            lease=SimpleNamespace(
                agent_id="codex-review",
                session_id="replacement-session",
                revision="replacement-revision",
            ),
        )
        return new_thread

    monkeypatch.setattr(bridge, "_thread_for", rebind_during_scan)

    result = bridge.step()[0]

    assert not result.ok
    assert result.detail == "binding agent changed; rescan required"
    assert not any(method == "turn/start" for method, _params in client.calls)
