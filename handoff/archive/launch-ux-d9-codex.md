# D9 launcher UX implementation handoff

> Status: historical record — D9 implementation report

Branch: `wt/rt-d9`

## D9.1 — five-harness detection and menu

Root cause: `pneu` only iterated the four names in its local menu order and
used the generic launcher resolver. That omitted Grok Build and meant the
menu did not share the optional adapters' executable discovery rules.

Fix: the menu now enumerates Claude Code, Codex, Hermes, OpenClaw, and Grok
Build. OpenClaw and Grok resolve through their own adapter resolver functions;
the other three retain their existing resolver. Every unavailable entry is
non-selectable and names the missing or non-executable binary, the install
remedy, and the `RT_*_BIN` override when one exists. Present, absent, and
broken-symlink cases have regression coverage.

## D9.2 — Ctrl-C and stranded panes

Root cause: the launcher uses `exec` to replace itself with the harness. From
an existing interactive shell, that preserves the shell as the parent, so the
harness exit (including Ctrl-C) returns to that prompt. A tmux window whose
command is the launcher has no parent interactive shell; after the command
exits, that window can only show its exit status or close. This is a launch
pattern/TTY lifecycle issue, not a Roundtable pane-management feature.

Fix: launcher behavior remains shell-preserving and does not add tmux control.
`docs/compatibility.md` now states the two cases in plain language and gives
the remedy: invoke the launcher from a shell in the window, or make the window
command run a shell that invokes it.

## D9.3 — inherited environment advisory in `~/quant`

Diagnosis was completed before changing the wording. The guard trips only when
the lease-context variables `RT_PROJECT_ROOT`, `RT_SESSION_ID`, and
`RT_LEASE_REVISION` are present; `RT_FROM` is cleared with them because it is
the identity paired with that foreign lease. They arrive through the parent
seat/shell environment, including tmux's captured server environment or a
nested relaunch from an existing seat; they are not discovered from the
project files. In the live check, `~/quant` had no lease-context
variables in its ordinary shell, while the active tmux environment held a
complete Claude lease for `the roundtable-product checkout` and the
process tree showed the quant seat launch wrappers.

The guard is correct and remains fail-closed: keeping those values would let a
nested launch claim or send as the wrong seat. Its message now names the exact
variables and explains that they are cleared to prevent wrong-agent mail.

## D9.4 — peer harness surfaces

Root cause: onboarding prose described OpenClaw and Grok as a separate
promotion-gated pair.

Fix: the `pneu` onboarding guide, harness selector, and README present all five
as peers. Maintainer validation status remains in `docs/compatibility.md`.

## Validation

- Focused launcher/menu and lease tests: 77 passed.
- Full suite: 962 passed, 1 skipped.
- `compileall`: passed.
- Public-safety scan: passed.
- Release version: `1.2.0`.
