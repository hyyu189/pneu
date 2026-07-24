from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtruntime


def write_project(path: Path, agent: str = "claude") -> Path:
    project = path.resolve()
    state = project / ".roundtable"
    state.mkdir(parents=True)
    harness = "claude-code" if agent.startswith("claude") else "hermes-agent"
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        f"  {agent}:\n"
        f"    harness: {harness}\n"
        "    instances:\n"
        f"      - id: {agent}\n"
    )
    return project


def claim_environment(
    monkeypatch,
    runtime: Path,
    project: Path,
    agent: str = "claude",
    *,
    owner_pid: int | None = None,
) -> dict[str, str]:
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    harness = "claude" if agent.startswith("claude") else "hermes"
    token = _rtruntime.claim(
        project,
        agent,
        harness,
        owner_pid=owner_pid or os.getpid(),
    )
    environment = os.environ.copy()
    environment.update(
        {
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": agent,
            "RT_SESSION_ID": token.session_id,
            "RT_LEASE_REVISION": str(token.revision),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return environment


def run_tool(
    name: str,
    *args: str,
    cwd: Path,
    env: dict[str, str],
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(BIN / name), *args],
        cwd=cwd,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def assert_no_project_liveness(project: Path, agent: str = "claude") -> None:
    inbox = project / ".roundtable" / "inbox" / agent
    if not inbox.exists():
        return
    forbidden = {
        path.name
        for path in inbox.iterdir()
        if path.name.startswith(".armed-")
        or path.name in {".last-active", ".empty-beats"}
    }
    assert forbidden == set()


def test_wait_requires_fenced_session_and_never_creates_project_markers(tmp_path):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = os.environ.copy()
    environment.update(
        {
            "RT_RUNTIME_DIR": str(runtime),
            "RT_CODEX_RUNTIME_DIR": str(runtime),
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": "claude",
        }
    )
    environment.pop("RT_SESSION_ID", None)
    environment.pop("RT_LEASE_REVISION", None)

    result = run_tool(
        "rt-wait-inbox", "claude", "0", cwd=project, env=environment
    )

    assert result.returncode == 2
    assert "RT_SESSION_ID" in result.stderr
    assert "RT_LEASE_REVISION" in result.stderr
    assert_no_project_liveness(project)
    assert not runtime.exists()


def test_global_claude_hook_is_noop_outside_managed_sessions(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    environment = os.environ.copy()
    for name in (
        "RT_PROJECT_ROOT",
        "RT_FROM",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
    ):
        environment.pop(name, None)

    result = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        "claude",
        "0",
        cwd=outside,
        env=environment,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_claude_hook_fails_closed_on_partial_managed_context(tmp_path):
    project = write_project(tmp_path / "project")
    environment = os.environ.copy()
    environment.update(
        {
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": "claude",
        }
    )
    environment.pop("RT_SESSION_ID", None)
    environment.pop("RT_LEASE_REVISION", None)

    result = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 2
    assert "missing claimed-seat environment" in result.stderr


@pytest.mark.parametrize("agent", ["claude", "hermes"])
def test_wait_keeps_maildir_project_local_and_wake_state_host_local(
    tmp_path, monkeypatch, agent
):
    project = write_project(tmp_path / "project", agent)
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project, agent)
    new_dir = project / ".roundtable" / "inbox" / agent / "new"
    new_dir.mkdir(parents=True)
    message = new_dir / "message-1.md"
    message.write_text("[CODEX→CLAUDE question id=message-1] test\n")

    result = run_tool(
        "rt-wait-inbox", agent, "1", cwd=project, env=environment
    )

    assert result.returncode == 0, result.stderr
    assert "mail after 0s" in result.stdout
    assert "message-1.md" in result.stdout
    assert message.is_file()
    assert (new_dir.parent / "cur").is_dir()
    assert (new_dir.parent / "tmp").is_dir()
    assert_no_project_liveness(project, agent)
    assert any(path.is_file() for path in runtime.rglob("*"))


def test_claude_hook_uses_async_rewake_exit_for_mail(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    install_prefix = tmp_path / "managed-prefix"
    environment["ROUNDTABLE_INSTALL_PREFIX"] = str(install_prefix)
    new_dir = project / ".roundtable" / "inbox" / "claude" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-claude.md").write_text(
        "[CODEX→CLAUDE question id=message-claude] test\n"
    )

    result = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        "claude",
        "1",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 2
    assert "message-claude.md" in result.stdout
    assert "Roundtable mail arrived" in result.stderr
    assert (
        f"{install_prefix}/bin/rt-inbox --fenced --archive-quiet-acks -f json"
        in result.stderr
    )
    assert f"{install_prefix}/bin/rt-ack --fenced <id>" in result.stderr
    assert (
        f"{install_prefix}/bin/rt-say --fenced --no-nudge"
        in result.stderr
    )
    assert "Stop hook re-arms" in result.stderr


def test_global_claude_hooks_use_the_claimed_instance_identity(
    tmp_path, monkeypatch
):
    agent = "claude-research"
    project = write_project(tmp_path / "project", agent)
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project, agent)
    new_dir = project / ".roundtable" / "inbox" / agent / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-custom.md").write_text(
        "[CODEX→CLAUDE question id=message-custom] test\n"
    )

    rewake = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        cwd=project,
        env=environment,
    )

    assert rewake.returncode == 2
    assert "message-custom.md" in rewake.stdout

    stop = run_tool(
        "rt-stop-gate",
        cwd=project,
        env=environment,
        input_text="{}",
    )
    assert stop.returncode == 2
    assert agent in stop.stderr


