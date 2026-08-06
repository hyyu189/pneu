"""Maildir-only three-seat and cross-worktree OpenClaw interop lab."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

from _rtlib import register_project, resolve_project_mailbox  # noqa: E402


SEATS = ("openclaw", "claude", "codex")


def _write_project(project: Path, registry: Path) -> None:
    state = project / ".roundtable"
    (state / "messages").mkdir(parents=True)
    (state / "locks").mkdir()
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  openclaw:\n"
        "    harness: openclaw\n"
        "    instances:\n"
        "      - id: openclaw\n"
        "  claude:\n"
        "    harness: claude-code\n"
        "    instances:\n"
        "      - id: claude\n"
        "  codex:\n"
        "    harness: codex\n"
        "    instances:\n"
        "      - id: codex\n",
        encoding="utf-8",
    )
    register_project(project, path=registry)


def _cli(name: str, *args: str, cwd: Path, sender: str, registry: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.update(
        {
            "RT_FROM": sender,
            "RT_PROJECTS_FILE": str(registry),
            "CMUX_SURFACE_ID": "",
            "CODEX_THREAD_ID": "",
        }
    )
    return subprocess.run(
        [sys.executable, str(BIN / name), *args],
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _snapshot(root: Path) -> tuple[str, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))


def _send_and_ack(project: Path, registry: Path, sender: str, recipient: str) -> str:
    sent = _cli(
        "rt-say",
        "--no-nudge",
        recipient,
        "directive",
        f"interop {sender} to {recipient}",
        cwd=project,
        sender=sender,
        registry=registry,
    )
    assert sent.returncode == 0, sent.stderr
    mailbox = resolve_project_mailbox(project, registry_path=registry)
    new_dir = mailbox.inbox_dir / recipient / "new"
    paths = sorted(path for path in new_dir.glob("*.md") if not path.name.startswith("ack-"))
    assert len(paths) == 1
    message_id = paths[0].stem

    listed = _cli("rt-inbox", "-f", "json", cwd=project, sender=recipient, registry=registry)
    assert listed.returncode == 0, listed.stderr
    records = json.loads(listed.stdout)
    assert any(record.get("msg_id") == message_id for record in records)

    acked = _cli(
        "rt-ack",
        message_id,
        "interop handled",
        cwd=project,
        sender=recipient,
        registry=registry,
    )
    assert acked.returncode == 0, acked.stderr
    assert (mailbox.inbox_dir / recipient / "cur" / paths[0].name).is_file()
    receipts = [
        path
        for path in (mailbox.inbox_dir / sender / "new").glob("ack-*.md")
        if f"refs={message_id}" in path.read_text(encoding="utf-8")
    ]
    assert len(receipts) == 1
    return message_id


def test_three_seat_maildir_interop_has_all_routes_and_no_watcher_side_effects(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    registry = tmp_path / "projects.yaml"
    _write_project(project, registry)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("RT_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RT_CODEX_RUNTIME_DIR", str(runtime))
    before = _snapshot(runtime)

    for sender in SEATS:
        for recipient in SEATS:
            if sender != recipient:
                _send_and_ack(project, registry, sender, recipient)

    for seat in SEATS:
        mailbox = resolve_project_mailbox(project, registry_path=registry)
        new_dir = mailbox.inbox_dir / seat / "new"
        assert not [path for path in new_dir.iterdir() if not path.name.startswith("ack-")]
        listed = _cli("rt-inbox", "-f", "json", cwd=project, sender=seat, registry=registry)
        assert listed.returncode == 0, listed.stderr
        assert json.loads(listed.stdout) == []
    assert _snapshot(runtime) == before


def test_cross_worktree_address_targets_only_openclaw_seat(tmp_path):
    registry = tmp_path / "projects.yaml"
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "README.md").write_text("interop\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=origin, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Roundtable Tests",
            "-c",
            "user.email=roundtable@example.invalid",
            "commit",
            "-qm",
            "interop",
        ],
        cwd=origin,
        check=True,
    )
    target = tmp_path / "openclaw-target"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "interop-openclaw", str(target)],
        cwd=origin,
        check=True,
    )
    _write_project(origin, registry)
    _write_project(target, registry)

    sent = _cli(
        "rt-say",
        "--no-nudge",
        f"openclaw@{target.name}",
        "directive",
        "cross-worktree OpenClaw delivery",
        cwd=origin,
        sender="claude",
        registry=registry,
    )
    assert sent.returncode == 0, sent.stderr
    target_mailbox = resolve_project_mailbox(target, registry_path=registry)
    paths = sorted((target_mailbox.inbox_dir / "openclaw" / "new").glob("*.md"))
    assert len(paths) == 1
    message_id = paths[0].stem
    origin_mailbox = resolve_project_mailbox(origin, registry_path=registry)
    origin_openclaw_new = origin_mailbox.inbox_dir / "openclaw" / "new"
    assert not origin_openclaw_new.exists() or not list(origin_openclaw_new.iterdir())

    acked = _cli(
        "rt-ack",
        message_id,
        "cross-worktree handled",
        cwd=target,
        sender="openclaw",
        registry=registry,
    )
    assert acked.returncode == 0, acked.stderr
    assert (target_mailbox.inbox_dir / "openclaw" / "cur" / paths[0].name).is_file()
    receipts = [
        path
        for path in (origin_mailbox.inbox_dir / "claude" / "new").glob("ack-*.md")
        if f"refs={message_id}" in path.read_text(encoding="utf-8")
    ]
    assert len(receipts) == 1
