"""Watcher lifecycle logging, crash/kill self-heal, and the pty arm journey.

The armed Claude inbox watcher died twice while idle on a live host with no
record of why.  These cases pin the two properties that make that
unreproducible failure diagnosable and survivable: every exit path leaves a
durable lifecycle record, and a watcher that dies without producing one is
re-armed by the hook process that owns the seat's wake channel.
"""

from __future__ import annotations

import json
import os
import pty
import signal
import subprocess
import sys
import threading
import time
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlib  # noqa: E402
import _rtruntime  # noqa: E402


JOURNEY_TIMEOUT = 20.0


@pytest.fixture(autouse=True)
def isolated_project_registry(tmp_path, monkeypatch) -> Path:
    registry = tmp_path / "projects.json"
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    return registry


def write_project(path: Path, agent: str = "claude") -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        f"  {agent}:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        f"      - id: {agent}\n"
    )
    _rtlib.register_project(project)
    return project


def project_inbox(project: Path, agent: str = "claude") -> Path:
    mailbox = _rtlib.resolve_project_mailbox(project)
    return mailbox.inbox_dir / agent


def claim_environment(
    monkeypatch,
    runtime: Path,
    project: Path,
    agent: str = "claude",
) -> dict[str, str]:
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    token = _rtruntime.claim(project, agent, "claude", owner_pid=os.getpid())
    environment = os.environ.copy()
    environment.update(
        {
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": agent,
            "RT_SESSION_ID": token.session_id,
            "RT_LEASE_REVISION": str(token.revision),
            "RT_WATCHER_POLL_SECONDS": "0.05",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def load_wait_module(name: str):
    loader = SourceFileLoader(name, str(BIN / "rt-wait-inbox"))
    spec = spec_from_loader(name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


def deliver(project: Path, name: str, agent: str = "claude") -> None:
    new_dir = project_inbox(project, agent) / "new"
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / name).write_text(
        f"[HERMES→CLAUDE question id={name}] mail\n", encoding="utf-8"
    )


def events(project: Path, agent: str = "claude") -> list[dict]:
    return _rtruntime.read_watcher_events(project, agent, limit=None)


def last_event(project: Path, event: str, agent: str = "claude") -> dict:
    matching = [
        record for record in events(project, agent) if record["event"] == event
    ]
    assert matching, f"no {event} record; log={json.dumps(events(project, agent))}"
    return matching[-1]


def wait_for_event(
    project: Path,
    predicate,
    *,
    agent: str = "claude",
    timeout: float = JOURNEY_TIMEOUT,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for record in events(project, agent):
            if predicate(record):
                return record
        time.sleep(0.05)
    raise AssertionError(
        "lifecycle event never appeared; log="
        + json.dumps(events(project, agent), indent=2)
    )


class Watcher:
    """One rt-wait-inbox hook process attached to a pty, as Claude runs it."""

    def __init__(self, project: Path, environment: dict[str, str], *args: str):
        self.master, slave = pty.openpty()
        self.output = bytearray()
        self._stopped = threading.Event()
        self.process = subprocess.Popen(
            [sys.executable, str(BIN / "rt-wait-inbox"), *args],
            cwd=project,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        while not self._stopped.is_set():
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                break
            if not chunk:
                break
            self.output.extend(chunk)

    def text(self) -> str:
        return self.output.decode("utf-8", errors="replace")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait(timeout=5)
        self._stopped.set()
        try:
            os.close(self.master)
        except OSError:
            pass
        self._reader.join(timeout=1)


@pytest.fixture
def watchers():
    started: list[Watcher] = []

    def factory(project: Path, environment: dict[str, str], *args: str) -> Watcher:
        watcher = Watcher(project, environment, *args)
        started.append(watcher)
        return watcher

    yield factory
    for watcher in started:
        watcher.close()


def test_hook_timeout_constant_matches_the_packaged_claude_hook(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    from pneu_packaging import setup as packaged_setup

    wait = load_wait_module("rt_wait_timeout_constant")
    assert (
        wait.CLAUDE_HOOK_TIMEOUT_SECONDS
        == packaged_setup.CLAUDE_HOOK_TIMEOUT_SECONDS
    )
    # The self-imposed lifetime must retire the watcher before Claude Code
    # cancels the hook, or the planned re-arm never runs.
    assert (
        wait.DEFAULT_WATCHER_MAX_LIFETIME_SECONDS
        < wait.CLAUDE_HOOK_TIMEOUT_SECONDS
    )


def test_arm_and_mail_exit_are_recorded_with_identity(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    deliver(project, "message-pending.md")

    result = subprocess.run(
        [sys.executable, str(BIN / "rt-wait-inbox"), "--claude-hook", "claude"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    stream = [record["event"] for record in events(project)]
    assert "armed" in stream
    armed = last_event(project, "armed")
    assert armed["hook"] == "claude-session-start"
    assert armed["session_id"] == environment["RT_SESSION_ID"]
    assert armed["lease_revision"] == environment["RT_LEASE_REVISION"]
    assert armed["generation"] == ["message-pending.md"]
    assert isinstance(armed["watcher_pid"], int)
    wake = last_event(project, "wake")
    assert wake["messages"] == ["message-pending.md"]
    exit_record = last_event(project, "exit")
    assert exit_record["code"] == 2
    assert exit_record["reason"] == "mail"
    assert last_event(project, "supervisor_child_exit")["code"] == 2


def test_duplicate_hook_stand_down_is_recorded(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    _rtruntime.update_wake(
        project,
        "claude",
        environment["RT_SESSION_ID"],
        environment["RT_LEASE_REVISION"],
        watcher_pid=os.getpid(),
    )

    result = subprocess.run(
        [sys.executable, str(BIN / "rt-wait-inbox"), "--claude-hook", "claude"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    stand_down = last_event(project, "stand_down")
    assert stand_down["reason"] == "duplicate-live-watcher"


def test_planned_lifetime_retires_the_watcher_with_a_quiet_notice(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    environment["RT_WATCHER_MAX_LIFETIME_SECONDS"] = "0.2"

    result = subprocess.run(
        [sys.executable, str(BIN / "rt-wait-inbox"), "--claude-hook", "claude"],
        cwd=project,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=JOURNEY_TIMEOUT,
    )

    assert result.returncode == 2
    assert "planned" in result.stderr
    assert "do not drain the inbox for this notice" in result.stderr.lower()
    stream = [record["event"] for record in events(project)]
    assert "lifetime_rearm" in stream
    assert last_event(project, "exit")["reason"] == "lifetime-rearm"
    # The retirement is not a mail generation, so it cannot arm the Stop-hook
    # breaker against the next real message.
    inspection = _rtruntime.inspect_seat(project, "claude")
    assert inspection.token is not None
    assert inspection.token.last_wake_messages == ()


def test_sigterm_records_a_signal_exit_on_both_processes(
    tmp_path, monkeypatch, watchers
):
    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    watcher = watchers(project, environment, "--claude-hook", "claude")
    armed = wait_for_event(project, lambda record: record["event"] == "armed")

    watcher.process.send_signal(signal.SIGTERM)
    assert watcher.process.wait(timeout=JOURNEY_TIMEOUT) == 0

    forwarded = wait_for_event(
        project, lambda record: record["event"] == "supervisor_signal"
    )
    assert forwarded["signal"] == "SIGTERM"
    assert forwarded["child_pid"] == armed["watcher_pid"]
    signalled = wait_for_event(
        project,
        lambda record: record["event"] == "exit" and record.get("signal") == "SIGTERM",
    )
    assert signalled["reason"] == "signal"
    # A watcher that stood down must not leave a heartbeat behind.
    inspection = _rtruntime.inspect_seat(project, "claude")
    assert not inspection.wake_healthy


def test_killed_watcher_is_re_armed_and_still_delivers_mail(
    tmp_path, monkeypatch, watchers
):
    """The self-heal journey: an uncatchable kill must not deafen the seat."""

    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    watcher = watchers(project, environment, "--claude-hook", "claude")

    first = wait_for_event(project, lambda record: record["event"] == "armed")
    assert first["supervisor_pid"] == watcher.process.pid
    assert first["watcher_pid"] != watcher.process.pid
    deadline = time.monotonic() + JOURNEY_TIMEOUT
    while time.monotonic() < deadline:
        if _rtruntime.inspect_seat(project, "claude").wake_healthy:
            break
        time.sleep(0.05)
    assert _rtruntime.inspect_seat(project, "claude").wake_healthy

    os.kill(first["watcher_pid"], signal.SIGKILL)

    killed = wait_for_event(
        project, lambda record: record["event"] == "watcher_killed"
    )
    assert killed["signal"] == "SIGKILL"
    assert killed["child_pid"] == first["watcher_pid"]
    restart = wait_for_event(
        project, lambda record: record["event"] == "supervisor_restart"
    )
    assert restart["attempt"] == 1
    second = wait_for_event(
        project,
        lambda record: record["event"] == "armed"
        and record["watcher_pid"] != first["watcher_pid"],
    )
    assert second["supervisor_pid"] == watcher.process.pid
    assert watcher.process.poll() is None

    deliver(project, "message-after-heal.md")
    assert watcher.process.wait(timeout=JOURNEY_TIMEOUT) == 2
    text = watcher.text()
    assert "message-after-heal.md" in text
    assert "Roundtable mail arrived" in text
    wake = wait_for_event(project, lambda record: record["event"] == "wake")
    assert wake["messages"] == ["message-after-heal.md"]


def test_unsupervised_kill_leaves_the_seat_deaf_and_unlogged(
    tmp_path, monkeypatch, watchers
):
    """Mutation counterpart: without supervision the same kill is terminal.

    This is the failing half of the self-heal journey.  If the supervisor is
    removed, ``test_killed_watcher_is_re_armed_and_still_delivers_mail``
    degrades into exactly this behavior.
    """

    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    environment["RT_WATCHER_NO_SUPERVISOR"] = "1"
    watcher = watchers(project, environment, "--claude-hook", "claude")

    armed = wait_for_event(project, lambda record: record["event"] == "armed")
    # Absent fields are omitted from a record: an unsupervised watcher has no
    # supervisor pid to report.
    assert "supervisor_pid" not in armed
    assert armed["watcher_pid"] == watcher.process.pid

    os.kill(watcher.process.pid, signal.SIGKILL)
    assert watcher.process.wait(timeout=JOURNEY_TIMEOUT) == -signal.SIGKILL

    deliver(project, "message-never-woken.md")
    time.sleep(0.5)
    stream = [record["event"] for record in events(project)]
    assert stream.count("armed") == 1
    assert "wake" not in stream
    assert "exit" not in stream

    summary = _rtruntime.watcher_lifecycle_summary(
        project, "claude", watcher_live=False
    )
    assert summary["verdict"] == "unlogged-death"
    assert summary["last_exit"] is None


def test_supervised_child_stands_down_when_the_hook_process_dies(
    tmp_path, monkeypatch, watchers
):
    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    watcher = watchers(project, environment, "--claude-hook", "claude")
    armed = wait_for_event(project, lambda record: record["event"] == "armed")

    os.kill(watcher.process.pid, signal.SIGKILL)
    watcher.process.wait(timeout=JOURNEY_TIMEOUT)

    orphaned = wait_for_event(
        project, lambda record: record["event"] == "supervisor_exited"
    )
    assert orphaned["supervisor_pid"] == watcher.process.pid
    wait_for_event(
        project,
        lambda record: record["event"] == "exit"
        and record.get("reason") == "supervisor-exited",
    )
    deadline = time.monotonic() + JOURNEY_TIMEOUT
    while time.monotonic() < deadline:
        try:
            os.kill(armed["watcher_pid"], 0)
        except OSError:
            break
        time.sleep(0.05)
    with pytest.raises(OSError):
        os.kill(armed["watcher_pid"], 0)
    assert not _rtruntime.inspect_seat(project, "claude").wake_healthy


def test_crash_self_heals_in_place_and_records_the_traceback(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    for name in ("RT_PROJECT_ROOT", "RT_FROM", "RT_SESSION_ID", "RT_LEASE_REVISION"):
        monkeypatch.setenv(name, environment[name])
    wait = load_wait_module("rt_wait_crash_self_heal")
    wait.POLL_SECONDS = 0.01
    wait.SELF_HEAL_BACKOFF_SECONDS = 0.01
    monkeypatch.setattr(wait, "_project_root", lambda: project)
    original = wait._mailbox_snapshot
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        # Fail inside the armed poll loop, not before the claim, so the
        # restart has to re-arm a live watcher rather than retry startup.
        if state["calls"] == 2:
            raise OSError("simulated transient runtime failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(wait, "_mailbox_snapshot", flaky)
    result: dict[str, int] = {}
    thread = threading.Thread(
        target=lambda: result.update(code=wait.run("claude", 0, claude_hook=True))
    )
    thread.start()
    try:
        wait_for_event(project, lambda record: record["event"] == "self_heal_restart")
        deliver(project, "message-after-crash.md")
        thread.join(timeout=JOURNEY_TIMEOUT)
    finally:
        if thread.is_alive():
            deliver(project, "message-cleanup.md")
            thread.join(timeout=JOURNEY_TIMEOUT)

    assert result == {"code": 2}
    stream = [record["event"] for record in events(project)]
    assert stream.count("armed") == 2
    crash = last_event(project, "crash")
    assert crash["error_type"] == "OSError"
    assert "simulated transient runtime failure" in crash["traceback"]
    assert last_event(project, "self_heal_restart")["attempt"] == 1
    assert last_event(project, "exit")["reason"] == "mail"


def test_crash_without_self_heal_is_terminal(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    environment = claim_environment(monkeypatch, tmp_path / "runtime", project)
    for name in ("RT_PROJECT_ROOT", "RT_FROM", "RT_SESSION_ID", "RT_LEASE_REVISION"):
        monkeypatch.setenv(name, environment[name])
    monkeypatch.setenv("RT_WATCHER_SELF_HEAL", "0")
    wait = load_wait_module("rt_wait_crash_terminal")
    wait.POLL_SECONDS = 0.01
    monkeypatch.setattr(wait, "_project_root", lambda: project)

    def always_fails(*_args, **_kwargs):
        raise OSError("simulated permanent runtime failure")

    monkeypatch.setattr(wait, "_mailbox_snapshot", always_fails)

    with pytest.raises(OSError):
        wait.run("claude", 0, claude_hook=True)

    stream = [record["event"] for record in events(project)]
    assert "crash" in stream
    assert "self_heal_restart" not in stream


def test_lifecycle_log_is_private_and_rotates(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    claim_environment(monkeypatch, tmp_path / "runtime", project)
    path = _rtruntime.watcher_log_path(project, "claude")

    assert _rtruntime.log_watcher_event(project, "claude", "armed", note="first")
    assert path.stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(_rtruntime, "WATCHER_LOG_MAX_BYTES", 200)
    for index in range(40):
        _rtruntime.log_watcher_event(project, "claude", "armed", note=index)
    assert path.with_name(path.name + ".1").exists()
    assert path.stat().st_size <= 4096
    assert events(project)


def test_lifecycle_logging_never_raises_on_a_broken_runtime(tmp_path):
    missing = tmp_path / "not-a-project"
    assert _rtruntime.log_watcher_event(missing, "claude", "armed") is False
    assert _rtruntime.read_watcher_events(missing, "claude") == []


def test_lifecycle_reader_tolerates_a_torn_tail(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    claim_environment(monkeypatch, tmp_path / "runtime", project)
    _rtruntime.log_watcher_event(project, "claude", "armed", note="good")
    path = _rtruntime.watcher_log_path(project, "claude")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"schema": "roundtable.watcher-lifecy')

    records = events(project)
    assert [record["event"] for record in records] == ["armed"]
    summary = _rtruntime.watcher_lifecycle_summary(project, "claude")
    assert summary["verdict"] == "armed"
