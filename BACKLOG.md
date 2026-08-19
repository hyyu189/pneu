# BACKLOG — at-a-glance work ledger

> One line per item; detail lives in the linked spec or brief. Status:
> `now` (next code window) · `accepted` (design ratified, unscheduled) ·
> `watch` (needs evidence) · `ruling` (needs the product lead).
> Update an item's status in the same commit as the work that changes it.

## Now — next code window

- `now` Seat occupancy UX: card rows show vacant/active/stale with owner
  locus; Enter on an active seat offers jump / guarded takeover / cancel;
  refusals name the holder (spec: docs/ux/launcher.md §5.4–5.6). Ruled
  2026-08-19: lease recognition and honest reporting are the remedy for the
  adoption/restore incidents — no separate bug-fixes.
## Accepted design — unscheduled

- `accepted` rc-host defaults off and moves behind an expert setting; no card
  prominence (ruled 2026-08-19: slow in-app response, low value today).
- `accepted` roundtable-init censuses installed harnesses at project birth,
  replacing the fixed claude/codex/hermes template (docs/ux/launcher.md §5.1).
- `accepted` `pneu worktree add` inherits the parent checkout's agents.yaml
  instead of re-templating (§5.2).
- `accepted` `pneu seat add/rm` explicit commands; optional `--group` batch
  (§5.3).
- `accepted` 1.5 refactor ladder: cheap correctness → test ownership → RC1
  prototype decision → one-fact-per-module → transport isolation →
  descriptors/planners → deferred (handoff/architecture-review-1.4-crosscheck-codex.md).
- `accepted` UX-SPEC rollout beyond the launcher: doctor, worktree, guide,
  error catalog, agent-facing JSON and exit codes.
- `accepted` herdr surface: tab-targeting parameter for `pneu worktree open`
  (today it spawns into the caller's tab).
- `accepted` Declined bound-thread resume shows a visible message instead of
  a silent fallback.
- `accepted` Mailbox mutation kernel consolidation; project-discovery dedup
  (1.5 ladder feeders).

## Bugs & watch items

- `watch` Case #1: 70-minute idle watcher death, unexplained; lifecycle log
  armed, awaiting recurrence (case file: d16-1-control-experiment, handoff
  archive).
- `watch` SessionStart adoption admits headless sessions (2026-08-19
  identity-capture incident: the rc-host registration session held the human
  seat and processed mail). Ruled: no direct fix — mitigated by rc-host
  default-off plus occupancy UX. Reopen if a non-rc-host headless capture
  occurs.
- `watch` herdr reattach restore fills a bare `claude --resume` command
  without launcher env. Ruled: not fixed — the command is only filled, never
  run; occupancy UX covers the resulting confusion.
- `watch` `downloaded_files/dashboard.lock` junk is recreated by an external
  scanner; delete before builds.

## Research follow-ups

- `accepted` Paseo borrowables B1–B5 adoption decisions
  (handoff/paseo-research.md §6 on wt/paseo-research; audited PASS
  2026-08-19).
- `accepted` E1: live check that a Paseo daemon restart drops pending finish
  obligations, before that claim is cited externally.
- `accepted` Qoder and Pi adapter labs per the T5 blueprints
  (handoff/harness-expansion-blueprints.md).

## Needs a ruling

- `ruling` BRIEF.md is frozen at the 1.0.0 launch plan (it still names the
  Grok ACP supervisor as the productization template): rewrite thin or
  retire.
- `ruling` `rt-say --legacy-nudge-only` (emergency cmux keyboard path, no
  validation path since cmux retired): retire?
- `ruling` `rt-stop-gate` binary (unused by the current Claude hook
  generation): stop shipping, keeping only legacy-upgrade recognition?
- `ruling` cmux adapter (`rt-resolve`, `rt-refresh`, agents.yaml cmux
  fields): retire?
- `ruling` GROK.md at the repository root: carry deliberately, or keep it a
  per-tree onboarding file only?
- `ruling` RC1 prototype decision (1.5 ladder layer 3).
