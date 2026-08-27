# ADR 0004 — project integration is modular

- Status: accepted, 2026-08-27

## Context

The 1.x scaffold (`roundtable-init`) manages or appends to AGENTS, CLAUDE,
HERMES, GROK, ROUTING, README, BRIEF, decision, and root `.gitignore` files
and creates handoff/log/skills directories. The mutations are marker-owned
and reversible in the narrow file sense, but product-intrusive: a user who
wants durable local agent communication is conscripted into pneu's
documentation, status, routing, and handoff system. Static project documents
also cannot establish session identity, lease validity, or wake readiness —
those are runtime facts.

## Decision

Default project adoption creates only the **smallest pneu-owned anchor**
needed for identity and roster. Two optional, explicit, reversible modules
sit above it:

- an **awareness module** — a marker-owned instruction block teaching an
  agent how to query runtime status and load the global pneu skill; no fixed
  roles; mutation plan shown first; never required for delivery, binding,
  or wake;
- a **collaboration module** — change-contract, handoff, and ADR
  conventions plus local change records; never a dependency of messaging.

Agent discovery priority: runtime activation from the real session adapter →
structured native tools/CLI → the global pneu skill → the optional awareness
block. The `load_if` routing convention is retired. The hidden path stays
`.roundtable/` during 1.x; renaming it is explicitly not a priority.

## Consequences

- Minimal mode stops mutating README/instruction files and root
  `.gitignore` entirely.
- The legacy scaffold is migrated, not abruptly deleted (roadmap Phase 3);
  templates keep shipping until then.
- The global skill shrinks to stable communication behavior.
- Host setup keeps plan/apply/status/remove with tracked ownership.
