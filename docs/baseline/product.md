# pneu — product baseline

Part of the 2026-08-27 rebaseline (see `README.md` in this directory). This file states
what pneu is for, what it refuses to be, its object model, and its ranked UX principles.
Sources: BRIEF.md, PRINCIPLES.md, README.md, docs/ux/launcher.md, decision.md.

## North star

> Open your harness the way you always do, and mail finds you there.

pneu (Project-Native Envelope Utility — "Postal Network, Entirely Unplugged") is the
durable, local messaging and coordination layer for coding agents sharing a machine.

## Goals, in priority order

1. **UX first.** The product is that one experience. Maildir, leases, fences, and wake
   bridges exist only to deliver it.
2. **Durable delivery without infrastructure.** Per-project maildir mailboxes are the fact
   source; an atomic write into the recipient's `new/` *is* delivery. No daemon,
   multiplexer, account, or network is required for delivery; offline seats lose nothing.
3. **Fenced seat identities.** Seats are the human's own interactive sessions with
   capability-bound identity.
4. **Harness-native wake bridges.** Claude Code (SessionStart/Stop hooks), Codex
   (app-server + Unix-socket bridge), Hermes (session-start plugin), Grok Build (TUI
   activation turn). Wake adapters may use whatever the harness natively provides —
   daemons included. "pneu opposes daemons" is a named false belief; the no-infrastructure
   rule binds the delivery core only.
5. **Honest support claims.** Support is claimed separately from shipping, and only after
   live end-to-end smokes.

## Non-goals

- Cross-host SSH transport, Linux service management, multi-auth switching.
- Headless replacement seats. A mechanism that spawns a headless session, drains mail
  there, and reports success has *failed* the acceptance test. Headless processes are
  legitimate inside a harness, never as user-facing seats.
- pneu as a permission gate. Harness-side permission models own execution policy; wake
  turns default to full permission.
- OpenClaw as a user-facing seat (lab machinery only; `rt-openclaw` refuses).
- Grok ACP supervisor as a seat (internal lab tool).
- Antigravity harness (parked at T0) and Gemini CLI (dropped).
- cmux as a requirement. Core paths work in ordinary terminals and never inject keyboard
  input.
- Heartbeat wakes (retired in 1.1.0). Watchers wake on mail or a configured reply alarm.
- Roster mutation by scan. Display surfaces never mutate state; the roster changes only by
  explicit human act.
- Fleet-grade migration machinery (self-only blast radius calibration).
- rc-host prominence (default-off expert setting).

## Object model

- **Project** — any folder (Git optional) anchored by `.roundtable/`, registered in the
  project registry. Holds per-project maildir mailboxes.
- **Seat** — the human's own interactive session, on whatever surface they actually use
  (terminal TUI, Codex Desktop, a Claude phone session). The surface is not the seat; the
  session is. Multiple clients driving one bound Codex thread are one seat.
- **Agent / instance** — a configured sender id in `.roundtable/agents.yaml`; the roster is
  the project's collaboration authorization list.
- **Mailbox / mail** — registry-selected UUID-addressed maildir. A message file written
  atomically into `new/` is delivery; `rt-ack` publishes a quiet `ack-<msgid>.md` receipt
  and archives to `cur/`. Receipts are never re-acknowledged and never trigger wake.
- **Wake** — an adapter layered over delivery, using whatever the harness natively offers.
  Armed watchers are long-lived, renew fenced leases silently, and wake only on mail;
  `rt-say --expect-reply <dur>` is the only timer edge.
- **Lease** — the seat's liveness/ownership record (vacant / active / stale), fenced by
  the watcher fence and the launch-claim gate.
- **Capability binding (Codex)** — identity bound out of band because the app-server, not
  the launcher, spawns tool processes: seat capability → thread association → per-call
  revalidation. The bound thread is the seat's control entry.
- **Worktree** — `pneu worktree add NAME` creates a registered linked tree under
  `<repo-parent>/<repo-name>-worktree/`; the container holds only pneu-created trees.
- **Surface** — where a seat renders. Surface records store only explicit addresses and
  are advisory navigation metadata, never ownership or liveness evidence.
- **rc-host** — expert, project-anchored opt-in for phone/web worktree spawn (per-project
  LaunchAgent + untracked local hooks); never installed globally.
- **Bound thread** — a persisted, project- and agent-scoped Codex thread binding, offered
  on a vacant/stale seat as a guarded resume.

## UX principles (ranked constitution; higher rank wins conflicts)

1. **UX first.** Acceptance test for any harness adaptation: *does mail reach the session
   the human is actually in?*
2. **Delivery core and wake adapters obey different rules.** Delivery: no infrastructure.
   Wake: anything the harness natively offers.
3. **State discipline.** Surfaces render fresh every run and mutate nothing by being
   looked at; project state changes only by explicit acts.
4. **Support-claim discipline.** Support requires a live end-to-end smoke; fixtures and
   version comparisons are design evidence only. "We do not use it" *raises* the bar. A
   shipped surface has no zero-cost parking state — retain, keep shipping, or stop
   shipping. Docs earn "current" only when checked against code or an artifact.
5. **Brief protocol.** Every dispatch brief opens with the constitution line and the
   track's north-star sentence; surface work must carry target screens.

Launcher-specific rules (as-built 1.3.5): interactive surface on stderr; the card is a pure
view re-reading all state on every redraw; `a` (append one seat block, compare-and-swap) is
the only write to durable project state; Enter writes only git-ignored `launcher.json` and
`execv`s into the harness; unhealthy hosts still get a usable card; unavailable rows always
state why and the next action; exit codes 0 / 2 / 130.
