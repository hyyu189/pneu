"""Journey-tier coverage for the three paths every pneu user walks.

These tests deliberately avoid stubbing the machinery under test. They create a
real project with the real ``roundtable-init``, drive the real launcher card
over a real pty, arm the real inbox watcher as a real process, and move real
mail with ``rt-say`` / ``rt-inbox`` / ``rt-ack``. The only interception is
``pneu``'s final ``os.execv``: replacing this process is the one step a test
cannot let happen.

Guards these journeys pin are mutation-checked in
``tests/test_journey_mutation.py``.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import threading
import time
import tty

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

from _rtruntime import claim, inspect_seat, release  # noqa: E402


INIT = BIN / "roundtable-init"
WAIT_INBOX = BIN / "rt-wait-inbox"
SAY = BIN / "rt-say"
INBOX = BIN / "rt-inbox"
ACK = BIN / "rt-ack"

# The watcher scans at most every POLL_SECONDS (5s in _rtruntime terms), so a
# send issued after arming is observed within one scan plus process overhead.
WAKE_TIMEOUT_SECONDS = 20.0
ACTIVATION_TIMEOUT_SECONDS = 20.0


def _load_pneu():
    loader = importlib.machinery.SourceFileLoader("pneu_journey_core", str(BIN / "pneu"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


PNEU = _load_pneu()


def _run_with_pty(callback, input_bytes: bytes) -> tuple[object, str]:
    """Drive ``callback`` with a real terminal on both stdin and stderr."""

    master, slave = pty.openpty()
    tty.setcbreak(slave)
    stdin = os.fdopen(os.dup(slave), "r", encoding="utf-8", buffering=1)
    stderr = os.fdopen(os.dup(slave), "w", encoding="utf-8", buffering=1)
    output = bytearray()
    stopped = threading.Event()

    def drain_master() -> None:
        while not stopped.is_set():
            ready, _write, _error = select.select([master], [], [], 0.05)
            if not ready:
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)

    reader = threading.Thread(target=drain_master, daemon=True)
    reader.start()
    os.write(master, input_bytes)
    try:
        result = callback(stdin, stderr)
        stderr.flush()
    finally:
        stdin.close()
        stderr.close()
        os.close(slave)
        stopped.set()
        reader.join(timeout=1)
    os.close(master)
    return result, output.decode(errors="replace")


@pytest.fixture
def project_lab(tmp_path, monkeypatch):
    """A real registered project with fake but resolvable harness executables."""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    parent = tmp_path / "projects"
    parent.mkdir()
    registry = tmp_path / "projects.yaml"
    runtime = tmp_path / "runtime"

    harness_bin = tmp_path / "harness-bin"
    harness_bin.mkdir()
    for name in ("claude", "codex", "hermes"):
        executable = harness_bin / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("RT_PROJECTS_FILE", str(registry))
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CLAUDE_BIN", str(harness_bin / "claude"))
    monkeypatch.setenv("RT_CODEX_BIN", str(harness_bin / "codex"))
    monkeypatch.setenv("RT_HERMES_BIN", str(harness_bin / "hermes"))
    monkeypatch.delenv("ROUNDTABLE_INSTALL_PREFIX", raising=False)
    monkeypatch.setenv("ROUNDTABLE_ONBOARDING_SUBPROCESS", "1")

    created = subprocess.run(
        [sys.executable, str(INIT), "demo", "--parent", str(parent)],
        cwd=parent,
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    project = (parent / "demo").resolve()
    assert (project / ".roundtable" / "agents.yaml").is_file()
    return project, home


def _seat_environment(project: Path, agent: str, token) -> dict[str, str]:
    environment = os.environ.copy()
    environment["RT_PROJECT_ROOT"] = str(project)
    environment["RT_FROM"] = agent
    environment["RT_SESSION_ID"] = token.session_id
    environment["RT_LEASE_REVISION"] = str(token.revision)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _wait_for(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {description}; last={last!r}")


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_launcher_navigation_journey_arrows_select_and_launch_the_next_seat(
    project_lab,
):
    """Real pty keys move the card cursor and Enter launches that exact seat."""

    project, home = project_lab
    executed: list[tuple[str, list[str]]] = []
    chdir_calls: list[Path] = []
    environ = os.environ.copy()

    def fake_exec(path, argv):
        executed.append((path, list(argv)))
        return 0

    # Enter dismisses the first-run welcome; the down arrow moves off the
    # default seat; the final Enter launches the seat under the cursor.
    result, rendered = _run_with_pty(
        lambda stdin, stderr: PNEU.main(
            [],
            cwd=project,
            home=home,
            stdin=stdin,
            stdout=io.StringIO(),
            stderr=stderr,
            environ=environ,
            exec_runner=fake_exec,
            chdir_runner=chdir_calls.append,
        ),
        b"\n\x1b[B\n",
    )

    assert result == 0, rendered
    assert "↑↓/1-9 select" in rendered
    # Frame ordering is the evidence that the arrow key moved the cursor: the
    # card must first render with claude selected, then redraw with codex
    # selected. Asserting only the final frame would also pass if codex had
    # been the default all along.
    frames = rendered.split("\x1b[2J\x1b[H")
    seat_frames = [frame for frame in frames if "Codex — codex" in frame]
    assert len(seat_frames) == 2, rendered
    assert " > Claude Code — claude" in seat_frames[0]
    assert "   Codex — codex" in seat_frames[0]
    assert "   Claude Code — claude" in seat_frames[1]
    assert " > Codex — codex" in seat_frames[1]

    assert len(executed) == 1, rendered
    path, argv = executed[0]
    assert Path(path) == BIN / "rt-codex"
    assert argv == [str(BIN / "rt-codex")]
    assert environ["RT_FROM"] == "codex"
    assert chdir_calls == [project]

    launcher_state = json.loads(
        (project / ".roundtable" / "launcher.json").read_text(encoding="utf-8")
    )
    assert launcher_state["lastSeat"] == "codex:codex"
    assert launcher_state["welcomePending"] is False


def test_seat_open_journey_reaches_an_active_healthy_lease_and_releases_it(
    project_lab,
):
    """Claiming a seat and arming its watcher is what makes a lease healthy."""

    project, _home = project_lab
    assert inspect_seat(project, "codex").status == "vacant"

    token = claim(project, "codex", "codex")
    watcher = None
    try:
        # A claimed seat with no armed watcher has no heartbeat yet.
        assert inspect_seat(project, "codex").status == "active_unhealthy"

        watcher = subprocess.Popen(
            [sys.executable, str(WAIT_INBOX), "codex"],
            cwd=project,
            env=_seat_environment(project, "codex", token),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        inspection = _wait_for(
            lambda: (
                inspect_seat(project, "codex")
                if inspect_seat(project, "codex").status == "active_healthy"
                else None
            ),
            ACTIVATION_TIMEOUT_SECONDS,
            "the armed watcher to make the lease active_healthy",
        )
        assert inspection.token is not None
        assert inspection.token.session_id == token.session_id
        assert inspection.token.harness == "codex"
        assert inspection.wake_healthy is True
        # Health is the watcher's heartbeat, not merely a claimed lease: the
        # wake slot must name the watcher process we actually started.
        assert inspection.token.watcher_pid == watcher.pid
    finally:
        if watcher is not None:
            _terminate(watcher)
        assert release(token)

    assert inspect_seat(project, "codex").status == "vacant"


def test_mail_journey_send_wakes_the_armed_watcher_and_ack_archives_the_message(
    project_lab,
):
    """send -> wake -> ack, with the maildir as the only fact source."""

    project, _home = project_lab
    mailbox = project / ".roundtable"
    codex_new = mailbox / "inbox" / "codex" / "new"
    codex_cur = mailbox / "inbox" / "codex" / "cur"
    claude_new = mailbox / "inbox" / "claude" / "new"

    token = claim(project, "codex", "codex")
    watcher = None
    try:
        seat_environment = _seat_environment(project, "codex", token)
        watcher = subprocess.Popen(
            [sys.executable, str(WAIT_INBOX), "codex"],
            cwd=project,
            env=seat_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for(
            lambda: inspect_seat(project, "codex").status == "active_healthy",
            ACTIVATION_TIMEOUT_SECONDS,
            "the watcher to arm before any mail exists",
        )
        assert watcher.poll() is None, "an empty inbox must not wake the seat"

        sender_environment = os.environ.copy()
        sender_environment["RT_FROM"] = "claude"
        sender_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        sent = subprocess.run(
            [sys.executable, str(SAY), "--no-nudge", "codex", "task", "journey ping"],
            cwd=project,
            env=sender_environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert sent.returncode == 0, sent.stderr

        stdout, _stderr = watcher.communicate(timeout=WAKE_TIMEOUT_SECONDS)
        assert watcher.returncode == 0, stdout
        assert "rt-wait-inbox: mail after" in stdout
        watcher = None
    finally:
        if watcher is not None:
            _terminate(watcher)

    try:
        listed = subprocess.run(
            [sys.executable, str(INBOX), "-f", "json"],
            cwd=project,
            env=_seat_environment(project, "codex", token),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert listed.returncode == 0, listed.stderr
        messages = json.loads(listed.stdout)
        # Ledger and maildir rows that share one message id are one logical
        # message; the maildir row is the delivery fact.
        assert len({entry["msg_id"] for entry in messages}) == 1, messages
        maildir_rows = [
            entry for entry in messages if entry["delivery_source"] == "maildir"
        ]
        assert len(maildir_rows) == 1, messages
        message = maildir_rows[0]
        assert message["to"] == "codex"
        assert message["from"].startswith("claude")
        assert message["kind"] == "task"
        assert "journey ping" in message["body"]
        assert (codex_new / f"{message['msg_id']}.md").is_file()

        acked = subprocess.run(
            [sys.executable, str(ACK), message["msg_id"]],
            cwd=project,
            env=_seat_environment(project, "codex", token),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        assert acked.returncode == 0, acked.stderr
    finally:
        assert release(token)

    # Archived out of new/ into cur/, and the quiet receipt reached the sender.
    assert not (codex_new / f"{message['msg_id']}.md").exists()
    archived = (
        sorted(codex_cur.glob(f"{message['msg_id']}*")) if codex_cur.is_dir() else []
    )
    assert archived, f"{message['msg_id']} was not archived into {codex_cur}"
    receipts = sorted(claude_new.glob("ack-*")) if claude_new.is_dir() else []
    assert receipts, f"no quiet receipt was delivered to {claude_new}"
