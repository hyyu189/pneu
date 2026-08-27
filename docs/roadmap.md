# pneu roadmap

> Status: current, rebaselined 2026-08-27. Dependency-ordered outcomes only —
> no track IDs, no live status tables. Active tasks belong in issues or a
> temporary local change record, not here.

## Phase 0 — rebaseline (this change)

Clean local topology; one compact repository baseline; conflicting
current-truth documents and ordinary handoff accumulation removed; the four
foundational ADRs recorded.

## Phase 1 — occupancy UX

Honest seat occupancy on the switchboard:

- vacant / active / stale / bound-resumable states with the holder's locus;
- jump/focus to the surface showing the occupying session;
- resume of the exact bound native session;
- guarded takeover and reclaim instead of dead-end refusals;
- refusals that name the holder and the next action;
- the launcher `w`-key defect fixed (warnings currently displace the
  worktree list).

No protocol redesign. The screen-level specification is
[`ux/launcher.md`](ux/launcher.md) §5.4–5.6.

## Phase 2 — one home per fact

- Fix the packaging boundary first, so module decomposition is cheap
  (helper packages or generated manifests instead of a flat exact list).
- Create the canonical stdlib-only harness registry/descriptor.
- Centralize the wake-predicate vocabulary and safe-I/O primitives where the
  audit supports it, behind contract tests — two named atomic-write
  contracts, never a global merge.
- Preserve the proven maildir, lease, migration, and setup ownership
  contracts untouched.

## Phase 3 — minimal init and optional packs

- Minimal project adoption becomes the default: the smallest pneu-owned
  anchor only; no README/AGENTS/BRIEF/ROUTING scaffolding, no broad
  `.gitignore` mutation.
- The awareness block and the collaboration conventions become explicit,
  reversible, marker-owned modules with plan/apply/remove.
- Migration from the legacy scaffold, not abrupt deletion.
- The global pneu skill reduced to stable communication behavior.

## Phase 4 — native adapter spikes

- Map the existing Codex bridge onto current official App Server primitives;
  prove same-thread binding and busy-turn non-interference.
- Implement Herdr as a surface adapter (focus/jump/open-worktree/badges —
  no mailbox drain, no wake ownership).
- Prototype DeepSeek Harness as a native out-of-tree plugin integration.
- Only then derive the minimal common adapter contract (discovery, binding,
  capability negotiation, health, errors, opaque native IDs — not
  thread/turn/tool semantics).

See [`harness-adapters.md`](harness-adapters.md).

## Phase 5 — support and portability

- Capability-based support matrix replacing any flat "supported" label.
- Clean-account and real-session live validation.
- No-Git and local-Git journeys exercised.
- Only then promote additional harnesses.
