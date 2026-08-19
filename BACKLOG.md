# BACKLOG

> Status: current. The at-a-glance open-work index — each item points at the
> file that holds its real detail; nothing here restates a design. Status
> marks: `now` (next code window) · `accepted` (ratified, unscheduled) ·
> `watch` (needs evidence) · `ruling` (needs the product lead). Shipped work
> is not listed: `decision.md` holds the rulings and `handoff/archive/` the
> records. Update an item's status in the same commit as the work that
> changes it.

## Now — next code window

| Item | Status | Source |
| --- | --- | --- |
| Seat occupancy on the launcher card (vacant / active / stale + owner locus) | `now` — ruled 2026-08-18, specified | `docs/ux/launcher.md` §5.4 |
| Guarded takeover instead of a dead-end refusal on an active seat | `now` — ruled 2026-08-18, specified | `docs/ux/launcher.md` §5.5 |
| Refusals that name the holder and the next action | `now` — ruled 2026-08-18, specified | `docs/ux/launcher.md` §5.6 |
| `w` shows registry warnings instead of the worktree list | `now` — defect: `_run_card_command` resolves `(stderr or stdout)` | `docs/ux/launcher.md` §1.9 |

The occupancy trio was ruled as the remedy for the 2026-08-18 seat-capture
and bare-restore incidents: lease recognition and honest reporting, not
adoption or restore-path bug-fixes.

## Decisions waiting on the product lead

| Item | Why it is open | Source |
| --- | --- | --- |
| Seat ruling restated surface-neutrally | Drafted for the ledger, not appended — `decision.md` is append-only and Ocean's | `handoff/docs-consolidation-report.md` §5 |
| `rt-say --legacy-nudge-only` and the cmux keyboard path | Ships today; the architecture review calls it retired. Retain or stop shipping? | `handoff/architecture-review-1.4.md` §6 Tier 2 |
| `rt-stop-gate` | A superseded hook generation still shipped as a tool; unused by the current hook generation | `handoff/architecture-review-1.4.md` §6 Tier 2 |
| The optional cmux adapter (`rt-resolve`, `rt-refresh`, agents.yaml cmux fields) | Documented as supported, with no current validation path | `docs/compatibility.md` terminal matrix |
| Paseo borrowables B1–B5 adoption | Research audited PASS 2026-08-18; each borrowable maps to a named pneu seam | `research/paseo-research.md` §6 (local, untracked by design) |
| Clean-machine validation + demo recording | Still an open release gate; scheduling is Ocean's | `docs/release.md` promotion gates |
| Launch materials | The only artifact describes pneu 1.1.0 and is archived; a resumed workstream needs a fresh brief | `handoff/archive/launch-materials-brief.md` |
| GROK.md at the repository root | Today a per-project generated file (untracked local state); carrying it in the repo is a deliberate decision, not a cleanup default | `templates/GROK.md.tmpl` |

The shipped-surface rows share one question, and `PRINCIPLES.md` principle 4
is why they are listed: a shipped surface has no zero-cost parking state, and
"we do not use it" raises the bar rather than lowering it.

## Accepted design — unscheduled

| Item | Detail | Source |
| --- | --- | --- |
| Roster census at project birth | `accepted` — init writes a fixed three-harness template today | `PRINCIPLES.md` §3, `docs/ux/launcher.md` §5.1 |
| Worktree roster inheritance at tree birth | `accepted` — today an ordinary Git checkout side effect | `PRINCIPLES.md` §3, `docs/ux/launcher.md` §5.2 |
| `pneu seat add` / `pneu seat rm`, optional `--group` batch | `accepted` — the card's `a` key is the only roster write that ships | `docs/ux/launcher.md` §5.3 |
| rc-host defaults off, behind an expert setting | `accepted` — ruled 2026-08-18: slow in-app response, low value today; no card prominence | — |
| herdr surface: tab-targeting for `pneu worktree open` | `accepted` — today it spawns into the caller's tab | — |
| Declined bound-thread resume shows a visible message | `accepted` — replaces the silent fallback | — |
| UX-SPEC rollout beyond the launcher | `accepted` — doctor, worktree, guide, error catalog, agent-facing JSON and exit codes | `docs/ux/` |
| E1: live check that a Paseo daemon restart drops pending finish obligations | `accepted` — required before that claim is cited externally | `research/paseo-research.md` §8 (local, untracked by design) |

## Architecture

The 1.4 whole-project review is the plan of record. Tier 0 and the
RC1/D2/D3/D4 defects landed in the 1.4 cycle; **Tiers 1, 2, and 3 are open**,
and their ordering is a hard dependency chain, not a preference.

- Tier 1 — one home per fact: `bin/_rtsafeio.py`, `bin/_rtharness.py`,
  `bin/_rtmail.py`. None exists yet.
- Tier 2 — the retired cmux path into `_rtlegacy.py`, `__all__` on `_rtlib`
  and the verified dead-code deletions, the `rt-stop-gate` decision above.
- Tier 3 — everything that depends on Tier 1.

Read `handoff/architecture-review-1.4.md` §6 with
`handoff/architecture-review-1.4-crosscheck-codex.md` beside it; the
cross-check refines the defect audit and wins where the two differ.

## Bugs & watch items

| Item | Status | Source |
| --- | --- | --- |
| Case #1: 70-minute idle watcher death, unexplained | `watch` — lifecycle log armed, awaiting recurrence | `handoff/archive/d16-1-control-experiment.md` |
| SessionStart adoption admits headless sessions | `watch` — 2026-08-18 identity capture: the rc-host registration session held the human seat and processed mail. Ruled: no direct fix; mitigated by rc-host default-off plus occupancy UX. Reopen on a non-rc-host capture | — |
| herdr reattach restore fills a bare `claude --resume` without launcher env | `watch` — ruled not-a-fix: the command is only filled, never run; occupancy UX covers the confusion | — |
| `downloaded_files/dashboard.lock` recreated by an external scanner | `watch` — delete before builds | — |

## Harness expansion

Nothing is implemented and nothing is scheduled. `handoff/harness-expansion-survey.md`
holds the 13-candidate verdict table and `handoff/harness-expansion-blueprints.md`
holds the per-family designs and their live gates. Every candidate is subject
to `PRINCIPLES.md` principle 1: the adaptation must reach the session the
human is actually in.

OpenClaw is a lab path, not a candidate, until it has a healthy
current-version install to validate against, a trust-boundary design for
attaching to the user's own Gateway, and a live-render probe. See
`handoff/d14-openclaw-source-audit-2026-08-17.md`.

Grok's A-class (code-armed) wake has a standing negative result: the leader
socket is not a wake channel. See
`handoff/grok-leader-socket-wake-2026-08-14.md`.

## Validation

`docs/compatibility.md` is the one home for what has been exercised and what
each harness still needs before promotion. It is not duplicated here. The
short version is that installation is automated and covered, while harness
*support* — clean-account repeats, the credentialed Codex path, Grok resume
re-arm, the terminal matrix, and the phone-host live spawn — is not yet
claimed.

## Good citizenship

The upstream Codex zero-turn-resume issue is drafted and unfiled, awaiting
Ocean's nod: `handoff/upstream-codex-zero-turn-resume.md`.

## Documentation

Two wording questions left open by the 2026-08-18 consolidation, neither a
contradiction:

- "P0" is used throughout `docs/` to mean the first supported scope, and is
  defined nowhere. Renaming it is a decision, not a correction.
- The harness onboarding matrix cites RC5/RC7/RC8 evidence labels that a new
  reader will not recognise. Dating them instead is a small follow-up.