def test_duplicate_claude_session_start_hook_quietly_uses_live_watcher(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    _rtruntime.update_wake(
        project,
        "claude",
        environment["RT_SESSION_ID"],
        environment["RT_LEASE_REVISION"],
        watcher_pid=os.getpid(),
    )

    result = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_claude_hook_uses_async_rewake_exit_for_heartbeat(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)

    result = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 2
    assert "heartbeat timeout after 0m" in result.stdout
    assert "heartbeat completed" in result.stderr
    assert "Stop hook will re-arm" in result.stderr


def test_claude_stop_hook_breaks_an_undrained_mail_retry_loop(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    new_dir = project / ".roundtable" / "inbox" / "claude" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "message-still-pending.md").write_text(
        "[CODEX→CLAUDE question id=message-still-pending] test\n"
    )

    initial_wake = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
    )
    assert initial_wake.returncode == 2

    first_retry = run_tool(
        "rt-wait-inbox",
        "--claude-stop-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
        input_text='{"stop_hook_active": false}',
    )
    assert first_retry.returncode == 2
    assert "message-still-pending.md" in first_retry.stdout

    breaker = run_tool(
        "rt-wait-inbox",
        "--claude-stop-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
        input_text='{"stop_hook_active": true}',
    )
    assert breaker.returncode == 0
    assert "automatic re-wake is paused" in breaker.stderr
    assert "message-still-pending.md" not in breaker.stdout


