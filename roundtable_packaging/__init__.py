"""Release installation support for Roundtable Messaging."""

from __future__ import annotations


VERSION = "0.1.9"
MANIFEST_SCHEMA = "roundtable.install.v1"
MANAGED_MARKER = ".roundtable-managed.json"

MANAGED_HELPERS = (
    "_rtcodex.py",
    "_rtlauncher.py",
    "_rtlib.py",
    "_rtruntime.py",
)

MANAGED_ASSETS = (
    "share/roundtable/integrations/hermes/roundtable/__init__.py",
    "share/roundtable/integrations/hermes/roundtable/plugin.yaml",
    "share/roundtable/skills/shared/roundtable/SKILL.md",
)

TOOLS = (
    "roundtable",
    "roundtable-init",
    "roundtable-setup",
    "roundtable-smoke",
    "roundtable-uninstall",
    "rt-ack",
    "rt-claude",
    "rt-codex",
    "rt-codex-daemon",
    "rt-codex-session-start",
    "rt-codex-wake",
    "rt-doctor",
    "rt-hermes",
    "rt-inbox",
    "rt-projects",
    "rt-refresh",
    "rt-resolve",
    "rt-say",
    "rt-startup-advisory",
    "rt-stop-gate",
    "rt-wait-inbox",
)

LAUNCH_AGENT_LABELS = (
    "com.roundtable.codex-wake",
    "com.roundtable.codex-app-server",
)
