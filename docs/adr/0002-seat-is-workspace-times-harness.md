# ADR 0002 — a seat is workspace × harness family

- Status: accepted, 2026-08-27
- Supersedes: the 2026-08-19 ruling "a seat is the human's interactive
  session" (and its 2026-08-12 TUI-era predecessor), recorded in the
  pre-baseline `decision.md` ledger (Git history)

## Context

The earlier wording equated the seat with the human's session. The
implementation was already more precise: stable mailbox identity, one pneu
session ownership term, an optional native thread id, a lease revision, and
advisory surfaces are distinct facts. Equating seat and session left no
vocabulary for a mailbox that persists while nothing runs, or for a
replacement session reoccupying the same address.

## Decision

> **A seat is a stable, workspace-scoped address for one harness family:
> `(workspace_id, harness_family)`. A native session occupies the seat;
> surfaces display the session.**

For pneu 1.x, one workspace holds at most one top-level seat per harness
family. Several clients driving one bound thread are one occupied seat.
Review/implementation/research are transient task purposes, never seat
identity; the default parallel unit is a change/workspace, and a second
top-level instance of one harness family belongs in another worktree.
Harness-internal subagents are not seats.

## Consequences

- Mail is addressed to the seat and durable across sessions; wake targets
  the currently bound session; focus targets a surface.
- The acceptance test survives unchanged: mail must reach the session the
  human is actually in; headless replacement seats remain a failure.
- No immediate runtime field renames: the conceptual contract lands first;
  names migrate only where correctness improves.