def test_claude_stop_hook_wakes_a_fresh_late_message_generation(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    inbox = project / ".roundtable" / "inbox" / "claude"
    new_dir = inbox / "new"
    cur_dir = inbox / "cur"
    new_dir.mkdir(parents=True)
    cur_dir.mkdir(parents=True)
    first = new_dir / "message-a.md"
    first.write_text("[CODEX→CLAUDE question id=message-a] first\n")

    initial_wake = run_tool(
        "rt-wait-inbox",
        "--claude-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
    )
    assert initial_wake.returncode == 2
    first.rename(cur_dir / first.name)
    second = new_dir / "message-b.md"
    second.write_text("[CODEX→CLAUDE question id=message-b] late\n")

    late_generation = run_tool(
        "rt-wait-inbox",
        "--claude-stop-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
        input_text='{"stop_hook_active": true}',
    )

    assert late_generation.returncode == 2
    assert "message-b.md" in late_generation.stdout
    assert "automatic re-wake is paused" not in late_generation.stderr


def test_claude_stop_hook_rearms_after_a_successful_drain(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)

    result = run_tool(
        "rt-wait-inbox",
        "--claude-stop-hook",
        "claude",
        "0",
        cwd=project,
        env=environment,
        input_text='{"stop_hook_active": true}',
    )

    assert result.returncode == 2
    assert "heartbeat timeout after 0m" in result.stdout


def test_quiet_ack_does_not_wake_and_empty_heartbeat_backoff_persists(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    new_dir = project / ".roundtable" / "inbox" / "claude" / "new"
    new_dir.mkdir(parents=True)
    (new_dir / "ack-message-1.md").write_text(
        "[CODEX→CLAUDE sync-ack id=message-1] received\n"
    )

    first = run_tool(
        "rt-wait-inbox", "claude", "0", cwd=project, env=environment
    )
    second = run_tool(
        "rt-wait-inbox", "claude", "0", cwd=project, env=environment
    )

    assert first.returncode == second.returncode == 0
    assert "heartbeat timeout after 0m" in first.stdout
    assert "consecutive empty beats: 1" in first.stdout
    assert "consecutive empty beats: 2" in second.stdout
    assert "1 quiet ack file(s) pending" in first.stdout
    assert "ack-message-1.md" not in first.stdout.split("heartbeat timeout", 1)[0]
    assert_no_project_liveness(project)


def test_fenced_inbox_archives_quiet_ack_without_a_shell_move(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    inbox = project / ".roundtable" / "inbox" / "claude"
    new_dir = inbox / "new"
    new_dir.mkdir(parents=True)
    msg_id = "20260721T220000Z-codex-to-claude-quiet"
    source = new_dir / f"ack-{msg_id}.md"
    source.write_text(
        f"[CODEX→CLAUDE sync-ack id={msg_id}] refs=original-message\n"
    )

    result = run_tool(
        "rt-inbox",
        "--fenced",
        "--archive-quiet-acks",
        "-f",
        "json",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert "archived 1 quiet acknowledgement" in result.stderr
    assert not source.exists()
    assert (inbox / "cur" / source.name).is_file()


def test_fenced_drain_lists_malformed_mail_and_prints_break_loop_guidance(
    tmp_path, monkeypatch
):
    # The exact hook-driven drain path from the 2026-07-21 field incident:
    # a fenced --archive-quiet-acks listing must surface an unparseable mail
    # file instead of hiding it while it keeps waking the seat.
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    inbox = project / ".roundtable" / "inbox" / "claude"
    new_dir = inbox / "new"
    new_dir.mkdir(parents=True)
    msg_id = "20260721T220000Z-codex-to-claude-quiet"
    ack = new_dir / f"ack-{msg_id}.md"
    ack.write_text(
        f"[CODEX→CLAUDE sync-ack id={msg_id}] refs=original-message\n"
    )
    malformed_stem = "20260721T222645Z-hermes-to-claude-29195"
    malformed = new_dir / f"{malformed_stem}.md"
    malformed.write_text(
        f"[HERMES→CLAUDE CROSS HERMES OK acknowledged id={malformed_stem}] "
        "--kind reply --refs 20260721T222617Z-claude-to-hermes-26393"
    )

    result = run_tool(
        "rt-inbox",
        "--fenced",
        "--archive-quiet-acks",
        "-f",
        "json",
        cwd=project,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [record["msg_id"] for record in payload] == [malformed_stem]
    assert payload[0]["schema"] == "roundtable.maildir_malformed.v1"
    assert payload[0]["kind"] == "malformed"
    assert payload[0]["problem"] == "invalid mail header"
    assert "archived 1 quiet acknowledgement" in result.stderr
    assert "1 malformed mail file(s) remain in new/" in result.stderr
    assert "rt-ack" in result.stderr
    assert not ack.exists()
    assert (inbox / "cur" / ack.name).is_file()
    # The malformed file is never a quiet ack: --archive-quiet-acks leaves it
    # in new/ until it is acknowledged or moved deliberately.
    assert malformed.is_file()


def test_fenced_ack_rejects_an_unanchored_session_before_foreign_archive(
    tmp_path,
):
    project = write_project(tmp_path / "project")
    msg_id = "20260721T220100Z-codex-to-hermes-foreign"
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    source = new_dir / f"{msg_id}.md"
    source.write_text(f"[CODEX→HERMES question id={msg_id}] keep\n")
    environment = os.environ.copy()
    for name in (
        "RT_PROJECT_ROOT",
        "RT_FROM",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
    ):
        environment.pop(name, None)

    result = run_tool(
        "rt-ack",
        "--fenced",
        msg_id,
        cwd=project,
        env=environment,
    )

    assert result.returncode != 0
    assert "--fenced requires a Roundtable-launched seat" in result.stderr
    assert source.is_file()
    assert not (new_dir.parent / "cur" / source.name).exists()


def test_active_fenced_claude_cannot_ack_another_seats_message(
    tmp_path,
    monkeypatch,
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    msg_id = "20260721T220200Z-codex-to-hermes-foreign"
    new_dir = project / ".roundtable" / "inbox" / "hermes" / "new"
    new_dir.mkdir(parents=True)
    source = new_dir / f"{msg_id}.md"
    source.write_text(f"[CODEX→HERMES question id={msg_id}] keep\n")

    result = run_tool(
        "rt-ack",
        "--fenced",
        msg_id,
        cwd=project,
        env=environment,
    )

    assert result.returncode != 0
    assert "fenced seat claude cannot acknowledge mail for hermes" in result.stderr
    assert source.is_file()
    assert not (new_dir.parent / "cur" / source.name).exists()


def test_global_stop_gate_is_noop_for_direct_launch_but_partial_lease_fails(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    environment = os.environ.copy()
    for name in (
        "RT_PROJECT_ROOT",
        "RT_FROM",
        "RT_SESSION_ID",
        "RT_LEASE_REVISION",
    ):
        environment.pop(name, None)

    noop = run_tool(
        "rt-stop-gate",
        "claude",
        cwd=outside,
        env=environment,
        input_text="{}",
    )
    assert noop.returncode == 0

    project = write_project(tmp_path / "project")
    direct = run_tool(
        "rt-stop-gate",
        "claude",
        cwd=project,
        env=environment,
        input_text="{}",
    )
    assert direct.returncode == 0

    partial_environment = {
        **environment,
        "RT_PROJECT_ROOT": str(project),
        "RT_FROM": "claude",
    }
    partial = run_tool(
        "rt-stop-gate",
        "claude",
        cwd=project,
        env=partial_environment,
        input_text="{}",
    )
    assert partial.returncode == 2
    assert "missing claimed-seat environment" in partial.stderr
    assert_no_project_liveness(project)


def test_stop_gate_recursion_flag_accepts_pretty_json_without_a_lease(tmp_path):
    project = write_project(tmp_path / "project")
    environment = os.environ.copy()
    environment.update(
        {
            "RT_PROJECT_ROOT": str(project),
            "RT_FROM": "claude",
        }
    )
    environment.pop("RT_SESSION_ID", None)
    environment.pop("RT_LEASE_REVISION", None)

    result = run_tool(
        "rt-stop-gate",
        "claude",
        cwd=project,
        env=environment,
        input_text='{\n\t"stop_hook_active"\t:\n true\n}',
    )

    assert result.returncode == 0, result.stderr


def test_stop_gate_requires_live_host_runtime_tripwire(tmp_path, monkeypatch):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    # A fresh heartbeat without a watcher PID may be valid for other adapters,
    # but it is not proof that this tripwire is armed.
    _rtruntime.update_wake(
        project,
        "claude",
        environment["RT_SESSION_ID"],
        environment["RT_LEASE_REVISION"],
    )

    result = run_tool(
        "rt-stop-gate",
        "claude",
        cwd=project,
        env=environment,
        input_text="{}",
    )

    assert result.returncode == 2
    assert "no live inbox tripwire" in result.stderr
    assert_no_project_liveness(project)


def wait_until_healthy(
    project: Path, agent: str, *, timeout: float = 5.0
) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _rtruntime.inspect_seat(project, agent)
        if getattr(last, "status", None) == "active_healthy":
            return
        time.sleep(0.02)
    pytest.fail(f"tripwire never became healthy: {last}")


def test_stop_gate_accepts_live_tripwire_and_blocks_undrained_mail(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    environment = claim_environment(monkeypatch, runtime, project)
    watcher = subprocess.Popen(
        [sys.executable, str(BIN / "rt-wait-inbox"), "claude", "1"],
        cwd=project,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_until_healthy(project, "claude")
        allowed = run_tool(
            "rt-stop-gate",
            "claude",
            cwd=project,
            env=environment,
            input_text="{}",
        )
        assert allowed.returncode == 0, allowed.stderr

        new_dir = project / ".roundtable" / "inbox" / "claude" / "new"
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "message-2.md").write_text(
            "[CODEX→CLAUDE question id=message-2] test\n"
        )
        blocked = run_tool(
            "rt-stop-gate",
            "claude",
            cwd=project,
            env=environment,
            input_text="{}",
        )
        assert blocked.returncode == 2
        assert "undrained mail: message-2.md" in blocked.stderr
    finally:
        watcher.terminate()
        watcher.communicate(timeout=5)
    assert_no_project_liveness(project)


def test_old_watcher_cannot_clear_replacement_lease_wake_state(
    tmp_path, monkeypatch
):
    project = write_project(tmp_path / "project")
    runtime = tmp_path / "runtime"
    owner = subprocess.Popen(["/bin/sleep", "0.2"])
    old_environment = claim_environment(
        monkeypatch, runtime, project, owner_pid=owner.pid
    )
    old = subprocess.Popen(
        [sys.executable, str(BIN / "rt-wait-inbox"), "claude", "10"],
        cwd=project,
        env=old_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        wait_until_healthy(project, "claude")
        owner.wait(timeout=3)
        new_environment = claim_environment(monkeypatch, runtime, project)
        _rtruntime.update_wake(
            project,
            "claude",
            new_environment["RT_SESSION_ID"],
            new_environment["RT_LEASE_REVISION"],
            watcher_pid=os.getpid(),
        )
        old.terminate()
        old.communicate(timeout=5)

        token = _rtruntime.load_validated_lease(
            project,
            "claude",
            new_environment["RT_SESSION_ID"],
            new_environment["RT_LEASE_REVISION"],
        )
        assert getattr(token, "watcher_pid", None) == os.getpid()
        assert _rtruntime.inspect_seat(project, "claude").status == "active_healthy"
    finally:
        if old.poll() is None:
            old.kill()
            old.communicate(timeout=5)
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
    assert_no_project_liveness(project)
