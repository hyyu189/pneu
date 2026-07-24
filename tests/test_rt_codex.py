import base64
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import socket
import stat
import struct
import subprocess
import sys
import threading
from argparse import Namespace
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtcodex


def load_wake_module():
    name = "rt_codex_wake_test_module"
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / "rt-codex-wake"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


wake = load_wake_module()


def load_daemon_module():
    name = "rt_codex_daemon_test_module"
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / "rt-codex-daemon"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


daemon = load_daemon_module()


def load_doctor_module():
    name = "rt_doctor_test_module"
    loader = importlib.machinery.SourceFileLoader(name, str(BIN / "rt-doctor"))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


doctor = load_doctor_module()


@pytest.fixture(autouse=True)
def isolate_wake_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(wake, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setenv("RT_PROJECTS_FILE", str(tmp_path / "projects.yaml"))


def write_project(path: Path) -> Path:
    state = path / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        f"""schema: roundtable.agents.v1
project: {path.resolve()}
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
    return path.resolve()


def test_project_config_parser_preserves_yaml_comment_characters(tmp_path):
    project = tmp_path / "project # notes"
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {json.dumps(str(project.resolve()))}\n"
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
    )

    assert wake.project_config_has_codex(project.resolve()) is True


def test_project_config_resolves_relative_project_from_config_root(
    tmp_path, monkeypatch
):
    project = tmp_path / "portable-project"
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        'project: "."\n'
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
    )
    elsewhere = tmp_path / "wake-launchd-cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert wake.project_config_has_codex(project.resolve()) is True


def test_project_config_rejects_relative_project_outside_config_root(tmp_path):
    project = tmp_path / "configured-project"
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        'project: "../other"\n'
        "agents:\n"
        "  codex:\n"
        "    harness: codex\n"
    )

    assert wake.project_config_has_codex(project.resolve()) is False


def add_mail(project: Path, msg_id: str) -> Path:
    inbox = project / ".roundtable" / "inbox" / "codex" / "new"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{msg_id}.md"
    path.write_text(f"[CLAUDE→CODEX directive id={msg_id}] test")
    return path


def test_needs_human_notification_falls_back_to_macos_without_cmux(
    tmp_path,
    monkeypatch,
):
    project = write_project(tmp_path / "project")
    events = []
    commands = []

    monkeypatch.setattr(
        wake.shutil,
        "which",
        lambda name: "/usr/bin/osascript" if name == "osascript" else None,
    )
    monkeypatch.setattr(
        wake,
        "log_event",
        lambda event, **fields: events.append((event, fields)),
    )

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(wake.subprocess, "run", run)

    wake.notify_needs_human(
        project,
        "thread-1",
        "generation-1",
        "turn-1",
        "approval",
        "approval required",
    )

    assert commands and commands[0][0] == "/usr/bin/osascript"
    assert [event for event, _fields in events] == [
        "wake_needs_human",
        "needs_human_notification_sent",
    ]
    assert events[-1][1]["provider"] == "macos"


def thread(project: Path, status: str = "idle", thread_id: str = "thread-1") -> dict:
    return {
        "id": thread_id,
        "sessionId": "session-1",
        "cwd": str(project),
        "source": "cli",
        "parentThreadId": None,
        "ephemeral": False,
        "status": {"type": status},
    }


class FakeClient:
    def __init__(self, value: dict):
        self.value = value
        self.calls = []
        self.turn_count = 0

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/loaded/list":
            return {"data": [self.value["id"]]}
        if method == "thread/read":
            return {"thread": dict(self.value)}
        if method == "thread/resume":
            return {"thread": dict(self.value)}
        if method == "thread/turns/list":
            return {"data": list(self.value.get("turns") or [])}
        if method == "hooks/list":
            return {
                "data": [
                    {
                        "cwd": self.value["cwd"],
                        "hooks": [],
                        "warnings": [],
                        "errors": [],
                    }
                ]
            }
        if method == "turn/start":
            self.turn_count += 1
            return {"turn": {"id": f"turn-{self.turn_count}"}}
        raise AssertionError(method)


def test_idle_three_messages_produce_one_wake(tmp_path):
    project = write_project(tmp_path / "project")
    for index in range(3):
        add_mail(project, f"20260716T04000{index}Z-claude-to-codex-{index}")
    client = FakeClient(thread(project))
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)

    first = bridge.step()
    client.value["status"] = {"type": "active"}
    second = bridge.step()

    starts = [call for call in client.calls if call[0] == "turn/start"]
    assert first[0].ok and second[0].ok
    assert len(starts) == 1
    assert "drain inbox at" in starts[0][1]["input"][0]["text"]
    assert len(list((project / ".roundtable/inbox/codex/new").iterdir())) == 3


def test_busy_waits_for_matching_turn_completed(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041000Z-claude-to-codex-1")
    live_thread = thread(project, status="active")
    client = FakeClient(live_thread)
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)

    assert bridge.step()[0].detail == "waiting for turn/completed"
    live_thread["status"] = {"type": "idle"}
    other = {"method": "turn/completed", "params": {"threadId": "other"}}
    assert "waiting" in bridge.step([other])[0].detail
    target = {"method": "turn/completed", "params": {"threadId": "thread-1"}}
    assert bridge.step([target])[0].detail == "wake started"

    starts = [call for call in client.calls if call[0] == "turn/start"]
    assert len(starts) == 1


def test_fresh_zero_turn_tui_uses_status_transition_when_resume_has_no_rollout(
    tmp_path,
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041100Z-claude-to-codex-1")
    live_thread = thread(project, status="active")

    class FreshClient(FakeClient):
        def request(self, method, params):
            if method == "thread/resume":
                self.calls.append((method, params))
                raise _rtcodex.RpcError(
                    f"thread/resume failed (-32600): no rollout found for thread id {params['threadId']}"
                )
            return super().request(method, params)

    client = FreshClient(live_thread)
    bridge = wake.WakeBridge(
        client,
        [project],
        wake.StateStore(tmp_path / "state.json"),
        auto_discover=True,
    )

    assert bridge.step()[0].detail == "waiting for turn/completed"
    live_thread["status"] = {"type": "idle"}
    assert bridge.step()[0].detail == "wake started"
    assert client.turn_count == 1
    assert "thread-1" not in bridge.subscribed_threads


def test_failed_wake_turn_keeps_mail_and_retries_with_backoff(tmp_path):
    project = write_project(tmp_path / "project")
    mail = add_mail(project, "20260716T041500Z-claude-to-codex-1")
    client = FakeClient(thread(project))
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"

    failed = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "failed"},
        },
    }
    result = bridge.step([failed])[0]

    assert not result.ok and "retry in 30s" in result.detail
    assert mail.exists()
    assert client.turn_count == 1
    state = store.project_state(project)
    state["retryAt"] = 0
    store.save()
    assert bridge.step()[0].detail == "wake started"
    assert client.turn_count == 2


def test_three_interrupted_undrained_wakes_latch_needs_human(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041600Z-claude-to-codex-1")
    client = FakeClient(thread(project))
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    notices = []
    monkeypatch.setattr(
        wake,
        "notify_needs_human",
        lambda *args: notices.append(args),
    )
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)

    assert bridge.step()[0].detail == "wake started"
    for attempt in range(1, wake.INTERRUPTED_BREAKER_LIMIT + 1):
        interrupted = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": f"turn-{attempt}", "status": "interrupted"},
            },
        }
        result = bridge.step([interrupted])[0]
        if attempt < wake.INTERRUPTED_BREAKER_LIMIT:
            assert "retry in" in result.detail
            store.project_state(project)["retryAt"] = 0
            store.save()
            assert bridge.step()[0].detail == "wake started"
        else:
            assert not result.ok and "needs human" in result.detail

    assert client.turn_count == wake.INTERRUPTED_BREAKER_LIMIT
    persisted = wake.StateStore(state_path).project_state(project)
    assert persisted["phase"] == "NEEDS_HUMAN"
    assert persisted["breaker"]["interruptedUndrainedCount"] == 3
    assert persisted["needsHuman"]["source"] == "interrupted_breaker"
    assert len(notices) == 1

    add_mail(project, "20260716T041601Z-claude-to-codex-2")
    restarted = wake.WakeBridge(client, [project], wake.StateStore(state_path))
    assert "needs human" in restarted.step()[0].detail
    assert client.turn_count == wake.INTERRUPTED_BREAKER_LIMIT
    assert len(notices) == 1

    wake.StateStore(state_path).bind(project, thread(project))
    recovered = wake.WakeBridge(client, [project], wake.StateStore(state_path))
    assert recovered.step()[0].detail == "wake started"
    assert client.turn_count == wake.INTERRUPTED_BREAKER_LIMIT + 1


def test_non_interrupted_terminal_resets_interrupted_breaker(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041610Z-claude-to-codex-1")
    client = FakeClient(thread(project))
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)

    assert bridge.step()[0].detail == "wake started"
    first = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-1", "status": "interrupted"},
        },
    }
    assert "retry in" in bridge.step([first])[0].detail
    assert store.project_state(project)["breaker"]["interruptedUndrainedCount"] == 1
    store.project_state(project)["retryAt"] = 0
    store.save()
    assert bridge.step()[0].detail == "wake started"

    failed = {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-1",
            "turn": {"id": "turn-2", "status": "failed"},
        },
    }
    assert "retry in" in bridge.step([failed])[0].detail
    assert "breaker" not in store.project_state(project)


@pytest.mark.parametrize(
    ("trust_status", "enabled", "blocked"),
    [
        ("untrusted", True, True),
        ("modified", True, True),
        ("trusted", True, False),
        ("managed", True, False),
        ("untrusted", False, False),
    ],
)
def test_hook_trust_preflight_blocks_only_enabled_review_gates(
    tmp_path, monkeypatch, trust_status, enabled, blocked
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041620Z-claude-to-codex-1")

    class TrustClient(FakeClient):
        def request(self, method, params):
            if method == "hooks/list":
                self.calls.append((method, params))
                return {
                    "data": [
                        {
                            "cwd": str(project),
                            "hooks": [
                                {
                                    "key": "test:session_start:0:0",
                                    "enabled": enabled,
                                    "trustStatus": trust_status,
                                }
                            ],
                            "warnings": [],
                            "errors": [],
                        }
                    ]
                }
            return super().request(method, params)

    client = TrustClient(thread(project))
    notices = []
    monkeypatch.setattr(
        wake,
        "notify_needs_human",
        lambda *args: notices.append(args),
    )
    bridge = wake.WakeBridge(
        client,
        [project],
        wake.StateStore(tmp_path / "state.json"),
        auto_discover=True,
    )

    result = bridge.step()[0]

    assert ("needs human" in result.detail) is blocked
    assert client.turn_count == (0 if blocked else 1)
    assert len(notices) == (1 if blocked else 0)


def test_malformed_hook_trust_response_fails_closed(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041625Z-claude-to-codex-1")

    class MalformedHooksClient(FakeClient):
        def request(self, method, params):
            if method == "hooks/list":
                self.calls.append((method, params))
                return {"data": []}
            return super().request(method, params)

    client = MalformedHooksClient(thread(project))
    bridge = wake.WakeBridge(
        client,
        [project],
        wake.StateStore(tmp_path / "state.json"),
        auto_discover=True,
    )

    result = bridge.step()[0]

    assert not result.ok and "hooks/list expected one entry" in result.detail
    assert client.turn_count == 0


def test_unknown_enabled_hook_trust_status_fails_closed(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041627Z-claude-to-codex-1")

    class UnknownTrustClient(FakeClient):
        def request(self, method, params):
            if method == "hooks/list":
                self.calls.append((method, params))
                return {
                    "data": [
                        {
                            "cwd": str(project),
                            "hooks": [
                                {
                                    "key": "test:session_start:0:0",
                                    "enabled": True,
                                    "trustStatus": "future-status",
                                }
                            ],
                            "warnings": [],
                            "errors": [],
                        }
                    ]
                }
            return super().request(method, params)

    client = UnknownTrustClient(thread(project))
    result = wake.WakeBridge(
        client,
        [project],
        wake.StateStore(tmp_path / "state.json"),
        auto_discover=True,
    ).step()[0]

    assert not result.ok and "unknown trustStatus" in result.detail
    assert client.turn_count == 0


def test_same_thread_rebind_during_human_gate_does_not_resurrect_latch(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041628Z-claude-to-codex-1")
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    store.bind(project, thread(project))
    old_revision = store.bindings[str(project)]["bindingRevision"]
    external = wake.StateStore(state_path)
    notices = []
    monkeypatch.setattr(wake, "notify_needs_human", lambda *args: notices.append(args))

    class RebindingTrustClient(FakeClient):
        def request(self, method, params):
            if method == "hooks/list":
                self.calls.append((method, params))
                external.bind(project, thread(project))
                return {
                    "data": [
                        {
                            "cwd": str(project),
                            "hooks": [
                                {
                                    "key": "test:session_start:0:0",
                                    "enabled": True,
                                    "trustStatus": "untrusted",
                                }
                            ],
                            "warnings": [],
                            "errors": [],
                        }
                    ]
                }
            return super().request(method, params)

    client = RebindingTrustClient(thread(project))
    result = wake.WakeBridge(client, [project], store).step()[0]

    persisted = wake.StateStore(state_path)
    assert result.detail == "binding changed before needs-human latch"
    assert persisted.bindings[str(project)]["threadId"] == "thread-1"
    assert persisted.bindings[str(project)]["bindingRevision"] != old_revision
    assert persisted.project_state(project)["phase"] == "EMPTY"
    assert "needsHuman" not in persisted.project_state(project)
    assert notices == []
    assert client.turn_count == 0


def test_human_request_waits_while_active_then_resolution_clears_marker(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041630Z-claude-to-codex-1")
    live = thread(project)
    client = FakeClient(live)
    store = wake.StateStore(tmp_path / "state.json")
    notices = []
    monkeypatch.setattr(
        wake,
        "notify_needs_human",
        lambda *args: notices.append(args),
    )
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    live["status"] = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    live["turns"] = [{"id": "turn-1", "status": "inProgress"}]
    approval = {
        "id": "approval-1",
        "method": "item/commandExecution/requestApproval",
        "params": {"threadId": "thread-1", "turnId": "turn-1"},
    }

    result = bridge.step([approval])[0]

    assert result.ok and result.detail == "generation already woken"
    assert store.project_state(project)["phase"] == "WAKE_ACTIVE"
    assert len(store.project_state(project)["pendingHumanRequests"]) == 1
    assert client.turn_count == 1
    assert notices == []

    resolved = {
        "method": "serverRequest/resolved",
        "params": {"requestId": "approval-1", "threadId": "thread-1"},
    }
    assert bridge.step([resolved])[0].detail == "generation already woken"
    assert "pendingHumanRequests" not in store.project_state(project)

    live["status"] = {"type": "idle"}
    live["turns"] = [{"id": "turn-1", "status": "interrupted"}]
    terminal = bridge.step()[0]
    assert "retry in" in terminal.detail
    assert "needsHuman" not in store.project_state(project)
    assert notices == []


def test_persisted_human_request_survives_reconnect_and_interrupted_latches(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041635Z-claude-to-codex-1")
    live = thread(project)
    first_client = FakeClient(live)
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    notices = []
    monkeypatch.setattr(wake, "notify_needs_human", lambda *args: notices.append(args))
    first_bridge = wake.WakeBridge(
        first_client, [project], store, auto_discover=True
    )
    assert first_bridge.step()[0].detail == "wake started"
    live["status"] = {"type": "active"}
    approval = {
        "id": "approval-1",
        "method": "item/fileChange/requestApproval",
        "params": {"threadId": "thread-1", "turnId": "turn-1"},
    }
    assert first_bridge.step([approval])[0].detail == "generation already woken"

    recovered = thread(project)
    recovered["turns"] = [{"id": "turn-1", "status": "interrupted"}]
    second_client = FakeClient(recovered)
    result = wake.WakeBridge(
        second_client, [project], wake.StateStore(state_path)
    ).step()[0]

    assert not result.ok and "needs human" in result.detail
    persisted = wake.StateStore(state_path).project_state(project)
    assert persisted["needsHuman"]["source"] == "server_request"
    assert "pendingHumanRequests" not in persisted
    assert first_client.turn_count == 1
    assert second_client.turn_count == 0
    assert len(notices) == 1


def test_generation_drift_reconciles_old_human_gate_before_new_wake(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041636Z-claude-to-codex-1")
    live = thread(project)
    first_client = FakeClient(live)
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    monkeypatch.setattr(wake, "notify_needs_human", lambda *_args: None)
    first_bridge = wake.WakeBridge(
        first_client, [project], store, auto_discover=True
    )
    assert first_bridge.step()[0].detail == "wake started"
    old_generation = store.project_state(project)["lastWakeGeneration"]
    live["status"] = {"type": "active"}
    request = {
        "id": "approval-old-generation",
        "method": "item/permissions/requestApproval",
        "params": {"threadId": "thread-1", "turnId": "turn-1"},
    }
    assert first_bridge.step([request])[0].detail == "generation already woken"
    add_mail(project, "20260716T041637Z-claude-to-codex-2")
    new_generation, _ids = wake.pending_generation(project)
    assert new_generation != old_generation

    recovered = thread(project)
    recovered["turns"] = [{"id": "turn-1", "status": "interrupted"}]
    second_client = FakeClient(recovered)
    result = wake.WakeBridge(
        second_client, [project], wake.StateStore(state_path)
    ).step()[0]

    assert "needs human" in result.detail
    persisted = wake.StateStore(state_path).project_state(project)
    assert persisted["needsHuman"]["generation"] == old_generation
    assert first_client.turn_count == 1
    assert second_client.turn_count == 0


def test_resolved_wrong_or_nonhuman_server_requests_do_not_latch(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041640Z-claude-to-codex-1")
    client = FakeClient(thread(project))
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    messages = [
        {
            "id": "wrong-thread",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "other", "turnId": "turn-1"},
        },
        {
            "id": "client-tool",
            "method": "item/tool/call",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        },
        {
            "id": "resolved",
            "method": "item/permissions/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        },
        {
            "method": "serverRequest/resolved",
            "params": {"requestId": "resolved", "threadId": "thread-1"},
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread-1",
                "turn": {"id": "turn-1", "status": "interrupted"},
            },
        },
    ]

    result = bridge.step(messages)[0]

    assert "retry in" in result.detail
    assert store.project_state(project)["phase"] == "BACKOFF"
    assert "needsHuman" not in store.project_state(project)


def test_active_human_flags_alone_do_not_latch(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041650Z-claude-to-codex-1")
    live = thread(project)
    client = FakeClient(live)
    store = wake.StateStore(tmp_path / "state.json")
    monkeypatch.setattr(wake, "notify_needs_human", lambda *_args: None)
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    live["status"] = {
        "type": "active",
        "activeFlags": ["waitingOnApproval"],
    }
    live["turns"] = [{"id": "turn-1", "status": "inProgress"}]

    result = bridge.step()[0]

    assert result.ok and result.detail == "generation already woken"
    assert "needsHuman" not in store.project_state(project)
    assert client.turn_count == 1


def test_auto_resolving_user_input_is_not_a_human_gate(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041655Z-claude-to-codex-1")
    live = thread(project)
    client = FakeClient(live)
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    live["status"] = {"type": "active", "activeFlags": ["waitingOnUserInput"]}
    request = {
        "id": "input-1",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "autoResolutionMs": 60000,
        },
    }

    result = bridge.step([request])[0]

    assert result.detail == "generation already woken"
    assert "pendingHumanRequests" not in store.project_state(project)
    assert "needsHuman" not in store.project_state(project)


def test_partial_resolution_leaves_exact_human_gate_pending(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041656Z-claude-to-codex-1")
    live = thread(project)
    client = FakeClient(live)
    store = wake.StateStore(tmp_path / "state.json")
    monkeypatch.setattr(wake, "notify_needs_human", lambda *_args: None)
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    live["status"] = {"type": "active"}
    requests = [
        {
            "id": "approval-1",
            "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        },
        {
            "id": "approval-2",
            "method": "item/fileChange/requestApproval",
            "params": {"threadId": "thread-1", "turnId": "turn-1"},
        },
    ]
    assert bridge.step(requests)[0].detail == "generation already woken"
    assert len(store.project_state(project)["pendingHumanRequests"]) == 2

    wrong_resolution = {
        "method": "serverRequest/resolved",
        "params": {"requestId": "approval-1", "threadId": "other"},
    }
    assert bridge.step([wrong_resolution])[0].detail == "generation already woken"
    assert len(store.project_state(project)["pendingHumanRequests"]) == 2
    exact_resolution = {
        "method": "serverRequest/resolved",
        "params": {"requestId": "approval-1", "threadId": "thread-1"},
    }
    assert bridge.step([exact_resolution])[0].detail == "generation already woken"
    assert len(store.project_state(project)["pendingHumanRequests"]) == 1

    live["status"] = {"type": "idle"}
    live["turns"] = [{"id": "turn-1", "status": "interrupted"}]
    result = bridge.step()[0]

    assert "needs human" in result.detail
    reason = store.project_state(project)["needsHuman"]["reason"]
    assert "item/fileChange/requestApproval" in reason
    assert "item/commandExecution/requestApproval" not in reason


def test_nullable_mcp_elicitation_is_tied_to_bridge_wake_turn(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041657Z-claude-to-codex-1")
    live = thread(project)
    client = FakeClient(live)
    store = wake.StateStore(tmp_path / "state.json")
    monkeypatch.setattr(wake, "notify_needs_human", lambda *_args: None)
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    live["status"] = {"type": "active"}
    elicitation = {
        "id": 7,
        "method": "mcpServer/elicitation/request",
        "params": {"threadId": "thread-1", "turnId": None},
    }

    assert bridge.step([elicitation])[0].detail == "generation already woken"
    pending = next(
        iter(store.project_state(project)["pendingHumanRequests"].values())
    )
    assert pending["turnId"] == "turn-1"
    live["status"] = {"type": "idle"}
    live["turns"] = [{"id": "turn-1", "status": "interrupted"}]
    assert "needs human" in bridge.step()[0].detail
    assert store.project_state(project)["needsHuman"]["source"] == "server_request"


def test_interrupted_wake_rechecks_hook_trust_before_retry(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041658Z-claude-to-codex-1")

    class ChangingTrustClient(FakeClient):
        trust_checks = 0

        def request(self, method, params):
            if method == "hooks/list":
                self.calls.append((method, params))
                self.trust_checks += 1
                return {
                    "data": [
                        {
                            "cwd": str(project),
                            "hooks": [
                                {
                                    "key": "test:session_start:0:0",
                                    "enabled": True,
                                    "trustStatus": (
                                        "trusted" if self.trust_checks == 1 else "modified"
                                    ),
                                }
                            ],
                            "warnings": [],
                            "errors": [],
                        }
                    ]
                }
            return super().request(method, params)

    live = thread(project)
    client = ChangingTrustClient(live)
    store = wake.StateStore(tmp_path / "state.json")
    monkeypatch.setattr(wake, "notify_needs_human", lambda *_args: None)
    bridge = wake.WakeBridge(client, [project], store, auto_discover=True)
    assert bridge.step()[0].detail == "wake started"
    live["turns"] = [{"id": "turn-1", "status": "interrupted"}]

    result = bridge.step()[0]

    assert "needs human" in result.detail
    assert store.project_state(project)["needsHuman"]["source"] == "hook_trust"
    assert "breaker" not in store.project_state(project)


def test_daemon_reconnect_resumes_before_retrying_undrained_wake(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041700Z-claude-to-codex-1")
    first_client = FakeClient(thread(project))
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    assert wake.WakeBridge(
        first_client, [project], store, auto_discover=True
    ).step()[0].detail == "wake started"

    recovered = thread(project)
    recovered["turns"] = [{"id": "turn-1", "status": "interrupted"}]
    second_client = FakeClient(recovered)
    second_bridge = wake.WakeBridge(
        second_client, [project], wake.StateStore(state_path)
    )

    result = second_bridge.step()[0]

    methods = [method for method, _params in second_client.calls]
    assert methods[:2] == ["thread/read", "thread/resume"]
    assert not result.ok and "retry in 30s" in result.detail
    assert "turn/start" not in methods


def test_resume_failure_obeys_persisted_backoff_on_same_connection(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041750Z-claude-to-codex-1")
    store = wake.StateStore(tmp_path / "state.json")
    store.bind(project, thread(project))

    class ResumeFailClient(FakeClient):
        resume_count = 0

        def request(self, method, params):
            if method == "thread/resume":
                self.calls.append((method, params))
                self.resume_count += 1
                raise _rtcodex.RpcError("resume unavailable")
            return super().request(method, params)

    client = ResumeFailClient(thread(project))
    bridge = wake.WakeBridge(client, [project], store)

    first = bridge.step()[0]
    second = bridge.step()[0]

    assert not first.ok and "resume unavailable" in first.detail
    assert second.detail == "backoff"
    assert client.resume_count == 1


def test_same_connection_unload_resumes_active_wake_before_history_check(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T041800Z-claude-to-codex-1")

    class RestartingClient(FakeClient):
        unloaded = False
        resumed_after_unload = 0

        def request(self, method, params):
            if self.unloaded and method == "thread/read":
                self.calls.append((method, params))
                return {"thread": thread(project, status="notLoaded")}
            if self.unloaded and method == "thread/resume":
                self.calls.append((method, params))
                self.resumed_after_unload += 1
                return {"thread": thread(project, status="idle")}
            if self.unloaded and method == "thread/turns/list":
                self.calls.append((method, params))
                return {"data": [{"id": "turn-1", "status": "failed"}]}
            return super().request(method, params)

    client = RestartingClient(thread(project))
    bridge = wake.WakeBridge(
        client,
        [project],
        wake.StateStore(tmp_path / "state.json"),
        auto_discover=True,
    )
    assert bridge.step()[0].detail == "wake started"
    client.unloaded = True

    result = bridge.step()[0]

    assert client.resumed_after_unload == 1
    assert not result.ok and "retry in 30s" in result.detail


def test_wrong_thread_identity_fails_closed_and_keeps_mail(tmp_path):
    project = write_project(tmp_path / "project")
    mail = add_mail(project, "20260716T042000Z-claude-to-codex-1")
    wrong = thread(tmp_path / "other")
    client = FakeClient(wrong)
    store = wake.StateStore(tmp_path / "state.json")

    result = wake.WakeBridge(
        client, [project], store, auto_discover=True
    ).step()[0]

    assert not result.ok
    assert "exactly one" in result.detail
    assert mail.exists()
    assert not any(call[0] == "turn/start" for call in client.calls)


def test_default_bridge_leaves_unbound_project_mail_waiting(tmp_path):
    project = write_project(tmp_path / "project")
    mail = add_mail(project, "20260716T042050Z-claude-to-codex-1")
    client = FakeClient(thread(project))
    store = wake.StateStore(tmp_path / "state.json")

    result = wake.WakeBridge(client, [project], store).step()[0]

    assert not result.ok
    assert "no explicit Codex binding" in result.detail
    assert mail.exists()
    assert store.bindings == {}
    assert not any(
        method in {"thread/loaded/list", "turn/start"}
        for method, _params in client.calls
    )


def test_remote_tui_vscode_source_binds_with_absent_thread_source(tmp_path):
    project = write_project(tmp_path / "project")
    remote = thread(project)
    remote.update({"source": "vscode", "threadSource": "user"})

    assert wake.validate_thread(project, remote)["id"] == "thread-1"

    remote["threadSource"] = "subAgent"
    with pytest.raises(wake.IdentityError, match="remote TUI threadSource is not user"):
        wake.validate_thread(project, remote)

    remote["threadSource"] = None
    validated = wake.validate_thread(project, remote, expected_id="thread-1")
    store = wake.StateStore(tmp_path / "state.json")
    store.bind(project, validated)
    assert store.bindings[str(project)]["threadId"] == "thread-1"

    remote["ephemeral"] = True
    with pytest.raises(wake.IdentityError, match="refusing to bind an ephemeral thread"):
        wake.validate_thread(project, remote, expected_id="thread-1")
    remote["ephemeral"] = False

    remote.update({"source": {"subAgent": "child"}, "threadSource": "user"})
    with pytest.raises(wake.IdentityError, match="source is not a supported TUI"):
        wake.validate_thread(project, remote)


def test_remote_vscode_thread_requires_explicit_binding_for_discovery(tmp_path):
    project = write_project(tmp_path / "project")
    remote = thread(project)
    remote.update({"source": "vscode", "threadSource": "user"})
    client = FakeClient(remote)

    with pytest.raises(wake.IdentityError, match="auto-discoverable local CLI"):
        wake.discover_thread(client, project)

    store = wake.StateStore(tmp_path / "state.json")
    store.bind(project, remote)
    selected = wake.WakeBridge(client, [project], store)._thread_for(project)
    assert selected["id"] == remote["id"]


def test_discovery_does_not_claim_uniqueness_when_a_thread_read_fails(tmp_path):
    project = write_project(tmp_path / "project")

    class PartialReadClient:
        def request(self, method, params):
            if method == "thread/loaded/list":
                return {"data": ["thread-1", "thread-2"]}
            if method == "thread/read" and params["threadId"] == "thread-1":
                return {"thread": thread(project, thread_id="thread-1")}
            if method == "thread/read":
                raise _rtcodex.RpcError("temporary read failure")
            raise AssertionError(method)

    with pytest.raises(_rtcodex.RpcError, match="temporary read failure"):
        wake.discover_thread(PartialReadClient(), project)


def test_malformed_or_symlink_mail_fails_closed(tmp_path):
    project = write_project(tmp_path / "project")
    inbox = project / ".roundtable/inbox/codex/new"
    inbox.mkdir(parents=True)
    target = tmp_path / "outside.md"
    target.write_text("outside")
    (inbox / "bad.md").symlink_to(target)

    try:
        wake.pending_generation(project)
    except wake.IdentityError as error:
        assert "non-regular" in str(error)
    else:
        raise AssertionError("symlink inbox entry was accepted")
    assert target.read_text() == "outside"


def test_malformed_project_backs_off_without_blocking_other_project(
    tmp_path, monkeypatch
):
    bad = write_project(tmp_path / "bad")
    good = write_project(tmp_path / "good")
    bad_id = "20260716T042100Z-claude-to-codex-bad"
    bad_mail = add_mail(bad, bad_id)
    bad_mail.write_bytes(b"\xff]\n")
    add_mail(good, "20260716T042100Z-claude-to-codex-good")
    original = wake.pending_generation
    calls = {str(bad): 0, str(good): 0}

    def counted(project, agent_id="codex"):
        calls[str(project)] += 1
        return original(project, agent_id)

    monkeypatch.setattr(wake, "pending_generation", counted)
    store = wake.StateStore(tmp_path / "state.json")
    bridge = wake.WakeBridge(
        FakeClient(thread(good)), [bad, good], store, auto_discover=True
    )

    first = bridge.step()
    second = bridge.step()

    assert not first[0].ok and "UTF-8" in first[0].detail
    assert first[1].detail == "wake started"
    assert second[0].detail == "backoff"
    assert calls[str(bad)] == 1
    assert bridge.client.turn_count == 1


def test_pending_generation_tolerates_mail_moved_during_scan(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    vanished = add_mail(project, "20260716T042200Z-claude-to-codex-gone")
    kept_id = "20260716T042200Z-claude-to-codex-kept"
    add_mail(project, kept_id)
    original_lstat = Path.lstat

    def moving_lstat(path):
        if path == vanished:
            path.unlink()
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", moving_lstat)

    generation, ids = wake.pending_generation(project)

    assert generation is not None
    assert ids == [kept_id]


def test_long_single_line_mail_body_does_not_count_as_oversized_header(tmp_path):
    project = write_project(tmp_path / "project")
    msg_id = "20260716T042300Z-claude-to-codex-long"
    mail = add_mail(project, msg_id)
    mail.write_text(
        f"[CLAUDE→CODEX directive id={msg_id}] " + ("x" * (32 * 1024))
    )

    generation, ids = wake.pending_generation(project)

    assert generation is not None
    assert ids == [msg_id]


def test_quiet_ack_files_do_not_wake_or_change_normal_generation(tmp_path):
    project = write_project(tmp_path / "project")
    ack_id = "20260716T042400Z-claude-to-codex-ack"
    ack = add_mail(project, ack_id)
    ack.rename(ack.with_name(f"ack-{ack.name}"))

    assert wake.pending_generation(project) == (None, [])

    normal_id = "20260716T042401Z-claude-to-codex-normal"
    add_mail(project, normal_id)
    generation, ids = wake.pending_generation(project)

    assert generation is not None
    assert ids == [normal_id]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema": wake.STATE_SCHEMA, "bindings": [], "projects": {}},
        {"schema": wake.STATE_SCHEMA, "bindings": {}, "projects": []},
        {"schema": wake.STATE_SCHEMA, "bindings": {"p": []}, "projects": {}},
        {"schema": wake.STATE_SCHEMA, "bindings": {}, "projects": {"p": []}},
    ],
)
def test_corrupt_state_shape_falls_back_to_empty(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload))

    store = wake.StateStore(path)

    assert store.bindings == {}
    assert store.data["projects"] == {}


def test_invalid_retry_numeric_fields_do_not_crash_state_machine(tmp_path):
    project = write_project(tmp_path / "project")
    add_mail(project, "20260716T042350Z-claude-to-codex-1")
    store = wake.StateStore(tmp_path / "state.json")
    store.bind(project, thread(project))
    generation, _ids = wake.pending_generation(project)
    store.project_state(project).update(
        {
            "phase": "WAKE_ACTIVE",
            "retryAt": "not-a-number",
            "retryCount": "also-not-a-number",
            "lastWakeGeneration": generation,
            "lastWakeTurnId": "turn-old",
            "lastWakeThreadId": "thread-1",
        }
    )
    store.save()
    value = thread(project)
    value["turns"] = [{"id": "turn-old", "status": "failed"}]

    result = wake.WakeBridge(FakeClient(value), [project], store).step()[0]

    assert not result.ok and "retry in 30s" in result.detail
    assert store.project_state(project)["retryCount"] == 1


def test_invalid_persisted_project_disables_daemon_without_restart_storm(tmp_path):
    args = Namespace(
        project=[str(tmp_path / "missing")],
        once=False,
    )

    assert wake.run_command(args) == 0

    args.once = True
    assert wake.run_command(args) == 1


def test_wake_heartbeat_reports_loaded_bridge_build(tmp_path):
    path = tmp_path / "wake-heartbeat.json"
    socket_path = tmp_path / "app.sock"

    wake.heartbeat(path, [], socket_path, "2026-07-20T00:00:00Z", None)

    payload = json.loads(path.read_text())
    assert payload["bridgeBuildFingerprint"] == wake.BRIDGE_BUILD_FINGERPRINT
    assert payload["bridgeBuildFingerprint"].startswith("sha256:")


def test_wake_run_backs_off_without_repairing_daemon(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    repair_calls = []
    monkeypatch.setattr(
        wake, "require_supported_version", lambda: (0, 144, 6)
    )
    monkeypatch.setattr(
        wake,
        "require_supported_daemon",
        lambda _socket: (_ for _ in ()).throw(
            _rtcodex.CodexRuntimeError("daemon unavailable")
        ),
    )
    monkeypatch.setattr(
        wake,
        "ensure_daemon",
        lambda _socket: repair_calls.append("repair"),
        raising=False,
    )
    args = Namespace(
        project=[str(project)],
        once=True,
        state_file=tmp_path / "wake-state.json",
        socket=tmp_path / "app.sock",
        auto_discover=False,
        poll_seconds=0.0,
    )

    assert wake.run_command(args) == 1
    assert repair_calls == []


def _recv_http(conn):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        data.extend(conn.recv(4096))
    return bytes(data)


def _recv_client_frame(conn):
    first, second = conn.recv(2)
    assert first & 0x0F in (0x1, 0x8)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", conn.recv(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", conn.recv(8))[0]
    assert second & 0x80
    mask = conn.recv(4)
    payload = bytearray()
    while len(payload) < length:
        payload.extend(conn.recv(length - len(payload)))
    return bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))


def _send_server_json(conn, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if len(raw) < 126:
        header = bytes((0x81, len(raw)))
    else:
        header = bytes((0x81, 126)) + struct.pack("!H", len(raw))
    conn.sendall(header + raw)


def test_real_unix_websocket_initialize_envelope(tmp_path, monkeypatch):
    # Darwin sockaddr_un paths are limited to roughly 104 bytes.  Keep the
    # socket inside pytest's isolated directory while passing the kernel a
    # short relative path; unlike /private/tmp, this is portable to Linux.
    monkeypatch.chdir(tmp_path)
    path = Path("rtws.sock")
    path.unlink(missing_ok=True)
    ready = threading.Event()
    observed = []

    def server():
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        listener.listen(1)
        ready.set()
        conn, _ = listener.accept()
        header = _recv_http(conn).decode("latin1")
        key_line = next(
            line
            for line in header.split("\r\n")
            if line.lower().startswith("sec-websocket-key:")
        )
        key = key_line.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        conn.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode()
        )
        initialize = json.loads(_recv_client_frame(conn))
        observed.append(initialize)
        _send_server_json(conn, {"id": initialize["id"], "result": {"codexHome": "/tmp"}})
        observed.append(json.loads(_recv_client_frame(conn)))
        conn.close()
        listener.close()

    worker = threading.Thread(target=server)
    worker.start()
    assert ready.wait(2)
    client = _rtcodex.AppServerClient(path)
    try:
        assert observed[0]["method"] == "initialize"
    finally:
        client.close()
    worker.join(2)
    path.unlink(missing_ok=True)

    assert "jsonrpc" not in observed[0]
    assert observed[0]["params"]["clientInfo"]["name"] == "roundtable_rt_codex_wake"
    assert observed[0]["params"]["capabilities"] == {"experimentalApi": True}
    assert observed[1] == {"method": "initialized"}


@pytest.mark.parametrize("payload", [b"{", b"[]", b"\xff"])
def test_websocket_rejects_malformed_or_non_object_json(payload):
    class FakeSocket:
        timeout = 1.0

        def gettimeout(self):
            return self.timeout

        def settimeout(self, value):
            self.timeout = value

    transport = object.__new__(_rtcodex.WebSocketUnix)
    transport.sock = FakeSocket()
    transport._read_frame = lambda: (True, 0x1, payload)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="JSON message"):
        transport.recv_json()


def test_rpc_non_object_error_and_result_fail_safely():
    class FakeTransport:
        responses = [
            {"id": 1, "error": ["bad"]},
            {"id": 2, "result": ["bad"]},
        ]

        def send_json(self, _payload):
            pass

        def recv_json(self, _timeout):
            return self.responses.pop(0)

    client = object.__new__(_rtcodex.AppServerClient)
    client.transport = FakeTransport()
    client.timeout = 1.0
    client.next_id = 1
    client.notifications = []

    with pytest.raises(_rtcodex.RpcError, match=r"\['bad'\]"):
        client.request("first")
    with pytest.raises(_rtcodex.CodexRuntimeError, match="expected an object"):
        client.request("second")


def test_ensure_daemon_kickstarts_once(monkeypatch, tmp_path):
    socket_path = tmp_path / "app.sock"
    refused = ConnectionRefusedError("down")
    outcomes = iter(
        [
            (False, "down", refused),
            (False, "down", refused),
            (True, "ready", None),
        ]
    )
    calls = []
    monkeypatch.setattr(_rtcodex, "DEFAULT_SOCKET", socket_path)
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        _rtcodex, "probe_handshake_detailed", lambda *args, **kwargs: next(outcomes)
    )
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda path: {"Label": "test"})
    monkeypatch.setattr(
        _rtcodex,
        "install_launch_agent",
        lambda *args, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        _rtcodex, "kickstart", lambda *args, **kwargs: calls.append("kickstart")
    )

    _rtcodex.ensure_daemon(socket_path, timeout=2)

    assert calls == ["install", "kickstart"]
    lock_path = tmp_path / "runtime/codex-app-server-start.lock"
    assert lock_path.is_file()
    with lock_path.open("r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def test_ensure_daemon_refuses_restart_on_permission_error(monkeypatch, tmp_path):
    socket_path = tmp_path / "app.sock"
    socket_path.touch()
    calls = []
    monkeypatch.setattr(_rtcodex, "DEFAULT_SOCKET", socket_path)
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *args, **kwargs: (False, "operation not permitted", PermissionError(1)),
    )
    monkeypatch.setattr(
        _rtcodex,
        "install_launch_agent",
        lambda *args, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        _rtcodex, "kickstart", lambda *args, **kwargs: calls.append("kickstart")
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match="refusing daemon restart"):
        _rtcodex.ensure_daemon(socket_path)
    assert calls == []


def test_ensure_daemon_rejects_custom_socket_before_any_side_effect(monkeypatch, tmp_path):
    calls = []
    custom = tmp_path / "custom.sock"
    monkeypatch.setattr(
        _rtcodex,
        "probe_handshake_detailed",
        lambda *args, **kwargs: calls.append("probe"),
    )
    monkeypatch.setattr(
        _rtcodex,
        "install_launch_agent",
        lambda *args, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        _rtcodex, "kickstart", lambda *args, **kwargs: calls.append("kickstart")
    )

    with pytest.raises(_rtcodex.UnsupportedVersion, match="non-default"):
        _rtcodex.ensure_daemon(custom)
    assert calls == []


def test_daemon_install_rejects_custom_socket_before_launchctl(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(
        daemon,
        "install_launch_agent",
        lambda *args, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["rt-codex-daemon", "install", "--socket", str(tmp_path / "custom.sock"), "--reload"],
    )

    assert daemon.main() == 1
    assert calls == []
    assert "non-default app-server socket" in capsys.readouterr().err


def test_wake_install_rejects_custom_socket_before_plist_or_launchctl(
    monkeypatch, tmp_path, capsys
):
    calls = []
    monkeypatch.setattr(
        wake,
        "wake_plist",
        lambda *args, **kwargs: calls.append("plist"),
    )
    monkeypatch.setattr(
        wake,
        "install_launch_agent",
        lambda *args, **kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rt-codex-wake",
            "--socket",
            str(tmp_path / "custom.sock"),
            "install",
            "--reload",
        ],
    )

    assert wake.main() == 1
    assert calls == []
    assert "non-default app-server socket" in capsys.readouterr().err


def test_daemon_install_rejects_unsupported_cli_before_plist_or_launchctl(
    monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(
        daemon,
        "require_supported_version",
        lambda: (_ for _ in ()).throw(
            _rtcodex.UnsupportedVersion("unsupported test CLI")
        ),
    )
    monkeypatch.setattr(
        daemon,
        "app_server_plist",
        lambda *_args: calls.append("plist"),
    )
    monkeypatch.setattr(
        daemon,
        "install_launch_agent",
        lambda *_args, **_kwargs: calls.append("install"),
    )
    monkeypatch.setattr(sys, "argv", ["rt-codex-daemon", "install", "--reload"])

    assert daemon.main() == 1
    assert calls == []
    assert "unsupported test CLI" in capsys.readouterr().err


def test_wake_install_rejects_unsupported_cli_before_plist_or_launchctl(
    monkeypatch, capsys
):
    calls = []
    monkeypatch.setattr(
        wake,
        "require_supported_version",
        lambda: (_ for _ in ()).throw(
            _rtcodex.UnsupportedVersion("unsupported test CLI")
        ),
    )
    monkeypatch.setattr(
        wake,
        "wake_plist",
        lambda *_args, **_kwargs: calls.append("plist"),
    )
    monkeypatch.setattr(
        wake,
        "install_launch_agent",
        lambda *_args, **_kwargs: calls.append("install"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["rt-codex-wake", "install", "--reload"],
    )

    assert wake.main() == 1
    assert calls == []
    assert "unsupported test CLI" in capsys.readouterr().err


def test_singleton_kernel_lock_releases_without_unlink(tmp_path):
    path = tmp_path / "bridge.lock"
    first = wake.acquire_singleton(path)
    with pytest.raises(_rtcodex.CodexRuntimeError):
        wake.acquire_singleton(path)
    first.close()  # Kernel releases the lock; the file intentionally remains.
    second = wake.acquire_singleton(path)
    second.close()
    assert path.is_file()


def test_running_store_does_not_overwrite_external_rebind(tmp_path):
    project = write_project(tmp_path / "project")
    path = tmp_path / "state.json"
    running = wake.StateStore(path)
    running.bind(project, thread(project, thread_id="thread-old"))
    external = wake.StateStore(path)
    external.bind(project, thread(project, thread_id="thread-new"))

    running.project_state(project).update(
        {
            "phase": "WAKE_ACTIVE",
            "lastWakeGeneration": "stale-generation",
            "lastWakeTurnId": "stale-turn",
        }
    )
    running.save()

    reloaded = wake.StateStore(path)
    assert reloaded.bindings[str(project)]["threadId"] == "thread-new"
    assert reloaded.project_state(project)["phase"] == "EMPTY"
    assert "reboundAt" in reloaded.project_state(project)
    assert "lastWakeGeneration" not in reloaded.project_state(project)


def test_unbind_removes_binding_and_project_phase_atomically(tmp_path):
    project = write_project(tmp_path / "project")
    path = tmp_path / "state.json"
    store = wake.StateStore(path)
    store.bind(project, thread(project))
    store.project_state(project)["phase"] = "WAKE_ACTIVE"
    store.save()

    assert wake.StateStore(path).unbind(project)
    reloaded = wake.StateStore(path)

    assert str(project) not in reloaded.bindings
    assert str(project) not in reloaded.data["projects"]
    assert not reloaded.unbind(project)


def test_resume_excludes_turn_history(tmp_path):
    project = write_project(tmp_path / "project")
    client = FakeClient(thread(project))

    resumed = wake.resume_thread(client, project, "thread-1")

    assert resumed["id"] == "thread-1"
    assert client.calls[-1] == (
        "thread/resume",
        {"threadId": "thread-1", "excludeTurns": True},
    )


def test_same_connection_not_loaded_thread_is_resumed(tmp_path):
    project = write_project(tmp_path / "project")

    class UnloadedClient(FakeClient):
        def request(self, method, params):
            self.calls.append((method, params))
            if method == "thread/read":
                return {"thread": thread(project, status="notLoaded")}
            if method == "thread/resume":
                return {"thread": thread(project, status="idle")}
            raise AssertionError(method)

    client = UnloadedClient(thread(project))
    store = wake.StateStore(tmp_path / "state.json")
    store.bind(project, thread(project))
    bridge = wake.WakeBridge(client, [project], store)

    refreshed = bridge._refresh(project, "thread-1")

    assert wake.status_type(refreshed) == "idle"
    assert [method for method, _params in client.calls] == [
        "thread/read",
        "thread/read",
        "thread/resume",
    ]


def test_rebind_before_locked_start_prevents_old_thread_wake(tmp_path):
    project = write_project(tmp_path / "project")
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    store.bind(project, thread(project, thread_id="thread-old"))
    external = wake.StateStore(state_path)

    class RebindingClient(FakeClient):
        def request(self, method, params):
            if method == "thread/read":
                external.bind(project, thread(project, thread_id="thread-new"))
            return super().request(method, params)

    client = RebindingClient(thread(project, thread_id="thread-old"))
    bridge = wake.WakeBridge(client, [project], store)
    revision = store.bindings[str(project)]["bindingRevision"]

    with pytest.raises(wake.IdentityError, match="binding changed before wake"):
        bridge._wake(
            project,
            "thread-old",
            "generation",
            1,
            expected_binding_revision=revision,
        )

    assert not any(method == "turn/start" for method, _params in client.calls)
    assert wake.StateStore(state_path).bindings[str(project)]["threadId"] == "thread-new"


def test_auto_discovery_does_not_overwrite_concurrent_explicit_bind(tmp_path):
    project = write_project(tmp_path / "project")
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    external = wake.StateStore(state_path)

    class DiscoverRaceClient:
        def __init__(self):
            self.calls = []

        def request(self, method, params):
            self.calls.append((method, params))
            if method == "thread/loaded/list":
                external.bind(project, thread(project, thread_id="thread-new"))
                return {"data": ["thread-old"]}
            if method == "thread/read":
                return {
                    "thread": thread(project, thread_id=params["threadId"])
                }
            if method == "thread/resume":
                return {
                    "thread": thread(project, thread_id=params["threadId"])
                }
            raise AssertionError(method)

    bridge = wake.WakeBridge(
        DiscoverRaceClient(), [project], store, auto_discover=True
    )

    selected = bridge._thread_for(project)

    assert selected["id"] == "thread-new"
    persisted = wake.StateStore(state_path)
    assert persisted.bindings[str(project)]["threadId"] == "thread-new"


def test_rebind_during_start_waits_then_resets_wake_state(tmp_path):
    project = write_project(tmp_path / "project")
    state_path = tmp_path / "state.json"
    store = wake.StateStore(state_path)
    store.bind(project, thread(project, thread_id="thread-old"))
    external = wake.StateStore(state_path)

    class ConcurrentRebindClient(FakeClient):
        worker = None
        attempted = threading.Event()
        finished = threading.Event()

        def request(self, method, params):
            if method == "turn/start":
                def rebind():
                    self.attempted.set()
                    external.bind(project, thread(project, thread_id="thread-new"))
                    self.finished.set()

                self.worker = threading.Thread(target=rebind)
                self.worker.start()
                assert self.attempted.wait(1)
            return super().request(method, params)

    client = ConcurrentRebindClient(thread(project, thread_id="thread-old"))
    bridge = wake.WakeBridge(client, [project], store)
    revision = store.bindings[str(project)]["bindingRevision"]

    assert bridge._wake(
        project,
        "thread-old",
        "generation",
        1,
        expected_binding_revision=revision,
    )
    client.worker.join(2)

    assert client.finished.is_set()
    persisted = wake.StateStore(state_path)
    assert persisted.bindings[str(project)]["threadId"] == "thread-new"
    assert persisted.project_state(project)["phase"] == "EMPTY"
    assert "lastWakeGeneration" not in persisted.project_state(project)


def test_supported_version_accepts_any_release_at_or_above_floor(monkeypatch):
    # No valve exists any more; every release at or above the floor is a
    # candidate proven later by the live protocol probe, not an allowlist.
    monkeypatch.delenv("RT_CODEX_ALLOW_UNVALIDATED", raising=False)
    for version in ((0, 144, 6), (0, 144, 7), (0, 145, 0), (1, 0, 0)):
        monkeypatch.setattr(
            _rtcodex,
            "codex_version",
            lambda version=version: (version, "codex-cli stub"),
        )
        assert _rtcodex.require_supported_version() == version


def test_below_floor_and_parse_failure_rejected(monkeypatch):
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 143, 0), "codex-cli 0.143.0")
    )
    with pytest.raises(_rtcodex.UnsupportedVersion, match="below the minimum"):
        _rtcodex.require_supported_version()

    monkeypatch.setattr(_rtcodex, "codex_version", lambda: (None, "garbage"))
    with pytest.raises(_rtcodex.CodexRuntimeError, match="could not parse"):
        _rtcodex.require_supported_version()


def test_require_validated_version_shim_applies_floor(monkeypatch):
    # The legacy shim now aliases the floor+parse check for mixed-version
    # upgrades rather than enforcing an exact allowlist.
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 145, 0), "codex-cli 0.145.0")
    )
    assert _rtcodex.require_validated_version() == (0, 145, 0)

    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 143, 0), "codex-cli 0.143.0")
    )
    with pytest.raises(_rtcodex.UnsupportedVersion, match="below the minimum"):
        _rtcodex.require_validated_version()


def _standalone_daemon(selected_codex, version="0.145.0"):
    return {
        "status": "running",
        "socketPath": str(_rtcodex.DEFAULT_SOCKET),
        "managedCodexPath": str(selected_codex),
        "managedCodexVersion": version,
        "cliVersion": version,
        "appServerVersion": version,
    }


def test_supported_daemon_probes_standalone_release(monkeypatch):
    selected_codex = Path("/tmp/standalone/current/codex")
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 145, 0), "codex-cli 0.145.0")
    )
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "daemon_version",
        lambda _path: (_standalone_daemon(selected_codex), ""),
    )
    probes = []
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: probes.append("probe") or (True, "probe passed"),
    )

    daemon = _rtcodex.require_supported_daemon()

    assert daemon["cliVersion"] == "0.145.0"
    assert probes == ["probe"]

    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: (False, "thread/loaded/list probe failed: closed"),
    )
    with pytest.raises(_rtcodex.UnsupportedVersion, match="protocol probe"):
        _rtcodex.require_supported_daemon()


def test_floor_release_is_probed(monkeypatch):
    # The floor release is a protocol claim, not an allowlist entry, so it is
    # probed like every other at-or-above-floor release.
    selected_codex = Path("/tmp/standalone/current/codex")
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6")
    )
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "daemon_version",
        lambda _path: (_standalone_daemon(selected_codex, "0.144.6"), ""),
    )
    probes = []
    monkeypatch.setattr(
        _rtcodex,
        "codex_protocol_probe",
        lambda _path: probes.append("probe") or (True, "probe passed"),
    )

    daemon = _rtcodex.require_supported_daemon()

    assert daemon["cliVersion"] == "0.144.6"
    assert probes == ["probe"]


class ProbeClient:
    responses = {}
    calls = []
    closed = False

    def __init__(self, _socket, timeout=3.0):
        type(self).calls = []
        type(self).closed = False

    def request(self, method, params):
        type(self).calls.append((method, params))
        value = type(self).responses[method]
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        type(self).closed = True


def test_protocol_probe_validates_rpc_surface(monkeypatch):
    monkeypatch.setattr(_rtcodex, "AppServerClient", ProbeClient)

    ProbeClient.responses = {
        "thread/loaded/list": {"data": ["thread-1"], "nextCursor": None},
        "hooks/list": {"data": []},
    }
    ok, detail = _rtcodex.codex_protocol_probe(Path("/tmp/probe.sock"))
    assert ok
    assert "probe passed" in detail
    assert [method for method, _params in ProbeClient.calls] == [
        "thread/loaded/list",
        "hooks/list",
    ]
    assert ProbeClient.closed

    ProbeClient.responses = {
        "thread/loaded/list": {"data": "not-a-list", "nextCursor": None},
        "hooks/list": {"data": []},
    }
    ok, detail = _rtcodex.codex_protocol_probe(Path("/tmp/probe.sock"))
    assert not ok
    assert "thread/loaded/list probe failed" in detail
    assert ProbeClient.closed

    ProbeClient.responses = {
        "thread/loaded/list": {"data": [], "nextCursor": None},
        "hooks/list": _rtcodex.RpcError("hooks/list failed (400): nope"),
    }
    ok, detail = _rtcodex.codex_protocol_probe(Path("/tmp/probe.sock"))
    assert not ok
    assert "hooks/list probe failed" in detail
    assert ProbeClient.closed


def test_wake_plist_never_forwards_a_release_valve(tmp_path, monkeypatch):
    # The unvalidated-release valve was removed; the wake LaunchAgent must not
    # carry any such acceptance toggle into its environment.
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_CODEX_BIN", str(fake_codex))
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)

    monkeypatch.setenv("RT_CODEX_ALLOW_UNVALIDATED", "1")
    bridge = _rtcodex.wake_plist(tmp_path / "app.sock")
    assert "RT_CODEX_ALLOW_UNVALIDATED" not in bridge["EnvironmentVariables"]


def test_codex_bin_preserves_explicit_symlink_path(tmp_path, monkeypatch):
    target = tmp_path / "versions" / "0.144.6" / "codex"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n")
    target.chmod(0o755)
    selected = tmp_path / "current" / "codex"
    selected.parent.mkdir()
    selected.symlink_to(target)
    monkeypatch.setenv("RT_CODEX_BIN", str(selected))

    assert _rtcodex.codex_bin() == selected.absolute()
    assert _rtcodex.codex_bin() != target


def test_codex_bin_prefers_standalone_cache_over_npm(tmp_path, monkeypatch):
    home = tmp_path / "home"
    standalone_target = home / "standalone-versions" / "0.144.6" / "codex"
    standalone_target.parent.mkdir(parents=True)
    standalone_target.write_text("#!/bin/sh\n")
    standalone_target.chmod(0o755)
    standalone = home / ".codex" / "packages" / "standalone" / "current" / "codex"
    standalone.parent.mkdir(parents=True)
    standalone.symlink_to(standalone_target)
    npm = home / ".npm-global" / "bin" / "codex"
    npm.parent.mkdir(parents=True)
    npm.write_text("#!/bin/sh\n")
    npm.chmod(0o755)
    monkeypatch.delenv("RT_CODEX_BIN", raising=False)
    monkeypatch.setattr(_rtcodex, "CODEX_HOME", home / ".codex")
    monkeypatch.setattr(
        _rtcodex.Path, "home", classmethod(lambda _cls: home)
    )

    assert _rtcodex.codex_bin() == standalone.absolute()


def test_rt_codex_socket_environment_cannot_redefine_validated_default(tmp_path):
    env = os.environ.copy()
    env["RT_CODEX_SOCKET"] = str(tmp_path / "unvalidated.sock")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; sys.path.insert(0, 'bin'); "
                "import _rtcodex; print(_rtcodex.DEFAULT_SOCKET)"
            ),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(
        Path.home() / ".codex/app-server-control/app-server-control.sock"
    )
    assert proc.stdout.strip() != env["RT_CODEX_SOCKET"]


def test_rt_codex_import_rejects_split_runtime_roots(tmp_path):
    env = os.environ.copy()
    env["RT_RUNTIME_DIR"] = str((tmp_path / "generic").resolve())
    env["RT_CODEX_RUNTIME_DIR"] = str((tmp_path / "legacy").resolve())
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'bin'); import _rtcodex",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode != 0
    assert "select different runtime roots" in proc.stderr


def test_daemon_version_mismatch_fails_closed(monkeypatch):
    selected_codex = Path("/tmp/roundtable-current-codex")
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6")
    )
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "daemon_version",
        lambda _path: (
            {
                "status": "running",
                "socketPath": str(_rtcodex.DEFAULT_SOCKET),
                "managedCodexPath": str(selected_codex),
                "managedCodexVersion": "0.144.6",
                "cliVersion": "0.144.6",
                "appServerVersion": "0.144.5",
            },
            "",
        ),
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match="version mismatch"):
        _rtcodex.require_supported_daemon()


@pytest.mark.parametrize("payload", ["[]", '"running"', "7"])
def test_daemon_version_rejects_non_object_json(monkeypatch, payload):
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: Path("/tmp/codex"))
    monkeypatch.setattr(
        _rtcodex.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 0, payload, ""),
    )

    daemon, detail = _rtcodex.daemon_version(_rtcodex.DEFAULT_SOCKET)

    assert daemon is None
    assert detail == payload


def test_daemon_cli_version_mismatch_fails_closed(monkeypatch):
    selected_codex = Path("/tmp/roundtable-current-codex")
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6")
    )
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "daemon_version",
        lambda _path: (
            {
                "status": "running",
                "socketPath": str(_rtcodex.DEFAULT_SOCKET),
                "managedCodexPath": str(selected_codex),
                "managedCodexVersion": "0.144.6",
                "cliVersion": "0.144.5",
                "appServerVersion": "0.144.6",
            },
            "",
        ),
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match="daemon CLI"):
        _rtcodex.require_supported_daemon()


def test_external_daemon_ignores_standalone_management_slot(monkeypatch):
    selected_codex = Path("/tmp/npm/bin/codex")
    managed_codex = Path("/tmp/standalone/current/codex")
    monkeypatch.setattr(
        _rtcodex, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6")
    )
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "daemon_version",
        lambda _path: (
            {
                "status": "running",
                "socketPath": str(_rtcodex.DEFAULT_SOCKET),
                "managedCodexPath": str(managed_codex),
                # A separate standalone install may exist and may be a
                # different version; neither field identifies this socket.
                "managedCodexVersion": "0.143.0",
                "cliVersion": "0.144.6",
                "appServerVersion": "0.144.6",
            },
            "",
        ),
    )
    monkeypatch.setattr(
        _rtcodex, "codex_protocol_probe", lambda _path: (True, "probe passed")
    )

    daemon = _rtcodex.require_supported_daemon()

    assert daemon["managedCodexPath"] == str(managed_codex)


def test_standalone_managed_version_mismatch_fails_closed(monkeypatch):
    selected_codex = Path("/tmp/standalone/current/codex")
    daemon = {
        "status": "running",
        "socketPath": str(_rtcodex.DEFAULT_SOCKET),
        "managedCodexPath": str(selected_codex),
        "managedCodexVersion": "0.144.5",
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )

    with pytest.raises(
        _rtcodex.CodexDaemonReloadRequired,
        match="standalone/managed Codex version mismatch",
    ):
        _rtcodex.require_daemon_identity(
            daemon,
            _rtcodex.DEFAULT_SOCKET,
            (0, 144, 6),
        )


@pytest.mark.parametrize("backend", [None, "pid", "future", 7])
def test_daemon_backend_field_fails_closed(monkeypatch, backend):
    selected_codex = Path("/tmp/npm/bin/codex")
    daemon = {
        "status": "running",
        "socketPath": str(_rtcodex.DEFAULT_SOCKET),
        "backend": backend,
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": None,
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: selected_codex)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="backend"):
        _rtcodex.require_daemon_identity(
            daemon,
            _rtcodex.DEFAULT_SOCKET,
            (0, 144, 6),
        )


@pytest.mark.parametrize(
    "managed_version",
    [pytest.param("missing", id="missing"), "", "   ", 7],
)
def test_daemon_managed_version_schema_fails_closed(monkeypatch, managed_version):
    daemon = {
        "status": "running",
        "socketPath": str(_rtcodex.DEFAULT_SOCKET),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    if managed_version != "missing":
        daemon["managedCodexVersion"] = managed_version
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: Path("/tmp/npm/bin/codex"))

    with pytest.raises(_rtcodex.CodexRuntimeError, match="managedCodexVersion"):
        _rtcodex.require_daemon_identity(
            daemon,
            _rtcodex.DEFAULT_SOCKET,
            (0, 144, 6),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cliVersion", None),
        ("cliVersion", []),
        ("appServerVersion", None),
        ("appServerVersion", 7),
    ],
)
def test_daemon_version_field_schema_is_unsafe_not_reloadable(
    monkeypatch,
    field,
    value,
):
    daemon = {
        "status": "running",
        "socketPath": str(_rtcodex.DEFAULT_SOCKET),
        "managedCodexPath": "/tmp/standalone/current/codex",
        "managedCodexVersion": None,
        "cliVersion": "0.144.6",
        "appServerVersion": "0.144.6",
    }
    daemon[field] = value
    monkeypatch.setattr(_rtcodex, "codex_bin", lambda: Path("/tmp/npm/bin/codex"))
    monkeypatch.setattr(
        _rtcodex,
        "require_roundtable_daemon_owner",
        lambda _path: (101, 102),
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match=field) as observed:
        _rtcodex.require_daemon_identity(
            daemon,
            _rtcodex.DEFAULT_SOCKET,
            (0, 144, 6),
        )

    assert type(observed.value) is _rtcodex.CodexRuntimeError


def test_launchd_job_snapshot_uses_only_top_level_fields(monkeypatch):
    output = """gui/501/com.roundtable.codex-app-server = {
\tpath = /tmp/com.roundtable.codex-app-server.plist
\tstate = running
\tprogram = /tmp/npm/bin/codex
\targuments = {
\t\t/tmp/npm/bin/codex
\t\tapp-server
\t\t--listen
\t\tunix:///tmp/app.sock
\t}
\tworking directory = /tmp
\tenvironment = {
\t\tHOME => /tmp
\t\tRT_CODEX_BIN => /tmp/npm/bin/codex
\t}
\tpid = 101
\tresource coalition = {
\t\tstate = active
\t}
}
"""
    monkeypatch.setattr(
        _rtcodex,
        "_launchd_print",
        lambda _label: subprocess.CompletedProcess([], 0, output, ""),
    )

    observed = _rtcodex.launchd_job_snapshot(_rtcodex.APP_SERVER_LABEL)

    assert observed.state == "running"
    assert observed.pid == 101
    assert observed.program == Path("/tmp/npm/bin/codex")
    assert observed.arguments[-1] == "unix:///tmp/app.sock"
    assert dict(observed.environment)["RT_CODEX_BIN"] == "/tmp/npm/bin/codex"


def _owner_snapshot(tmp_path: Path, app_payload: dict, *, pid: int = 101):
    return _rtcodex.LaunchdJobSnapshot(
        path=tmp_path / f"{_rtcodex.APP_SERVER_LABEL}.plist",
        state="running",
        program=Path(app_payload["ProgramArguments"][0]),
        arguments=tuple(app_payload["ProgramArguments"]),
        environment=tuple(sorted(app_payload["EnvironmentVariables"].items())),
        working_directory=Path(app_payload["WorkingDirectory"]),
        pid=pid,
    )


@contextmanager
def _fake_authenticated_peer(pid: int):
    yield pid


def test_roundtable_daemon_owner_accepts_node_child_lineage(tmp_path, monkeypatch):
    socket_path = tmp_path / "app.sock"
    app_payload = {
        "ProgramArguments": [
            "/tmp/npm/bin/codex",
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        ],
        "WorkingDirectory": "/tmp",
        "EnvironmentVariables": {"RT_CODEX_BIN": "/tmp/npm/bin/codex"},
    }
    snapshot = _owner_snapshot(tmp_path, app_payload)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(
        _rtcodex,
        "launch_agent_path",
        lambda _label: snapshot.path,
    )
    monkeypatch.setattr(
        _rtcodex,
        "app_server_plist",
        lambda *_a, **_k: app_payload,
    )
    monkeypatch.setattr(_rtcodex, "launchd_job_snapshot", lambda _label: snapshot)
    monkeypatch.setattr(
        _rtcodex,
        "authenticated_socket_peer",
        lambda _path: _fake_authenticated_peer(102),
    )
    monkeypatch.setattr(
        _rtcodex,
        "_process_parent_and_uid",
        lambda pid: (101, os.getuid()) if pid == 102 else (1, os.getuid()),
    )

    assert _rtcodex.require_roundtable_daemon_owner(socket_path) == (101, 102)


def test_roundtable_daemon_owner_rejects_foreign_socket_peer(tmp_path, monkeypatch):
    socket_path = tmp_path / "app.sock"
    app_payload = {
        "ProgramArguments": ["/tmp/npm/bin/codex", "app-server"],
        "WorkingDirectory": "/tmp",
        "EnvironmentVariables": {"RT_CODEX_BIN": "/tmp/npm/bin/codex"},
    }
    snapshot = _owner_snapshot(tmp_path, app_payload)
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: snapshot.path)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: app_payload)
    monkeypatch.setattr(_rtcodex, "launchd_job_snapshot", lambda _label: snapshot)
    monkeypatch.setattr(
        _rtcodex,
        "authenticated_socket_peer",
        lambda _path: _fake_authenticated_peer(202),
    )
    monkeypatch.setattr(
        _rtcodex,
        "_process_parent_and_uid",
        lambda _pid: (0, os.getuid()),
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match="not owned"):
        _rtcodex.require_roundtable_daemon_owner(socket_path)


def test_roundtable_daemon_owner_rejects_launchd_pid_race(tmp_path, monkeypatch):
    socket_path = tmp_path / "app.sock"
    app_payload = {
        "ProgramArguments": ["/tmp/npm/bin/codex", "app-server"],
        "WorkingDirectory": "/tmp",
        "EnvironmentVariables": {"RT_CODEX_BIN": "/tmp/npm/bin/codex"},
    }
    before = _owner_snapshot(tmp_path, app_payload, pid=101)
    after = _owner_snapshot(tmp_path, app_payload, pid=103)
    snapshots = iter((before, after))
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: app_payload)
    monkeypatch.setattr(_rtcodex, "launchd_job_snapshot", lambda _label: next(snapshots))
    monkeypatch.setattr(
        _rtcodex,
        "authenticated_socket_peer",
        lambda _path: _fake_authenticated_peer(102),
    )
    monkeypatch.setattr(_rtcodex, "require_pid_lineage", lambda *_args: None)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="changed during"):
        _rtcodex.require_roundtable_daemon_owner(socket_path)


def test_roundtable_daemon_owner_rejects_unmanaged_environment(
    tmp_path,
    monkeypatch,
):
    socket_path = tmp_path / "app.sock"
    app_payload = {
        "ProgramArguments": ["/tmp/npm/bin/codex", "app-server"],
        "WorkingDirectory": "/tmp",
        "EnvironmentVariables": {"RT_CODEX_BIN": "/tmp/npm/bin/codex"},
    }
    expected = _owner_snapshot(tmp_path, app_payload)
    injected = _rtcodex.LaunchdJobSnapshot(
        path=expected.path,
        state=expected.state,
        program=expected.program,
        arguments=expected.arguments,
        environment=tuple(sorted((*expected.environment, ("NODE_OPTIONS", "--require=/tmp/x")))),
        working_directory=expected.working_directory,
        pid=expected.pid,
    )
    monkeypatch.setattr(_rtcodex, "_validate_service_paths", lambda _path: None)
    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: expected.path)
    monkeypatch.setattr(_rtcodex, "app_server_plist", lambda *_a, **_k: app_payload)
    monkeypatch.setattr(_rtcodex, "launchd_job_snapshot", lambda _label: injected)
    monkeypatch.setattr(
        _rtcodex,
        "authenticated_socket_peer",
        lambda _path: _fake_authenticated_peer(102),
    )
    monkeypatch.setattr(_rtcodex, "require_pid_lineage", lambda *_args: None)

    with pytest.raises(
        _rtcodex.CodexDaemonReloadRequired,
        match="NODE_OPTIONS",
    ):
        _rtcodex.require_roundtable_daemon_owner(socket_path)


def test_launchd_payloads_are_persistent_and_explicit(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    monkeypatch.setenv("RT_CODEX_BIN", str(fake_codex))
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)

    app = _rtcodex.app_server_plist(tmp_path / "app.sock")
    bridge = _rtcodex.wake_plist(tmp_path / "app.sock")

    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert app["RunAtLoad"] and app["KeepAlive"]
    assert app["ProgramArguments"][-2:] == ["--listen", f"unix://{tmp_path / 'app.sock'}"]
    assert app["ProgramArguments"][0] == str(fake_codex)
    assert app["EnvironmentVariables"]["RT_CODEX_BIN"] == str(fake_codex)
    assert app["EnvironmentVariables"]["RT_RUNTIME_DIR"] == str(tmp_path / "runtime")
    assert app["EnvironmentVariables"]["RT_CODEX_RUNTIME_DIR"] == str(
        tmp_path / "runtime"
    )
    assert bridge["RunAtLoad"] and bridge["KeepAlive"] == {"SuccessfulExit": False}
    assert bridge["ProgramArguments"][-1] == "run"
    assert bridge["EnvironmentVariables"]["RT_CODEX_BIN"] == str(fake_codex)
    assert bridge["EnvironmentVariables"]["RT_RUNTIME_DIR"] == str(
        tmp_path / "runtime"
    )


def test_launchd_payload_preview_does_not_create_runtime(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_CODEX_BIN", str(fake_codex))
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)

    app = _rtcodex.app_server_plist(
        tmp_path / "app.sock",
        ensure_runtime=False,
    )
    bridge = _rtcodex.wake_plist(
        tmp_path / "app.sock",
        ensure_runtime=False,
    )

    assert not runtime.exists()
    assert app["EnvironmentVariables"]["RT_RUNTIME_DIR"] == str(runtime)
    assert bridge["EnvironmentVariables"]["RT_RUNTIME_DIR"] == str(runtime)


def test_codex_runtime_root_rejects_symlink(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    runtime = tmp_path / "runtime"
    runtime.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="is a symlink"):
        _rtcodex.ensure_private_runtime_dir()


def test_codex_runtime_root_rejects_foreign_owner(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(_rtcodex.os, "getuid", lambda: runtime.stat().st_uid + 1)

    with pytest.raises(_rtcodex.CodexRuntimeError, match="owner uid"):
        _rtcodex.ensure_private_runtime_dir()


def test_launchd_payloads_preserve_stable_install_prefix(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    prefix = tmp_path / "roundtable"
    monkeypatch.setenv("RT_CODEX_BIN", str(fake_codex))
    monkeypatch.setattr(_rtcodex, "INSTALL_PREFIX", str(prefix))

    app = _rtcodex.app_server_plist(tmp_path / "app.sock")
    bridge = _rtcodex.wake_plist(tmp_path / "app.sock")

    assert app["EnvironmentVariables"]["ROUNDTABLE_INSTALL_PREFIX"] == str(prefix)
    assert bridge["EnvironmentVariables"]["ROUNDTABLE_INSTALL_PREFIX"] == str(prefix)


def test_launchd_payload_uses_registry_and_persists_only_auto_discover(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("RT_CODEX_BIN", str(fake_codex))
    monkeypatch.setattr(_rtcodex, "RUNTIME_DIR", tmp_path / "runtime")

    payload = _rtcodex.wake_plist(tmp_path / "app.sock", auto_discover=True)

    assert payload["ProgramArguments"][-2:] == ["run", "--auto-discover"]
    assert "--project" not in payload["ProgramArguments"]
    assert "RT_FALLBACK_PROJECT" not in payload["EnvironmentVariables"]


def test_explicit_launch_agent_reload_applies_unchanged_plist(tmp_path, monkeypatch):
    path = tmp_path / "agent.plist"
    commands = []
    prints = iter(
        [
            subprocess.CompletedProcess([], 0, "loaded", ""),
            subprocess.CompletedProcess([], 0, "removing", ""),
            subprocess.CompletedProcess([], 113, "", "not found"),
        ]
    )
    sleeps = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: path)
    monkeypatch.setattr(_rtcodex, "_write_plist", lambda _path, _payload: False)
    monkeypatch.setattr(_rtcodex, "_launchd_print", lambda _label: next(prints))
    monkeypatch.setattr(_rtcodex.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        _rtcodex.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or Result(),
    )

    _rtcodex.install_launch_agent("test.agent", {"Label": "test.agent"}, reload=True)

    assert commands[0][1] == "bootout"
    assert commands[1][1] == "bootstrap"
    assert sleeps == [0.1]


def test_launch_agent_reload_timeout_fails_before_bootstrap(tmp_path, monkeypatch):
    path = tmp_path / "agent.plist"
    commands = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: path)
    monkeypatch.setattr(_rtcodex, "_write_plist", lambda _path, _payload: False)
    monkeypatch.setattr(
        _rtcodex,
        "_launchd_print",
        lambda _label: subprocess.CompletedProcess([], 0, "loaded", ""),
    )
    monkeypatch.setattr(
        _rtcodex.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or Result(),
    )

    with pytest.raises(_rtcodex.CodexRuntimeError, match="timed out.*unload"):
        _rtcodex.install_launch_agent(
            "test.agent",
            {"Label": "test.agent"},
            reload=True,
            unload_timeout=0,
        )

    assert [command[1] for command in commands] == ["bootout"]


def test_launch_agent_reload_inspection_error_fails_before_bootstrap(
    tmp_path, monkeypatch
):
    path = tmp_path / "agent.plist"
    commands = []
    prints = iter(
        [
            subprocess.CompletedProcess([], 0, "loaded", ""),
            subprocess.CompletedProcess([], 64, "", "not authorized"),
        ]
    )

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(_rtcodex, "launch_agent_path", lambda _label: path)
    monkeypatch.setattr(_rtcodex, "_write_plist", lambda _path, _payload: False)
    monkeypatch.setattr(_rtcodex, "_launchd_print", lambda _label: next(prints))
    monkeypatch.setattr(
        _rtcodex.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or Result(),
    )

    with pytest.raises(
        _rtcodex.CodexRuntimeError,
        match="exit 64.*not authorized",
    ):
        _rtcodex.install_launch_agent(
            "test.agent", {"Label": "test.agent"}, reload=True
        )

    assert [command[1] for command in commands] == ["bootout"]


def test_doctor_failure_matrix_and_install_fix(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6"))
    monkeypatch.setattr(doctor, "daemon_version", lambda _path: (None, "down"))
    monkeypatch.setattr(doctor, "socket_check", lambda _path: (False, "missing"))
    monkeypatch.setattr(doctor, "probe_handshake", lambda _path: (False, "refused"))
    monkeypatch.setattr(doctor, "bridge_check", lambda *_args: (False, "missing pid"))
    monkeypatch.setattr(doctor, "launchd_loaded", lambda _label: False)
    monkeypatch.setattr(sys, "argv", ["rt-doctor"])

    assert doctor.main() == 1
    output = capsys.readouterr().out
    assert "FAIL daemon:" in output
    assert "FAIL socket:" in output
    assert "FAIL rpc:" in output
    # With the app-server unreachable, the version can no longer be proven by
    # a live probe, so it fails closed rather than passing on the version alone.
    assert "FAIL version:" in output
    assert "cannot run the app-server protocol probe" in output
    assert "FAIL bridge:" in output
    assert "run `roundtable codex` from a normal terminal" in output


def test_doctor_reports_persisted_needs_human_as_warn(tmp_path, monkeypatch, capsys):
    project = str((tmp_path / "project").resolve())
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps(
            {
                "schema": wake.STATE_SCHEMA,
                "bindings": {},
                "projects": {
                    project: {
                        "phase": "NEEDS_HUMAN",
                        "needsHuman": {
                            "threadId": "thread-1",
                            "reason": "hook trust review required",
                        },
                    }
                },
            }
        )
    )
    monkeypatch.setattr(doctor, "load_project_registry", lambda _path: ([], []))
    report = doctor.Report()

    doctor.project_health_checks(
        report,
        tmp_path / "projects.yaml",
        state_file,
        tmp_path / "app.sock",
        False,
    )

    output = capsys.readouterr().out
    assert "WARN codex-wake:" in output
    assert "thread=thread-1 needs human" in output
    assert f"rt-codex-wake bind {project}" in output
    assert not report.failed


def test_doctor_failed_probe_fails_closed(monkeypatch, capsys):
    socket_path = doctor.DEFAULT_SOCKET
    monkeypatch.setattr(doctor, "codex_version", lambda: ((0, 144, 7), "codex-cli 0.144.7"))
    monkeypatch.setattr(
        doctor,
        "daemon_version",
        lambda _path: (
            {
                "status": "running",
                "socketPath": str(socket_path),
                "cliVersion": "0.144.7",
                "appServerVersion": "0.144.7",
            },
            "",
        ),
    )
    monkeypatch.setattr(doctor, "socket_check", lambda _path: (True, "ok"))
    monkeypatch.setattr(doctor, "probe_handshake", lambda _path: (True, "ok"))
    monkeypatch.setattr(
        doctor,
        "codex_protocol_probe",
        lambda _path: (False, "hooks/list probe failed: closed"),
    )
    monkeypatch.setattr(doctor, "bridge_check", lambda *_args: (False, "disabled"))
    monkeypatch.setattr(doctor, "launchd_loaded", lambda _label: False)
    monkeypatch.setattr(sys, "argv", ["rt-doctor"])

    assert doctor.main() == 1
    output = capsys.readouterr().out
    assert "FAIL version:" in output
    assert "failed the app-server protocol probe" in output
    assert "FAIL bridge:" in output
    assert "legacy" not in output


def test_doctor_rejects_daemon_on_different_socket(monkeypatch, capsys, tmp_path):
    requested = tmp_path / "requested.sock"
    monkeypatch.setattr(doctor, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6"))
    monkeypatch.setattr(
        doctor,
        "daemon_version",
        lambda _path: (
            {
                "status": "running",
                "socketPath": "/tmp/other.sock",
                "cliVersion": "0.144.6",
                "appServerVersion": "0.144.6",
            },
            "",
        ),
    )
    monkeypatch.setattr(doctor, "socket_check", lambda _path: (True, "ok"))
    monkeypatch.setattr(doctor, "probe_handshake", lambda _path: (True, "ok"))
    monkeypatch.setattr(doctor, "bridge_check", lambda *_args: (True, "ok"))
    monkeypatch.setattr(sys, "argv", ["rt-doctor", "--socket", str(requested)])

    assert doctor.main() == 1
    output = capsys.readouterr().out
    assert "FAIL daemon: reported socket /tmp/other.sock" in output


def test_doctor_version_mismatch_reinstalls_loaded_plist(monkeypatch, capsys):
    socket_path = doctor.DEFAULT_SOCKET
    monkeypatch.setattr(
        doctor, "codex_version", lambda: ((0, 144, 6), "codex-cli 0.144.6")
    )
    monkeypatch.setattr(
        doctor,
        "daemon_version",
        lambda _path: (
            {
                "status": "running",
                "socketPath": str(socket_path),
                "cliVersion": "0.144.6",
                "appServerVersion": "0.144.5",
            },
            "",
        ),
    )
    monkeypatch.setattr(doctor, "socket_check", lambda _path: (True, "ok"))
    monkeypatch.setattr(doctor, "probe_handshake", lambda _path: (True, "ok"))
    monkeypatch.setattr(doctor, "bridge_check", lambda *_args: (True, "ok"))
    monkeypatch.setattr(sys, "argv", ["rt-doctor"])

    assert doctor.main() == 1
    output = capsys.readouterr().out
    assert "FAIL version:" in output
    assert "run `roundtable codex` from a normal terminal" in output


def test_daemon_status_rejects_handshake_only_readiness(monkeypatch, capsys):
    monkeypatch.setattr(
        daemon, "probe_handshake", lambda _path: (True, "initialize succeeded")
    )
    monkeypatch.setattr(daemon, "launchd_running", lambda _label: True)
    monkeypatch.setattr(
        daemon,
        "require_supported_daemon",
        lambda _path: (_ for _ in ()).throw(
            _rtcodex.CodexRuntimeError("CLI/app-server version mismatch")
        ),
    )
    monkeypatch.setattr(sys, "argv", ["rt-codex-daemon", "status"])

    assert daemon.main() == 1
    assert "version mismatch" in capsys.readouterr().err


def test_bridge_check_reports_non_object_heartbeat_as_failure(tmp_path, monkeypatch):
    pid_path = tmp_path / "rt-codex-wake.pid"
    pid_path.write_text("123\n")
    (tmp_path / "rt-codex-wake-heartbeat.json").write_text("[]")
    monkeypatch.setattr(doctor, "pid_is_running", lambda *_args: (True, "pid 123"))

    ok, detail = doctor.bridge_check(tmp_path, 15)

    assert not ok
    assert "not a JSON object" in detail
