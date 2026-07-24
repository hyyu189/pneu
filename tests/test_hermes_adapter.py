from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import threading
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "hermes" / "roundtable" / "__init__.py"
MANIFEST = ROOT / "integrations" / "hermes" / "roundtable" / "plugin.yaml"


def _load_plugin():
    spec = importlib.util.spec_from_file_location("roundtable_hermes_test", PLUGIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeContext:
    def __init__(
        self,
        *,
        inject_result=True,
        inject_error=None,
        dispatch_result=None,
        dispatch_results=None,
        dispatch_error=None,
    ) -> None:
        self.hooks = {}
        self.injected: list[tuple[str, str]] = []
        self.injected_event = threading.Event()
        self.inject_result = inject_result
        self.inject_error = inject_error
        self.dispatch_result = dispatch_result
        self.dispatch_results = (
            list(dispatch_results) if dispatch_results is not None else None
        )
        self.dispatch_error = dispatch_error
        self.dispatch_calls = []

    def register_hook(self, name, callback) -> None:
        self.hooks[name] = callback

    def inject_message(self, content, role="user") -> bool:
        self.injected.append((content, role))
        self.injected_event.set()
        if self.inject_error is not None:
            raise self.inject_error
        return self.inject_result

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatch_calls.append((name, args, kwargs))
        if self.dispatch_error is not None:
            raise self.dispatch_error
        if self.dispatch_results is not None:
            result = self.dispatch_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return self.dispatch_result


class FakeProcess:
    def __init__(self, output: str | None = None, returncode: int = 0) -> None:
        self.output = output
        self.returncode = None
        self.final_returncode = returncode
        self.communicating = threading.Event()
        self.release = threading.Event()
        self.terminated = False
        self.killed = False
        if output is not None:
            self.release.set()

    def communicate(self):
        self.communicating.set()
        assert self.release.wait(5)
        self.returncode = self.final_returncode
        return self.output or "", None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.final_returncode = -15
        self.release.set()

    def kill(self) -> None:
        self.killed = True
        self.final_returncode = -9
        self.release.set()


class FakePopen:
    def __init__(self, plans) -> None:
        self.plans = list(plans)
        self.calls = []
        self.created: list[FakeProcess] = []
        self.called = threading.Event()

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        plan = self.plans.pop(0)
        process = (
            plan
            if isinstance(plan, FakeProcess)
            else FakeProcess(*plan)
        )
        self.created.append(process)
        self.called.set()
        return process


def _set_activation(monkeypatch, tmp_path, agent="hermes"):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    prefix = tmp_path / "prefix"
    waiter = prefix / "bin" / "rt-wait-inbox"
    waiter.parent.mkdir(parents=True)
    waiter.write_text("#!/bin/sh\n", encoding="utf-8")
    waiter.chmod(0o755)
    monkeypatch.setenv("RT_PROJECT_ROOT", str(project))
    monkeypatch.setenv("RT_FROM", agent)
    monkeypatch.setenv("RT_SESSION_ID", "session-1")
    monkeypatch.setenv("RT_LEASE_REVISION", "revision-1")
    monkeypatch.setenv("ROUNDTABLE_INSTALL_PREFIX", str(prefix))
    return project, waiter.resolve()


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_registers_classic_and_tui_session_hooks_declared_by_manifest():
    plugin = _load_plugin()
    context = FakeContext()

    plugin.register(context)

    assert {
        "on_session_start",
        "on_session_reset",
        "on_session_finalize",
    }.issubset(context.hooks)
    assert "  - on_session_reset\n" in MANIFEST.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "missing",
    ["RT_PROJECT_ROOT", "RT_FROM", "RT_SESSION_ID", "RT_LEASE_REVISION"],
)
def test_incomplete_roundtable_environment_is_a_noop(
    tmp_path, monkeypatch, missing
):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    monkeypatch.delenv(missing)
    popen = FakePopen([FakeProcess()])
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"](session_id="native-hermes")

    assert not popen.calls
    assert not context.injected


