import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SHIPPED_ROOTS = (
    "templates",
    "skills",
    "docs",
    "bin",
    "integrations",
    "pneu_packaging",
    "scripts",
)
ROLE_LINE = (
    'role:  # optional — e.g. "implementation and tests" — assign per project'
)


def test_orientation_templates_leave_roles_unassigned():
    for relative in (
        "templates/CLAUDE.md.tmpl",
        "templates/AGENTS.md.tmpl",
        "templates/HERMES.md.tmpl",
    ):
        lines = (ROOT / relative).read_text().splitlines()
        assert [line.strip() for line in lines if line.strip().startswith("role:")] == [
            ROLE_LINE
        ]

    combined = "\n".join(
        (ROOT / "templates" / name).read_text()
        for name in ("BRIEF.md.tmpl", "decision.md.tmpl")
    )
    assert "role:" not in combined


def test_shipped_payload_contains_no_cjk_text():
    matches = []
    pattern = re.compile(r"[一-鿿]")
    for relative in SHIPPED_ROOTS:
        for path in (ROOT / relative).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            if pattern.search(text):
                matches.append(str(path.relative_to(ROOT)))
    assert matches == []


def test_routing_template_mirrors_pneu_trigger_vocabulary():
    routing = (ROOT / "templates" / "ROUTING.md.tmpl").read_text()
    for trigger in (
        "inbound_pneu_mail_message",
        "message_or_dispatch_to_coding_agent_seat",
        "coordinate_another_coding_agent_seat",
        "check_or_drain_agent_mail_or_inbox",
        "claude_codex_hermes_grok_openclaw_peer_agents",
        "rt_say_rt_inbox_rt_ack_rt_refresh_rt_resolve",
        "handoff_writing_or_delivery",
        "sibling_worktree_seat_coordination",
        "seat_lease_coordination",
        "wake_or_delivery_debugging",
    ):
        assert f"- {trigger}" in routing


def test_pneu_skill_description_and_dispatch_workflow_are_complete():
    skill = (ROOT / "skills" / "shared" / "pneu" / "SKILL.md").read_text()
    frontmatter = yaml.safe_load(skill.split("---", 2)[1])
    description = frontmatter["description"]

    assert description.startswith("Use when")
    assert len(description) <= 950
    for phrase in (
        "[FROM→TO kind id=...]",
        "message, dispatch to, or coordinate another coding-agent seat",
        "check or drain agent mail or an inbox",
        "Claude",
        "Codex",
        "Hermes",
        "Grok",
        "OpenClaw",
        "rt-say",
        "rt-inbox",
        "rt-ack",
        "rt-refresh",
        "rt-resolve",
        "write or deliver a handoff",
        "sibling-worktree seats",
        "seat leases",
        "wake or delivery behavior",
    ):
        assert phrase in description
    assert description.endswith(
        "Do not use merely because a repo contains .roundtable/agents.yaml."
    )

    dispatch = skill.split("## Dispatch workflow", 1)[1].split("\n## ", 1)[0]
    assert len(dispatch.splitlines()) <= 25
    normalized_dispatch = " ".join(dispatch.split())
    steps = (
        "pneu worktree add NAME",
        "pneu worktree open NAME --seat AGENT",
        "handoff/<topic>.md",
        "goal, boundaries, and verification expectations",
        "rt-say --fenced --no-nudge --expect-reply <dur> AGENT@NAME dispatch",
        "reports back with a result file and a mail message",
    )
    positions = [normalized_dispatch.index(step) for step in steps]
    assert positions == sorted(positions)
    assert "surface chain handles the terminal" in normalized_dispatch
    assert "does not prescribe project roles" in normalized_dispatch
