# ADR 0001 — pneu is a thin local control plane

- Status: accepted, 2026-08-27
- Supersedes: the implicit scope accumulated through the 1.x cycles

## Context

pneu sits between two attractors: a `send/inbox/ack` utility too small to
coordinate real multi-agent work, and a full Agent Development Environment
that owns conversations, transcripts, orchestration, and approval UI.
Comparable systems (ORG2, Orca, Paseo) chose the ADE shape and inherit its
obligations: transcript privacy and retention, schema drift, session
hosting, and the requirement that users relocate their work.

## Decision

pneu is a **thin, local-first coordination control plane for native
coding-agent sessions**. It owns local identity, seat addressing, durable
mail, occupancy/leases/fencing, native-session binding, wake/resume policy,
surface navigation, and reversible host integration. It does not own model
calls, the agent loop, transcripts, tool execution or approval, rendering
surfaces, task decomposition, an issue tracker, or a cloud service.

Boundary test: if a feature requires pneu to reconstruct or render the agent
conversation in order to work, it belongs to the harness or an adjacent
product.

## Consequences

- Delivery works with nothing running; coordination survives daemon death.
- The switchboard stays a navigation surface, never a workspace.
- Deep per-vendor engineering happens inside adapters, not in core scope.
- Some capabilities (multi-surface attach, work-while-unattended, uniform
  cross-provider UI) are structurally ceded to ADEs; that trade is the
  product moat, accepted knowingly.
