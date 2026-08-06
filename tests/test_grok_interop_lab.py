"""Three-seat durable-mail interop lab for Grok, Claude, and Codex."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

from _rtlib import register_project, resolve_project_mailbox  # noqa: E402


SEATS = ("grok", "claude", "codex")


def _write_project(project: Path, registry: Path) -> None:
    state = project / ".roundtable"
    (state / "messages").mkdir(parents=True)
    (state / "locks").mkdir()
    (state / "agents.yaml").write_text(
        "schema: roundtable.agents.v1\n"
        f"project: {project}\n"
        "agents:\n"
        "  grok:\n"
        "    harness: grok-build\n"
        "    instances:\n"
        "      - id: grok\n"
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


def _cli(name: str, *args: str, cwd: Path, sender: str, registry: Path):
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


def _send_and_ack(project: Path, registry: Path, sender: str, recipient: str) -> None:
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
    assert any(record.get("msg_id") == message_id for record in json.loads(listed.stdout))
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


def test_three_seat_maildir_interop_covers_every_grok_route(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    registry = tmp_path / "projects.yaml"
    _write_project(project, registry)

    for sender in SEATS:
        for recipient in SEATS:
            if sender != recipient:
                _send_and_ack(project, registry, sender, recipient)

    for seat in SEATS:
        listed = _cli("rt-inbox", "-f", "json", cwd=project, sender=seat, registry=registry)
        assert listed.returncode == 0, listed.stderr
        assert json.loads(listed.stdout) == []
