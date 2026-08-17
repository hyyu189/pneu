"""One home for the ``.roundtable`` project fixture.

Thirteen test modules each wrote their own ``write_project``, and with it
thirteen hand-spelled copies of the ``agents.yaml`` shape. That is the same
one-fact-many-copies problem the architecture review names in production code:
nothing fails when one copy drifts, because each copy is only ever read by its
own module.

This module owns the document. Modules keep their own ``write_project``
wrapper for what is genuinely local to them — registering the project, running
``git init``, writing ``runtime.json`` — and delegate the document here.

The rendered text is deliberately byte-compatible with the definitions it
replaced; the existing suite is the oracle for that.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


SCHEMA = "roundtable.agents.v1"

#: ``project: "."`` — the quoted relative form.
PROJECT_DOT = '"."'

#: ``project: .`` — the bare relative form. Both are valid YAML for the same
#: value; fixtures differ only because they were written independently.
PROJECT_DOT_BARE = "."


@dataclass(frozen=True)
class Seat:
    """One ``agents.yaml`` seat entry.

    ``instances`` defaults to a single instance named after the agent. Pass an
    explicit empty tuple to omit the ``instances:`` key entirely, which is a
    shape the seat-lifecycle tests rely on.
    """

    agent_id: str
    harness: str
    instances: Sequence[str] | None = None
    submit: Mapping[str, str] | None = None
    detect_screen: Sequence[str] | None = None
    instance_session_id_null: bool = False

    def instance_ids(self) -> tuple[str, ...]:
        if self.instances is None:
            return (self.agent_id,)
        return tuple(self.instances)


CODEX = Seat("codex", "codex")
CLAUDE = Seat("claude", "claude-code")
HERMES = Seat("hermes", "hermes-agent")
GROK = Seat("grok", "grok-build")
OPENCLAW = Seat("openclaw", "openclaw")


def _seat_lines(seat: Seat) -> list[str]:
    lines = [f"  {seat.agent_id}:", f"    harness: {seat.harness}"]
    if seat.submit is not None:
        lines.append("    submit:")
        lines.extend(f"      {key}: {value}" for key, value in seat.submit.items())
    instance_ids = seat.instance_ids()
    if instance_ids:
        lines.append("    instances:")
        for instance_id in instance_ids:
            lines.append(f"      - id: {instance_id}")
            if seat.instance_session_id_null:
                lines.append("        session_id: null")
    if seat.detect_screen is not None:
        lines.append("    detect:")
        rendered = ", ".join(f'"{name}"' for name in seat.detect_screen)
        lines.append(f"      screen: [{rendered}]")
    return lines


def agents_document(
    seats: Sequence[Seat],
    *,
    project: str,
    workspace_title: str | None = None,
) -> str:
    """Render an ``agents.yaml`` document.

    ``project`` is emitted verbatim after ``project:``, so a caller chooses
    between an absolute path, an unresolved path, and :data:`PROJECT_DOT`.
    """

    if not seats:
        raise ValueError("an agents.yaml document needs at least one seat")
    lines = [f"schema: {SCHEMA}", f"project: {project}"]
    if workspace_title is not None:
        lines.append(f"workspace_title: {workspace_title}")
    lines.append("agents:")
    for seat in seats:
        lines.extend(_seat_lines(seat))
    return "\n".join(lines) + "\n"


def write_project(
    path: Path,
    seats: Sequence[Seat] = (CODEX,),
    *,
    project: str | None = None,
    workspace_title: str | None = None,
) -> Path:
    """Create ``<path>/.roundtable/agents.yaml`` and return the resolved root.

    Only the state directory and the document are written. Registration, git
    initialization, and any extra state files stay with the caller, because
    those choices are what actually differ between test modules.
    """

    root = Path(path)
    state = root / ".roundtable"
    state.mkdir(parents=True)
    document = agents_document(
        seats,
        project=str(root.resolve()) if project is None else project,
        workspace_title=workspace_title,
    )
    (state / "agents.yaml").write_text(document, encoding="utf-8")
    return root.resolve()
