# pneu — roadmap baseline

Part of the 2026-08-27 rebaseline. Current phase: **consolidation before 1.5** — make the
ground solid rather than add surface; nothing new promotes until its validation gates pass.
(The 1.4 cycle shipped through v1.3.5; the next milestone is named 1.5.)
Sources: BRIEF.md, BACKLOG.md, docs/ux/launcher.md, handoff/architecture-review-1.4.md.

## Committed — next code window

- Launcher seat-occupancy rows (launcher.md §5.4): occupancy column with owner locus,
  degrading cleanly to bare `active`.
- Guarded takeover panel (§5.5): Enter on an active seat opens jump / guarded take-over /
  free cancel instead of a post-exit `SeatOccupied` dead end.
- Holder-naming refusals (§5.6): refusals everywhere name the holder and the next action
  via one shared locus resolver.
- Fix the `w`-key defect (§1.9): registry warnings currently displace the worktree list.

The trio (§5.4–5.6) is the ruled remedy for the 2026-08-18 seat-capture and bare-restore
incidents.

## Committed architecture (plan of record, hard dependency chain)

- **Tier 1 — one home per fact**: extract `bin/_rtsafeio.py`, `_rtharness.py`,
  `_rtmail.py` (none exists yet).
- **Tier 2**: retire the cmux path into `_rtlegacy.py`; `__all__` + dead-code deletions;
  the `rt-stop-gate` decision.
- **Tier 3** follows. Where the two 1.4 reviews differ, the Codex cross-check wins.

## Accepted, unscheduled

Roster census at init; worktree roster inheritance; `pneu seat add/rm` (+ optional
`--group`); rc-host default-off behind an expert setting; herdr tab-targeting for
`worktree open`; visible message on declined bound-thread resume; UX-SPEC rollout beyond
the launcher (doctor, worktree, guide, error catalog, agent JSON/exit codes); Paseo E1
live check.

## Open rulings awaited

Surface-neutral seat ruling restated for the append-only ledger; retain-or-stop-shipping
for `rt-say --legacy-nudge-only` + the cmux keyboard path; `rt-stop-gate` (ships but
belongs to a superseded hook generation); the optional cmux adapter (documented supported,
no validation path); Paseo borrowables B1–B5; clean-machine validation + demo recording
(open release gate); fresh launch-materials brief (current materials describe 1.1.0);
whether GROK.md belongs in the repo.

## Watch items (evidence-gated)

Unexplained ~70-minute idle watcher death; SessionStart adoption admitting headless
sessions (mitigated; reopen on non-rc-host capture); herdr bare-restore template (ruled
not-a-fix); `dashboard.lock` recreated by an external scanner.

## Explicitly not scheduled

All harness expansion (13-candidate survey and blueprints exist; every candidate is gated
by principle 1); OpenClaw seat work (lab path only); Grok A-class wake (standing negative
result: the leader socket is not a wake channel). Upstream Codex zero-turn-resume issue is
drafted but unfiled.

## Not yet claimed as support

Clean-account repeats; credentialed Codex path; Grok resume re-arm; terminal matrix;
phone-host live spawn (rc-host stays RC until the live phone-side smoke passes).
