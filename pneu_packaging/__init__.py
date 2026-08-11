"""Release installation support for pneu."""

from __future__ import annotations


VERSION = "1.3.0"
MANIFEST_SCHEMA = "roundtable.install.v1"
MANAGED_MARKER = ".roundtable-managed.json"

MANAGED_HELPERS = (
    "_rtcodex.py",
    "_rtlauncher.py",
    "_rtlib.py",
    "_rtmigrate.py",
    "_rtrchost.py",
    "_rtruntime.py",
)

MANAGED_ASSETS = (
    "share/pneu/integrations/hermes/pneu/__init__.py",
    "share/pneu/integrations/hermes/pneu/plugin.yaml",
    "share/pneu/integrations/openclaw/roundtable/__init__.py",
    "share/pneu/integrations/grok/roundtable/__init__.py",
    "share/pneu/skills/shared/pneu/SKILL.md",
)

TOOLS = (
    "pneu",
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
    "rt-openclaw",
    "rt-openclaw-wake",
    "rt-grok",
    "rt-grok-wake",
    "rt-projects",
    "rt-rc-host",
    "rt-refresh",
    "rt-resolve",
    "rt-say",
    "rt-startup-advisory",
    "rt-stop-gate",
    "rt-wait-inbox",
    "rt-worktree",
)

LAUNCH_AGENT_LABELS = (
    "com.roundtable.codex-wake",
    "com.roundtable.codex-app-server",
)
