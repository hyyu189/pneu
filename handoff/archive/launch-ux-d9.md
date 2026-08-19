# D9 — onboarding surface truth + exit UX (four fixes)

> Status: historical record — D9 dispatch; shipped

Owner decisions (Ocean, 2026-08-08). Standing UX rule for every user-facing
string this batch touches: **assume the user never reads the underlying
docs** — each surface must be self-explanatory, and every refusal must name
its remedy in plain language.

## D9.1 — Harness detection and a truthful launcher menu

The interactive `pneu` entry still offers only Claude/Codex/Hermes, and it
does not know which harnesses exist on the host machine.

- Detect installed harnesses at menu time (per-adapter executable/config
  discovery; reuse each adapter's own notion of "installed", do not invent a
  parallel one).
- The menu lists all five supported harnesses — Claude Code, Codex, Hermes,
  OpenClaw, Grok — with harnesses that are not installed clearly marked and
  either non-selectable or failing with a plain-language message that names
  the missing binary and where to get it. No stack traces, no silent absence.
- Selecting an installed OpenClaw/Grok seat must route into their real
  adapters (they are first-class; see D9.4).

## D9.2 — Ctrl-C exit strands the pane

Reproduce: a pneu-launched session (e.g. `rt-claude` / `pneu claude`), exit
with Ctrl-C → the pane sits on an exit status instead of returning to a
shell prompt; a second Ctrl-C kills the pane entirely.

- Diagnose where the shell is lost (launcher exec chain vs TTY teardown vs
  the window-command launch pattern). State the root cause explicitly in the
  report.
- Required outcome: when launched from an interactive shell, exiting the
  harness returns the user to that shell prompt in the same pane. If part of
  the observed behavior is inherent to "tmux window whose command IS the
  launcher" (no parent shell to return to), fix what the launcher owns and
  document the launch-pattern caveat in plain language.
- Do NOT add tmux window/pane management features; Ocean handles pane
  topology manually by design.

## D9.3 — Environment-inheritance advisory in ~/quant (diagnose first)

Launching claude via pneu inside ~/quant triggers the inherited-seat-
environment advisory. Deliver a diagnosis before any change: which exact
variables trip it there, where they come from (tmux server environment
captured at server start? login shell? a previous seat's exports?), whether
the advisory is doing its fail-closed job correctly, and only then whether
its wording/trigger should change. A legitimate guard must not be silenced
for being noisy; a guard that fires on the normal nested-relaunch path
should say so in words a non-reader understands.

## D9.4 — Five harnesses are peers on every user surface

User-facing copy (the `pneu` onboarding screens, `pneu guide`, README top)
still presents Grok Build and OpenClaw as a separate caveated pair
("isolated, fail-closed wake adapters … promotion gates"). That sentence is
release-engineering jargon leaking into onboarding.

- All five harnesses appear as peer supported harnesses on user surfaces.
- Validation-status caveats (dev-host-verified vs public support claims,
  credentialed E2E gates) live in docs/compatibility.md only, phrased for
  maintainers. If a harness's support really is narrower today, the user
  surface may say so in one plain sentence ("verified on the development
  host; broader validation in progress"), not in gate vocabulary.

## Constraints and release

- Full suite + compileall + public-safety green; condition-level tests for
  the detection logic (present/absent/broken-symlink harness binaries).
- No `Claude-Session:` trailers in commits (decision.md 2026-08-07).
- Version 1.2.0 (detection is a feature). Work on branch `wt/rt-d9` in
  `~/Code/rt-d9`; report each part's root cause + fix with a handoff pointer
  via `rt-say claude@roundtable-product`. I run acceptance, merge, hot-swap,
  release.
