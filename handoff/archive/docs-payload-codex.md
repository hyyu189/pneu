# Shipped-payload docs neutralization — Codex completion

> Status: historical record — shipped-payload docs neutralization report

Status: complete on `wt/rt-docs-payload`.

## Delivered

- Replaced the prescribed Claude, Codex, and Hermes role defaults with empty,
  project-assigned `role:` fields and confirmed that the BRIEF/decision
  scaffolds contain no equivalent role prescription.
- Translated all Chinese text in the shipped payload roots to English,
  including the startup advisory and Codex wake prompt.
- Expanded the pneu skill description to cover generic messaging, dispatch,
  inbox, five-harness peer, command, handoff, worktree-seat, lease, and
  wake/delivery triggers while retaining the negative guard. The rendered
  description is 500 characters.
- Mirrored that trigger vocabulary in `templates/ROUTING.md.tmpl`.
- Added the harness-neutral sibling-seat dispatch workflow immediately before
  the receiving section in the pneu skill.
- Added focused regression coverage for neutral roles, the shipped-payload CJK
  sweep, routing trigger coverage, the skill description contract, and the
  ordered dispatch workflow. Updated exact-output assertions for translated
  CLI strings.

## Invariants

- `bin/roundtable-init` is unchanged; its before/after SHA-256 is
  `ace020343eb52dca800b198026c75bd1345ff5c5148c2d2e3c7ff14e58b15649`,
  preserving `MARKER_BLOCKS` byte-for-byte.
- No harness session-attribution trailer was added.
- The source handoff `handoff/docs-payload.md` was left unmodified and excluded
  from the Codex commit.

## Verification

- Focused regressions: `8 passed`.
- Full suite: `1061 passed, 1 skipped` in 1114.67 seconds.
- `python -m compileall -q bin pneu_packaging scripts`: passed in the dedicated
  `general` environment.
- `python scripts/check_public_safety.py`: passed for 178 tracked files and the
  full reachable history.
- `git diff --check`: passed.
- `rg -n -P '[一-鿿]' templates skills docs bin integrations pneu_packaging scripts`:
  zero matches (expected `rg` exit 1).
- `git diff --exit-code -- bin/roundtable-init`: passed.

No blockers remain.