def test_activation_prefers_managed_waiter_and_restarts_after_heartbeat(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    project, waiter = _set_activation(monkeypatch, tmp_path, agent="hermes/review")
    blocking = FakeProcess()
    popen = FakePopen(
        [
            ("rt-wait-inbox: heartbeat timeout after 45m\n", 0),
            blocking,
        ]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    _wait_until(lambda: len(popen.calls) == 2)

    command, kwargs = popen.calls[0]
    assert command == [str(waiter), "hermes/review"]
    assert kwargs["cwd"] == str(project)
    assert kwargs["env"]["RT_SESSION_ID"] == "session-1"
    assert not context.injected

    context.hooks["on_session_finalize"]()
    assert blocking.terminated


def test_tui_reset_starts_and_restarts_without_watcher_overlap(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    first = FakeProcess()
    second = FakeProcess()
    popen = FakePopen([first, second])
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_reset"](
        session_id="tui-session-1", platform="tui"
    )
    assert first.communicating.wait(1)

    # Hermes may emit classic on_session_start on the first turn after its
    # initial TUI reset.  It must not create a second watcher.
    context.hooks["on_session_start"](
        session_id="tui-session-1", platform="tui"
    )
    time.sleep(0.05)
    assert len(popen.calls) == 1

    # A reset is a session replacement even if a host omits the usual
    # preceding finalize.  The old process must be fully reaped first.
    context.hooks["on_session_reset"](
        session_id="tui-session-2", platform="tui"
    )
    assert first.terminated
    assert first.returncode == -15
    assert second.communicating.wait(1)
    assert len(popen.calls) == 2

    context.hooks["on_session_finalize"]()
    assert second.terminated


def _start_tui_mail(plugin, tmp_path, monkeypatch, context, *, session_id):
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-1").write_text("pending", encoding="utf-8")
    popen = FakePopen(
        [("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0)]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)

    plugin.register(context)
    context.hooks["on_session_reset"](
        session_id=session_id, platform="tui"
    )
    return project, popen


def test_tui_mail_handshake_uses_exact_session_key_and_call_order(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin.secrets, "token_hex", lambda size: "ab" * size)
    context = FakeContext(
        inject_result=False,
        dispatch_results=[
            (
                '{"session_id":"process-1","notify_on_complete":true,'
                '"error":null}'
            ),
            '{"exit_code":0,"error":null}',
        ],
    )

    _project, _popen = _start_tui_mail(
        plugin,
        tmp_path,
        monkeypatch,
        context,
        session_id="tui-session-key",
    )
    _wait_until(lambda: len(context.dispatch_calls) == 2)

    background_name, background_args, background_kwargs = (
        context.dispatch_calls[0]
    )
    release_name, release_args, release_kwargs = context.dispatch_calls[1]
    assert [background_name, release_name] == ["terminal", "terminal"]
    assert background_kwargs == release_kwargs == {
        "task_id": "tui-session-key"
    }
    assert background_args["background"] is True
    assert background_args["notify_on_complete"] is True
    assert background_args["pty"] is False
    assert "workdir" not in background_args
    assert plugin._MAIL_MESSAGE in background_args["command"]
    assert set(release_args) == {"command", "background", "timeout", "pty"}
    assert release_args["background"] is False
    assert release_args["timeout"] == plugin._TUI_RELEASE_TIMEOUT_SECONDS
    assert release_args["pty"] is False

    path_pattern = r"/tmp/roundtable-hermes-[0-9a-f]{64}\.sentinel"
    background_paths = set(re.findall(path_pattern, background_args["command"]))
    release_paths = set(re.findall(path_pattern, release_args["command"]))
    expected_digest = plugin.hashlib.sha256(
        b"tui-session-key\0" + ("ab" * 32).encode("utf-8")
    ).hexdigest()
    expected_path = f"/tmp/roundtable-hermes-{expected_digest}.sentinel"
    assert background_paths == release_paths == {expected_path}
    assert "ab" * 32 in background_args["command"]
    assert "ab" * 32 in release_args["command"]
    assert "/bin/rm" not in background_args["command"]
    assert "/bin/mv" not in release_args["command"]
    assert context.injected == [(plugin._MAIL_MESSAGE, "user")]

    context.hooks["on_session_finalize"]()


def test_tui_release_failure_kills_spawned_notification_process(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    context = FakeContext(
        inject_result=False,
        dispatch_results=[
            (
                '{"session_id":"process-1","notify_on_complete":true,'
                '"error":null}'
            ),
            '{"exit_code":1,"error":"release failed"}',
            '{"status":"killed","session_id":"process-1"}',
        ],
    )

    _project, popen = _start_tui_mail(
        plugin,
        tmp_path,
        monkeypatch,
        context,
        session_id="tui-session-key",
    )
    _wait_until(lambda: len(context.injected) == 2)

    assert [call[0] for call in context.dispatch_calls] == [
        "terminal",
        "terminal",
        "process",
    ]
    assert all(
        call[2] == {"task_id": "tui-session-key"}
        for call in context.dispatch_calls
    )
    assert context.dispatch_calls[-1][1] == {
        "action": "kill",
        "session_id": "process-1",
    }
    assert "invalid" in context.injected[-1][0]

    # A failed delivery is session-scoped, not process-permanent: a later
    # reset re-arms exactly one fresh watcher for the replacement session.
    blocking = FakeProcess()
    popen.plans.append(blocking)
    context.hooks["on_session_reset"](
        session_id="tui-session-key-2", platform="tui"
    )
    _wait_until(lambda: len(popen.calls) == 2)
    assert blocking.communicating.wait(1)

    context.hooks["on_session_finalize"]()
    assert blocking.terminated


def test_tui_unsupported_background_dispatch_is_killed_before_release(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    context = FakeContext(
        inject_result=False,
        dispatch_results=[
            (
                '{"session_id":"process-1","notify_on_complete":false,'
                '"notify_unsupported":"stateless host","error":null}'
            ),
            '{"status":"killed","session_id":"process-1"}',
        ],
    )

    _project, _popen = _start_tui_mail(
        plugin,
        tmp_path,
        monkeypatch,
        context,
        session_id="tui-session-key",
    )
    _wait_until(lambda: len(context.injected) == 2)

    assert [call[0] for call in context.dispatch_calls] == [
        "terminal",
        "process",
    ]
    assert context.dispatch_calls[-1][1] == {
        "action": "kill",
        "session_id": "process-1",
    }
    assert all(
        call[2] == {"task_id": "tui-session-key"}
        for call in context.dispatch_calls
    )


@pytest.mark.parametrize(
    "background_result",
    [
        "{not-json",
        '{"session_id":null,"notify_on_complete":false,"error":null}',
    ],
    ids=["malformed-json", "null-process-id"],
)
def test_tui_unaddressable_background_result_fails_without_release(
    tmp_path, monkeypatch, background_result
):
    plugin = _load_plugin()
    context = FakeContext(
        inject_result=False,
        dispatch_results=[background_result],
    )

    _project, _popen = _start_tui_mail(
        plugin,
        tmp_path,
        monkeypatch,
        context,
        session_id="tui-session-key",
    )
    _wait_until(lambda: len(context.injected) == 2)

    assert len(context.dispatch_calls) == 1
    assert context.dispatch_calls[0][0] == "terminal"
    assert context.dispatch_calls[0][2] == {
        "task_id": "tui-session-key"
    }


def test_mail_is_injected_once_until_non_ack_mail_is_drained(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_MAIL_DRAIN_POLL_SECONDS", 0.01)
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    mail = new_dir / "message-1"
    mail.write_text("body is never read by the adapter", encoding="utf-8")
    (new_dir / "ack-quiet").write_text("", encoding="utf-8")
    blocking = FakeProcess()
    popen = FakePopen(
        [
            ("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0),
            blocking,
        ]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    assert context.injected_event.wait(1)
    time.sleep(0.05)

    assert len(context.injected) == 1
    assert len(popen.calls) == 1

    mail.unlink()
    _wait_until(lambda: len(popen.calls) == 2)
    assert len(context.injected) == 1

    context.hooks["on_session_finalize"]()
    assert blocking.terminated


def test_mail_arriving_during_drain_gets_its_own_wake_generation(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_MAIL_DRAIN_POLL_SECONDS", 0.01)
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    first = new_dir / "message-1"
    first.write_text("first", encoding="utf-8")
    blocking = FakeProcess()
    popen = FakePopen(
        [
            ("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0),
            ("rt-wait-inbox: mail after 0s:\nmessage-2\n", 0),
            blocking,
        ]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    _wait_until(lambda: len(context.injected) == 1)

    second = new_dir / "message-2"
    second.write_text("arrived during first turn", encoding="utf-8")
    time.sleep(0.05)
    assert len(context.injected) == 1
    assert len(popen.calls) == 1

    first.unlink()
    _wait_until(lambda: len(context.injected) == 2)
    assert len(popen.calls) == 2

    second.unlink()
    _wait_until(lambda: len(popen.calls) == 3)
    context.hooks["on_session_finalize"]()
    assert blocking.terminated


@pytest.mark.parametrize(
    "output",
    [
        "rt-wait-inbox: mail after 5s:\n",
        "rt-wait-inbox: mail after 5s:\n../outside\n",
        "rt-wait-inbox: mail after 5s:\nack-quiet\n",
        "rt-wait-inbox: mail after 5s:\n padded-name\n",
    ],
)
def test_invalid_waiter_generation_fails_closed(tmp_path, monkeypatch, output):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    popen = FakePopen([(output, 0)])
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    _wait_until(lambda: context.injected == [(plugin._CONFIG_MESSAGE, "user")])

    assert len(popen.calls) == 1


@pytest.mark.parametrize(
    "context",
    [
        FakeContext(inject_result=False),
        FakeContext(inject_error=RuntimeError("CLI unavailable")),
    ],
)
def test_failed_mail_injection_stops_instead_of_waiting_forever(
    tmp_path, monkeypatch, context
):
    plugin = _load_plugin()
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-1").write_text("pending", encoding="utf-8")
    popen = FakePopen(
        [("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0)]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)

    plugin.register(context)
    context.hooks["on_session_start"]()
    assert context.injected_event.wait(1)
    _wait_until(lambda: len(context.injected) == 2)

    assert len(popen.calls) == 1
    assert len(context.injected) == 2
    assert "invalid" in context.injected[-1][0]


@pytest.mark.parametrize(
    ("output", "returncode", "expected"),
    [
        (
            "rt-wait-inbox: seat lease or watcher was superseded\n",
            0,
            "superseded",
        ),
        ("rt-wait-inbox: missing claimed-seat environment\n", 2, "invalid"),
    ],
)
def test_fence_or_configuration_failure_stops_with_one_diagnostic(
    tmp_path, monkeypatch, output, returncode, expected
):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    popen = FakePopen([(output, returncode)])
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    assert context.injected_event.wait(1)
    time.sleep(0.05)

    assert len(popen.calls) == 1
    assert len(context.injected) == 1
    assert expected in context.injected[0][0]

    context.hooks["on_session_start"]()
    time.sleep(0.05)
    assert len(popen.calls) == 1
    assert len(context.injected) == 1


def test_finalize_and_atexit_cleanup_terminate_the_watcher(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    registered = []
    monkeypatch.setattr(plugin.atexit, "register", registered.append)
    first = FakeProcess()
    popen = FakePopen([first])
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    assert first.communicating.wait(1)
    context.hooks["on_session_finalize"]()
    assert first.terminated

    plugin2 = _load_plugin()
    _set_activation(monkeypatch, tmp_path / "second")
    registered2 = []
    monkeypatch.setattr(plugin2.atexit, "register", registered2.append)
    second = FakeProcess()
    monkeypatch.setattr(plugin2.subprocess, "Popen", FakePopen([second]))
    context2 = FakeContext()
    plugin2.register(context2)
    context2.hooks["on_session_start"]()
    assert second.communicating.wait(1)
    registered2[0]()

    assert second.terminated


def test_finalize_allows_a_new_native_session_in_the_same_cli(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    first = FakeProcess()
    second = FakeProcess()
    popen = FakePopen([first, second])
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    assert first.communicating.wait(1)
    context.hooks["on_session_finalize"]()
    assert first.terminated

    context.hooks["on_session_start"]()
    assert second.communicating.wait(1)
    context.hooks["on_session_finalize"]()
    assert second.terminated


def test_pending_generation_renotifies_once_then_pauses_and_rearms_after_ack(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    monkeypatch.setattr(plugin, "_MAIL_DRAIN_POLL_SECONDS", 0.01)
    monkeypatch.setattr(plugin, "_PENDING_RENOTIFY_SECONDS", 0.05)
    monkeypatch.setattr(plugin, "_PENDING_PAUSE_SECONDS", 0.15)
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    mail = new_dir / "message-1"
    mail.write_text("body is never read by the adapter", encoding="utf-8")
    blocking = FakeProcess()
    popen = FakePopen(
        [
            ("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0),
            blocking,
        ]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    _wait_until(lambda: len(context.injected) == 3, timeout=5.0)
    time.sleep(0.05)

    # One re-notice, then one pause diagnostic, then silence; still only the
    # original waiter because the generation never left new/.
    assert [content for content, _role in context.injected] == [
        plugin._MAIL_MESSAGE,
        plugin._MAIL_MESSAGE,
        plugin._PENDING_MESSAGE,
    ]
    assert len(popen.calls) == 1

    # A late rt-ack (modeled by the archival leaving new/) must still re-arm
    # a fresh waiter automatically after the pause notice.
    mail.unlink()
    _wait_until(lambda: len(popen.calls) == 2, timeout=5.0)
    assert len(context.injected) == 3

    context.hooks["on_session_finalize"]()
    assert blocking.terminated


def test_session_reset_after_transient_failure_rearms_fresh_watcher(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-1").write_text("pending", encoding="utf-8")
    blocking = FakeProcess()
    popen = FakePopen(
        [
            ("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0),
            blocking,
        ]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext(inject_result=False)

    plugin.register(context)
    context.hooks["on_session_start"]()
    _wait_until(lambda: len(context.injected) == 2)
    assert "invalid" in context.injected[-1][0]
    assert len(popen.calls) == 1

    # A failed delivery is session-scoped: replacing the session through the
    # TUI reset boundary must arm one fresh watcher.
    context.hooks["on_session_reset"](session_id="tui-2", platform="tui")
    _wait_until(lambda: len(popen.calls) == 2)
    assert blocking.communicating.wait(1)

    context.hooks["on_session_finalize"]()
    assert blocking.terminated


def test_session_reset_after_supersession_stays_closed(tmp_path, monkeypatch):
    plugin = _load_plugin()
    _set_activation(monkeypatch, tmp_path)
    popen = FakePopen(
        [("rt-wait-inbox: seat lease or watcher was superseded\n", 0)]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext()

    plugin.register(context)
    context.hooks["on_session_start"]()
    _wait_until(
        lambda: context.injected == [(plugin._FENCE_MESSAGE, "user")]
    )

    # Supersession is permanent for this process: the RT_* session env can
    # never match the newer lease, so a reset must not re-arm or re-diagnose.
    context.hooks["on_session_reset"](session_id="tui-2", platform="tui")
    time.sleep(0.05)
    assert len(popen.calls) == 1
    assert context.injected == [(plugin._FENCE_MESSAGE, "user")]


def test_wake_message_teaches_reply_syntax_and_quiet_ack_drain():
    plugin = _load_plugin()

    assert "rt-inbox --fenced --archive-quiet-acks -f json" in (
        plugin._MAIL_MESSAGE
    )
    assert "rt-ack <id>[,<id>...]" in plugin._MAIL_MESSAGE
    assert 'rt-say <agent> <kind> "body"' in plugin._MAIL_MESSAGE
    assert "`--kind`/`--refs` options do not exist" in plugin._MAIL_MESSAGE
    assert "referenced message id belongs in the body" in plugin._MAIL_MESSAGE
    assert "rt-ack" in plugin._PENDING_MESSAGE
    assert "durable" in plugin._PENDING_MESSAGE


def test_desktop_platform_fails_closed_without_cli_injection(
    tmp_path, monkeypatch
):
    plugin = _load_plugin()
    project, _waiter = _set_activation(monkeypatch, tmp_path)
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-1").write_text("pending", encoding="utf-8")
    popen = FakePopen(
        [("rt-wait-inbox: mail after 5s:\nmessage-1\n", 0)]
    )
    monkeypatch.setattr(plugin.subprocess, "Popen", popen)
    context = FakeContext(inject_result=False)

    plugin.register(context)
    context.hooks["on_session_reset"](
        session_id="desktop-session", platform="desktop"
    )
    _wait_until(lambda: len(context.injected) == 2)

    # The dispatch_tool handshake is validated for the TUI only. A "desktop"
    # session without working CLI injection must fail closed instead of
    # claiming an unproven delivery surface.
    assert context.dispatch_calls == []
    assert len(popen.calls) == 1
    assert context.injected[-1][0] == plugin._CONFIG_MESSAGE
