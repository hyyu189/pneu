# pneu — accepted decisions (ADR baseline)

Part of the 2026-08-27 rebaseline. One line per accepted decision still in force;
superseded entries are folded into their successors. `decision.md` remains the append-only
ledger of record; this file is the distilled current state.

- **Seat definition (2026-08-19).** A seat is the human's interactive session on any
  surface; the bound-thread model is canonical. (Absorbs the 2026-08-12 "seats are
  interactive TUIs" wording.)
- **Lease recognition over adoption (2026-08-18).** The occupancy UX trio
  (launcher §5.4–5.6) answers the seat-capture and bare-restore incidents; no direct fix
  to SessionStart adoption or herdr restore. rc-host demoted to a default-off expert
  setting.
- **No zero-cost parking (2026-08-17).** A shipped surface must be retained, kept
  shipping, or stopped; "we do not use it" raises the support bar. Applied: OpenClaw
  Gateway adapter off the seat surface, lab-only, isolation-root guard required.
- **Not a permission gate (2026-08-11).** The communication layer never gates execution;
  wake turns default to full permission with an audit trail
  (`RT_GROK_WAKE_MAILROOM_ONLY=1` restores fencing).
- **Quiet wake (2026-08-07).** Heartbeats retired; `--expect-reply` is the only timer
  edge; fail-closed runtime reclamation at `worktree remove`.
- **No session URLs in the public repo (2026-08-07).** No `Claude-Session:` trailers.
- **Onboarding never dirties a linked worktree (2026-08-07).**
- **Product name pneu (2026-08-06).** rt-*/RT_* prefixes retained; content shipped as
  1.0.0. (Closes the 2026-07-23 Orca rejection.)
- **Antigravity parked at T0; Gemini CLI dropped (2026-08-05).**
- **Launcher-primed first turn for bare Codex launches (2026-07-30).** No-action primer,
  exact-argv pinned, `RT_CODEX_NO_PRIMER=1` escape; tested against Codex 0.146 only.
  (Supersedes the earlier synthetic-turn refusal; builds the backlog item from the
  2026-07-23 zero-turn-arming decision.)
- **Migration robustness frozen (2026-07-29).** Only the exclusive layout lock, verified
  pre-cutover backup, and atomic registry flip are defended.
- **No self-daemonizing `rt-wait-inbox` (2026-07-23).** Arming is owned by lifecycle
  hooks, never model turns (preserves Claude's exit-2 `asyncRewake` contract).
- **Build Week boundary (2026-07-21).** The v0.1.8 tag is immutable and the competition
  worktree is not a development surface (the temporary freeze expired 2026-08-12 by its
  own terms).
- **Quiet ack receipts (wp21).** Maildir-only is the sole normal delivery path;
  `--no-nudge` is a compat alias; `--legacy-nudge-only` is the human-coordinated keyboard
  emergency path — its retain-or-stop ruling is still open (see roadmap).
