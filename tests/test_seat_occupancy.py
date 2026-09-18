"""Phase 1 occupancy: one holder/locus interpretation, fenced takeover, jump.

Contracts defended here:

- the resolver renders vacant / active / stale / ambiguous from runtime facts
  and degrades to a bare ``active`` rather than guessing a locus;
- a surface is attributed to a lease only when it provably belongs to that
  lease generation (fenced capability, or an advisory record written after
  the claim);
- a Claude phone session is recognised from the rc-host registration or from
  the owner's command line, never from a tty it does not have;
- the refusal names the holder and the next action, and keeps the forensics;
- takeover replaces exactly the fenced holder lease under this pid and fails
  closed on any change; it never widens what a claim may displace;
- jump is navigation only: it writes nothing and is offered only when the
  recorded surface answers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import _kit as kit


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _rtlauncher
import _rtruntime
import _rtsurface
from _rtruntime import claim, inspect_seat, record_seat_capability, record_seat_surface, release


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("RT_RUNTIME_DIR", str(root))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(root))
    monkeypatch.setattr(_rtlauncher, "iter_states", lambda: [])
    return root


@pytest.fixture
def project(tmp_path):
    return kit.write_project(tmp_path / "project", [kit.CLAUDE, kit.CODEX])


@pytest.fixture
def holder():
    """A live process that is not this test process."""

    child = subprocess.Popen(["/bin/sleep", "60"])
    try:
        yield child
    finally:
        child.kill()
        child.wait()


def no_probe(monkeypatch, evidence=None):
    monkeypatch.setattr(
        _rtlauncher, "_probe_owner_process", lambda _pid: dict(evidence or {})
    )


def test_vacant_seat_renders_vacant(runtime, project, monkeypatch):
    no_probe(monkeypatch)
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    assert occupancy.state == "vacant"
    assert occupancy.row_text() == "vacant"
    assert occupancy.takeover_eligible is False
    assert _rtlauncher.jump_reachability(occupancy) == (False, None)


def test_active_seat_without_locus_renders_active_and_start_time(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    token = claim(project, "claude", "claude", owner_pid=holder.pid)

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    assert occupancy.state == "active"
    assert occupancy.holder_harness == "claude"
    assert occupancy.surface is None
    assert occupancy.phone is False
    assert occupancy.since is not None
    assert occupancy.row_text().startswith(f"active — since {occupancy.since}")
    assert occupancy.wake_unhealthy is True  # fresh lease, no heartbeat yet
    assert occupancy.row_text().endswith("· wake unhealthy")
    assert occupancy.holder_phrase() == f"a Claude Code session started {occupancy.since}"
    assert occupancy.takeover_eligible is True
    assert release(token)


def test_stale_seat_renders_the_runtime_detail(runtime, project, monkeypatch):
    no_probe(monkeypatch)
    child = subprocess.Popen(["/bin/sleep", "60"])
    token = claim(project, "codex", "codex", owner_pid=child.pid)
    child.kill()
    child.wait()

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "codex", "codex")

    assert occupancy.state == "stale"
    assert occupancy.row_text() == f"stale — owner pid {child.pid} is not running"
    assert occupancy.takeover_eligible is False
    del token


def test_ambiguous_seat_is_neither_vacant_nor_takeover_eligible(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    claim(project, "claude", "claude", owner_pid=holder.pid)
    monkeypatch.setattr(_rtruntime, "_pid_state", lambda _pid: "ambiguous")

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    assert occupancy.state == "ambiguous"
    assert occupancy.row_text().startswith("ambiguous — ")
    assert occupancy.takeover_eligible is False
    taken, detail = _rtlauncher.take_over_seat(project, occupancy)
    assert taken is False
    assert "not takeover-eligible" in detail


def test_fenced_capability_surface_names_the_locus(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch, {"tty": "ttys009"})
    token = claim(project, "codex", "codex", owner_pid=holder.pid)
    record_seat_capability(
        project,
        "codex",
        "codex",
        session_id=token.session_id,
        revision=token.revision,
        surface={"kind": "herdr", "pane": "w1:p3", "tab": "w1:t1"},
    )

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "codex", "codex")

    assert occupancy.surface == {"kind": "herdr", "pane": "w1:p3", "tab": "w1:t1"}
    assert occupancy.locus == "pane w1:p3"
    assert occupancy.row_text() == "active — pane w1:p3 · wake unhealthy"
    assert occupancy.holder_phrase() == "a Codex session in pane w1:p3"
    assert occupancy.tty is None  # a recorded surface settles the locus
    assert release(token)


def test_advisory_surface_counts_only_when_written_after_the_claim(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    first = claim(project, "codex", "codex", owner_pid=holder.pid)
    record_seat_surface(
        project,
        "codex",
        "codex",
        {"kind": "tmux", "target": "build:1.0"},
        session_id=first.session_id,
        revision=first.revision,
    )
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "codex", "codex")
    assert occupancy.locus == "tmux build:1.0"
    assert occupancy.holder_phrase() == "a Codex session in tmux build:1.0"

    # A later lease generation must not inherit the old pane.
    assert release(first)
    surface_path = _rtruntime.seat_paths(project, "codex").surface
    payload = json.loads(surface_path.read_text())
    payload["recordedAt"] = "2000-01-01T00:00:00.000Z"
    surface_path.write_text(json.dumps(payload))
    second = claim(project, "codex", "codex", owner_pid=holder.pid)

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "codex", "codex")
    assert occupancy.surface is None
    assert occupancy.locus == f"since {occupancy.since}"
    assert release(second)


def test_phone_session_is_recognised_from_the_rc_host_registration(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch, {"tty": "none"})
    token = claim(project, "claude", "claude", owner_pid=holder.pid)
    monkeypatch.setattr(
        _rtlauncher,
        "iter_states",
        lambda: [
            {
                "lastRegistration": {
                    "projectRoot": str(project),
                    "agent": "claude",
                    "sessionId": token.session_id,
                }
            }
        ],
    )

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    assert occupancy.phone is True
    assert occupancy.locus == f"phone session since {occupancy.since}"
    assert occupancy.holder_phrase() == f"a Claude phone session started {occupancy.since}"
    assert occupancy.next_action().startswith("Resume it there")
    assert release(token)


def test_phone_session_is_recognised_from_the_owner_command_line(
    runtime, project, holder, monkeypatch
):
    no_probe(
        monkeypatch,
        {
            "tty": "none",
            "command": "claude --print --sdk-url wss://example.invalid/cse_1",
        },
    )
    token = claim(project, "claude", "claude", owner_pid=holder.pid)

    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    assert occupancy.phone is True
    assert occupancy.tty is None
    assert release(token)


def test_terminal_evidence_names_tmux_then_tty(runtime, project, holder, monkeypatch):
    token = claim(project, "claude", "claude", owner_pid=holder.pid)

    no_probe(monkeypatch, {"tty": "ttys017", "tmux": "build:editor"})
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")
    assert occupancy.locus == f"tmux build:editor since {occupancy.since}"
    assert occupancy.holder_phrase() == (
        f"a Claude Code session in tmux build:editor started {occupancy.since}"
    )

    no_probe(monkeypatch, {"tty": "ttys017"})
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")
    assert occupancy.locus == f"tty ttys017 since {occupancy.since}"
    assert occupancy.holder_phrase() == (
        f"a Claude Code session on tty ttys017 started {occupancy.since}"
    )
    assert release(token)


def test_refusal_names_holder_next_action_and_forensics(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    token = claim(project, "claude", "claude", owner_pid=holder.pid)
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    text = _rtlauncher.occupied_seat_refusal("rt-claude", project, occupancy)
    lines = text.splitlines()

    assert lines[0] == (
        f"rt-claude: seat 'claude' in {project} is held by "
        f"a Claude Code session started {occupancy.since} (wake unhealthy)."
    )
    assert lines[1] == (
        "  Return to it there, or run `pneu` and take the seat over from the card."
    )
    assert lines[2] == f"  (owner pid {holder.pid} is running; wake adapter has no heartbeat)"
    assert release(token)


def test_takeover_replaces_exactly_the_holder_lease_under_this_pid(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    old = claim(project, "claude", "claude", owner_pid=holder.pid)
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    taken, detail = _rtlauncher.take_over_seat(project, occupancy)

    assert taken is True
    assert detail.startswith("took over seat 'claude' from a Claude Code session")
    inspection = inspect_seat(project, "claude")
    assert inspection.status in {"active_healthy", "active_unhealthy"}
    assert inspection.token.owner_pid == os.getpid()
    assert inspection.token.session_id != old.session_id
    # The displaced holder's fence no longer matches: it lost the seat.
    assert release(old) is False
    # A launcher entered through execv keeps this pid and re-enters the lease.
    same = _rtlauncher._same_process_lease(project, "claude", "claude")
    assert same is not None and same.session_id == inspection.token.session_id
    assert release(inspection.token)


def test_takeover_fails_closed_when_the_seat_changed_under_the_fence(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    old = claim(project, "claude", "claude", owner_pid=holder.pid)
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "claude", "claude")

    # A different generation took the seat between the card and the decision.
    assert release(old)
    replacement = claim(project, "claude", "claude", owner_pid=holder.pid)

    taken, detail = _rtlauncher.take_over_seat(project, occupancy)
    assert taken is False
    assert "changed under you" in detail
    after = inspect_seat(project, "claude")
    assert after.token.session_id == replacement.session_id
    assert after.token.owner_pid == holder.pid

    # A vanished holder is a refusal too, never a silent fresh claim.
    assert release(replacement)
    taken, detail = _rtlauncher.take_over_seat(project, occupancy)
    assert taken is False
    assert "changed under you" in detail
    assert inspect_seat(project, "claude").status == "vacant"


@pytest.mark.parametrize("mismatched_field", ["session_id", "revision"])
def test_claim_replace_fence_rejects_a_mismatched_generation(
    runtime, project, holder, mismatched_field
):
    token = claim(project, "codex", "codex", owner_pid=holder.pid)
    fence = {"session_id": token.session_id, "revision": token.revision}
    fence[mismatched_field] = "wrong-generation"

    with pytest.raises(_rtruntime.FenceRejected):
        claim(
            project,
            "codex",
            "codex",
            replace_fence=(fence["session_id"], fence["revision"]),
        )
    with pytest.raises(_rtruntime.RuntimeStateError):
        claim(project, "codex", "codex", replace_fence=("only-one",))  # type: ignore[arg-type]
    assert inspect_seat(project, "codex").token.session_id == token.session_id
    assert release(token)


def test_old_takeover_fence_cannot_replace_a_new_stale_generation(
    runtime, project, holder
):
    old = claim(project, "codex", "codex", owner_pid=holder.pid)
    assert release(old)
    replacement = claim(project, "codex", "codex", owner_pid=holder.pid)
    holder.kill()
    holder.wait()
    assert inspect_seat(project, "codex").status == "stale"
    lease_path = _rtruntime.seat_paths(project, "codex").lease
    before = lease_path.read_bytes()

    with pytest.raises(_rtruntime.FenceRejected):
        claim(
            project,
            "codex",
            "codex",
            replace_fence=(old.session_id, old.revision),
        )

    assert lease_path.read_bytes() == before
    assert inspect_seat(project, "codex").token == replacement


@pytest.mark.parametrize("mismatched_field", ["session_id", "revision"])
def test_stale_takeover_requires_both_fence_fields_to_match(
    runtime, project, holder, mismatched_field
):
    token = claim(project, "codex", "codex", owner_pid=holder.pid)
    holder.kill()
    holder.wait()
    lease_path = _rtruntime.seat_paths(project, "codex").lease
    before = lease_path.read_bytes()
    fence = {"session_id": token.session_id, "revision": token.revision}
    fence[mismatched_field] = "wrong-generation"

    with pytest.raises(_rtruntime.FenceRejected):
        claim(
            project,
            "codex",
            "codex",
            replace_fence=(fence["session_id"], fence["revision"]),
        )

    assert lease_path.read_bytes() == before


@pytest.mark.parametrize("guarded", [False, True])
def test_stale_generation_can_be_replaced_without_a_fence_or_with_its_exact_fence(
    runtime, project, holder, guarded
):
    token = claim(project, "codex", "codex", owner_pid=holder.pid)
    holder.kill()
    holder.wait()

    replacement = claim(
        project,
        "codex",
        "codex",
        replace_fence=(token.session_id, token.revision) if guarded else None,
    )

    assert replacement.owner_pid == os.getpid()
    assert replacement.session_id != token.session_id
    assert release(token) is False
    assert release(replacement)


def fake_runner(calls, outputs=None):
    outputs = outputs or {}

    def runner(command, **_kwargs):
        calls.append(list(command))
        stdout = ""
        for marker, payload in outputs.items():
            if marker in command:
                stdout = payload
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return runner


def test_tmux_jump_selects_the_recorded_pane_and_writes_nothing(tmp_path):
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/sh\nexit 0\n")
    tmux.chmod(0o755)
    calls = []

    focused = _rtsurface.focus_capability_surface(
        {"kind": "tmux", "target": "build:1.0", "endpoint": "/tmp/tmux-sock"},
        environ={"PATH": str(tmp_path), "TMUX": "/tmp/tmux-sock,1,0"},
        runner=fake_runner(calls),
    )

    assert focused == "tmux build:1.0"
    assert [command[1:] for command in calls] == [
        ["-S", "/tmp/tmux-sock", "select-window", "-t", "build:1.0"],
        ["-S", "/tmp/tmux-sock", "select-pane", "-t", "build:1.0"],
        ["-S", "/tmp/tmux-sock", "switch-client", "-t", "build:1.0"],
    ]
    assert not any(path.name.endswith(".json") for path in tmp_path.rglob("*"))


def test_herdr_jump_focuses_the_tab_showing_the_pane(tmp_path):
    herdr = tmp_path / "herdr"
    herdr.write_text("#!/bin/sh\nexit 0\n")
    herdr.chmod(0o755)
    calls = []
    pane_info = json.dumps(
        {"result": {"pane": {"pane_id": "w1:p3", "tab_id": "w1:t1", "workspace_id": "w1"}}}
    )

    focused = _rtsurface.focus_capability_surface(
        {"kind": "herdr", "pane": "w1:p3"},
        environ={"PATH": str(tmp_path), "HERDR_ENV": "1"},
        runner=fake_runner(calls, {"get": pane_info}),
    )

    assert focused == "pane w1:p3"
    assert [command[1:] for command in calls] == [
        ["pane", "get", "w1:p3"],
        ["workspace", "focus", "w1"],
        ["tab", "focus", "w1:t1"],
    ]


def test_herdr_jump_outside_a_pane_without_a_broker_fails_closed():
    with pytest.raises(_rtsurface.SurfaceError) as error:
        _rtsurface.focus_capability_surface(
            {"kind": "herdr", "pane": "w1:p3"},
            environ={"PATH": "/usr/bin"},
        )
    assert "never fabricates HERDR_ENV=1" in str(error.value)


def test_jump_is_offered_only_for_a_surface_that_answers(
    runtime, project, holder, monkeypatch
):
    no_probe(monkeypatch)
    token = claim(project, "codex", "codex", owner_pid=holder.pid)
    record_seat_capability(
        project,
        "codex",
        "codex",
        session_id=token.session_id,
        revision=token.revision,
        surface={"kind": "herdr", "pane": "w1:p3"},
    )
    occupancy = _rtlauncher.inspect_seat_occupancy(project, "codex", "codex")

    monkeypatch.setattr(
        _rtlauncher,
        "probe_capability_surface",
        lambda _surface: (_ for _ in ()).throw(_rtsurface.SurfaceError("pane gone")),
    )
    assert _rtlauncher.jump_reachability(occupancy) == (False, "pane gone")

    monkeypatch.setattr(_rtlauncher, "probe_capability_surface", lambda _surface: None)
    assert _rtlauncher.jump_reachability(occupancy) == (True, None)
    assert release(token)
