# D10 — upgrade hardening: registry liveness, migration residue, resume shape

Owner decisions (Ocean, 2026-08-10). All four parts were found live during
the Codex 0.146.0 → 0.147.0 host upgrade and the quant→quant-lab project
migration. Root-cause evidence is in this file; reproduce each before fixing.

## D10.1 — the wake bridge must see registry changes made after it started

Live failure: three quant-lab worktrees registered at 01:25–01:28Z; the
bridge (started 01:07Z) rejected their binds with "bind request project is
not registered" and never watched their mailboxes. The bridge reads the
project registry once at startup and never again.

- Re-read the registry when validating a bind request (a bind for an
  unknown project must consult the current registry before rejecting).
- Refresh the watch list on a periodic cadence you justify (heartbeat tick
  is a natural place) so newly registered projects' mail wakes seats
  without a bridge restart, and tombstoned projects stop being watched.
- Fail-closed behavior for malformed registries is unchanged.

## D10.2 — migrated-project runtime residue (third residue shape)

Live failure: `~/quant` was moved to `~/quant-lab/quant` with a compat
symlink left behind and the old registry entry tombstoned. The old runtime
directory (hash of the old path) then failed metadata validation — its
recorded path now resolves elsewhere — and the launch preflight refused the
coordinated app-server reload with a raw
"runtime project metadata mismatch at <path>" and no remedy.

- `rt-doctor`: extend the runtime-residue advisory to cover this shape —
  a runtime directory whose registry entry is tombstoned or whose recorded
  projectRoot no longer canonicalizes to the recorded hash. Report-only,
  name the exact directory and the safe cleanup.
- The preflight ambiguity refusal must name the remedy in plain language
  (which directory, why it is stale, what command/action clears it), per
  the D9 standing rule: every refusal names its remedy.
- Reclaim stays manual/fail-closed; no auto-deletion.

## D10.3 — primer-skip must be loud

Live failure: two of three quant-lab seats were launched with extra argv
(model/plan flags), which skips the launcher-primed first turn by design;
with zero interactions their SessionStart hook never fired and the seats
sat unarmed and unbound indefinitely, with nothing telling the operator.

- When the primer is skipped (non-bare argv or RT_CODEX_NO_PRIMER), the
  launcher prints a prominent advisory: this seat will not arm or bind
  until its first turn; interact with it once (or resume it) to arm.
- Document the rule in the skill doc's launch section in one sentence.

## D10.4 — resume commands must match the installed CLI

Live failure: Codex 0.147.0 removed the `--resume` flag (`error: unexpected
argument '--resume'`); the subcommand form `codex resume <id>` works. Our
`rt-codex-wake handoff` output and docs print the flag form, which now
fails after the very upgrade handoff exists to serve.

- Determine from Codex source/changelog whether the `resume` subcommand is
  supported at our floor (0.144.6). If yes, switch handoff output, docs,
  and any launcher handling to the subcommand form unconditionally; if
  not, branch on the installed version.
- Evaluate whether MINIMUM_CODEX_RELEASE should rise now that 0.147 is the
  validated live release; recommend, do not decide — record the trade-off
  in the handoff for Ocean.
- Add a launch-path regression test that fails if the constructed resume
  invocation uses an argument the pinned floor↔current CLI surface does
  not accept (condition-level, not string-cosmetic).

## Constraints and release

- Full suite + compileall + public-safety green; mutation checks on new
  conditions; no `Claude-Session:` trailers.
- Version 1.2.1 (fixes). Branch `wt/rt-d10` in `~/Code/rt-d10`. Commit your
  work, then report root causes + fixes with a handoff pointer via
  `rt-say claude@roundtable-product`. I run acceptance, merge, hot-swap,
  release.
