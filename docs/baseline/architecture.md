# pneu — target architecture

Part of the 2026-08-27 rebaseline. The intended end state established by the two 1.4
architecture reviews and their crosscheck (crosscheck ordering wins where they differ),
plus the already-merged 1.4 direction. Tier 0 and the small defect fixes landed in the 1.4
tracks; Tiers 1–3 are open backlog (see roadmap.md).

## Principles

- **One home per fact.** Every load-bearing fact (harness registry, wake predicate,
  ancestor walk, atomic-write contracts) gets exactly one module; consumers import, never
  re-derive.
- **Strength visible at the call site.** Two named atomic-write contracts (bootstrap
  publisher vs owned-rewrite) — never a global merge.
- **Code motion, not redesign.** Extractions are literal moves with fault injection, and
  never share a cycle with wake-predicate, layout-locking, or lease changes.
- **Do not abstract ahead of the second consumer.** Generic supervised-service machinery
  stays under its Codex nouns until a second harness proves the boundary.

## Planned modules (none exist yet)

- `bin/_rtharness.py` — the single stdlib-only harness registry (both reviews converged on
  this independently). Consumers: launcher, `bin/pneu`, `rt-worktree`, `_rtrchost.py`,
  `rt-doctor`, `setup.py`.
- `_rtlib.project_at_or_above` — the one project-ancestor primitive (today ×5 copies).
- `bin/_rtmail.py` — wake-predicate vocabulary with an explicit strictness enum,
  preserving the three real strictness variants.
- `bin/_rtmaildir.py` — the maildir mutation kernel (`publish_new`/`archive_new`,
  lock-free, callers hold the layout lock), extracted as literal code motion with fault
  injection.
- `bin/_rtsafeio.py` — the two named atomic-write contracts.
- `_rtlegacy_nudge.py` — the retired cmux keyboard path extracted out of `rt-say`.

## Sequenced tiers

- **Tier 1**: `_rtsafeio`, `_rtharness`, `_rtmail`, the single ancestor primitive.
- **Tier 2**: legacy-cmux extraction; `__all__` + dead-code deletions; the `rt-stop-gate`
  retain-or-remove decision.
- **Tier 3**: launcher `HarnessDescriptor` refactor (depends on `_rtharness`); setup
  planners layered over the unchanged transaction engine.
- **Deferred until a second supervised-service harness exists**: generic extraction of the
  Codex service classifier / readiness / launch-intent machinery; the `rt-doctor` probe
  registry.

## Untouchable

`_rtmigrate.py` (frozen), `_rtsurface.py`, `roundtable-init`, the setup transaction core,
the migration/mailbox crash matrices, the lease mechanism, and the rt-ack→rt-say process
boundary (it IS the lock discipline).

## Already-merged direction (verified in tree)

Capability binding as the identity transport (environment channel is dead — see
adapters.md); one canonical Codex app-server host with Desktop joining via the login-agent
switch; watcher lifecycle log + two-layer self-heal + doctor truth; OpenClaw off the seat
surface; journey/xdist test tiers with mutation evidence; derived-consumer fitness tests.
