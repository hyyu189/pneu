# D14 input: OpenClaw isolation root can enter the project

> Status: historical record — the isolation-root defect; the guard shipped with the OpenClaw demotion

`integrations/openclaw/roundtable/__init__.py:create_isolation` derives its root
from `RT_OPENCLAW_RUNTIME_DIR` or `RT_RUNTIME_DIR` but does not reject a root
beneath the project. In contrast,
`integrations/grok/roundtable/__init__.py:create_isolation` applies `_under(root,
project)` and raises `GrokError` before creating directories.

An operator-supplied OpenClaw runtime root beneath a checkout can therefore put
adapter state in a possibly tracked working tree, including the secret-bearing
`openclaw.json` generated-token configuration. If D14 retains the adapter, its
isolation creation must reject any root under the resolved project before the
root, state, home, temporary, or log directories are created, with regression
coverage matching the Grok guard.

This is deliberately recorded but unfixed: the OpenClaw Gateway adapter is
parked pending D14's retain-or-park decision under the 2026-08-12 TUI-first
ruling in `decision.md`.
