# Grok TUI-first implementation handoff

> Status: historical record — the TUI-first Grok seat implementation report

Date: 2026-08-12

Branch: `wt/rt-grok-tui`

Dispatch: `handoff/grok-tui-first.md`

## Outcome

The Grok seat is now TUI-first. `rt-grok` resolves and execs the native
interactive Grok executable in the project root after the normal fenced lease
claim. A bare launch supplies one pinned positional activation prompt that
creates exactly one persistent, session-scoped mailbox monitor using the
authoritative absolute `new/` path and package-managed absolute fenced
`rt-inbox`/`rt-ack` commands.

Explicit native arguments and `RT_GROK_NO_PRIMER=1` pass through without the
primer and print a prominent re-arm advisory. Resume is documented as requiring
one re-arm turn. The native TUI keeps the user's normal HOME, Grok, XDG,
temporary, and log state; the launcher credential preflight checks only for an
existing source and never reads, parses, refreshes, copies, or logs it.

The former `rt-grok-wake` ACP supervisor remains packaged and directly
invocable as an explicitly labeled internal lab tool. It is no longer selected
by the user-facing Grok seat path.

## Product surface

- Added neutral `GROK.md` project orientation plus idempotent
  `roundtable-init` wiring and wheel packaging.
- Updated the launcher menu, README, architecture, compatibility, release
  checklist, and pneu skill to describe the native TUI monitor contract and
  the resume re-arm rule.
- Added a bounded, read-only `rt-doctor` advisory for active Grok seats. It
  reports monitor evidence as present, absent, or unreadable and never treats
  session records as lease or routing evidence.
- Kept Grok's own approval UX authoritative; the seat path does not answer
  permissions on Grok's behalf.

## Regression coverage

- Exact native TUI argv and pinned primer text.
- Exact fenced seat environment and absence of isolated-HOME variables.
- Presence-only credential gate, explicit-argument pass-through, and
  `RT_GROK_NO_PRIMER=1` behavior.
- Mutation pin proving `rt-grok` cannot select `rt-grok-wake`, while the lab
  entry remains importable and self-identifies as internal.
- Doctor fixtures for monitor present, absent, and unreadable, including the
  native owner/no-host-heartbeat presentation.
- GROK template initialization, preservation/idempotence, shipped payload, and
  wheel inclusion.

## Verification

- Expanded affected suite: `227 passed in 49.07s`.
- Mutation-focused rerun after resolving a pre-existing needle collision:
  `53 passed in 28.92s`.
- Final full suite: `1081 passed, 1 skipped in 1125.77s`.
- `mamba run -n general python -m compileall -q bin pneu_packaging scripts tests integrations`: passed.
- `mamba run -n general python scripts/check_public_safety.py`: passed
  (`181 tracked files, full reachable history`).
- `git diff --check`: passed.
- Forbidden session-trailer scan: clear.

The first full-suite run exposed only a mutation-harness needle collision:
the new Grok presence check used the same source line as the Hermes gate. The
Grok check was rewritten as an equivalent explicit loop, the focused mutation
contract passed, and the full suite was rerun from scratch to the green result
above.

## Deliberate acceptance boundary

No Grok process was launched, and no live `~/.grok` state or credential was
read or modified. Native TUI wake-to-drain/ack, resume re-arm, clean-account,
and terminal-matrix acceptance remain Ocean's live promotion gate. Until those
pass, the doctor evidence stays advisory and the documentation makes no public
support claim.
