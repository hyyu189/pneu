# 1.4 Track T1 — capability binding, bridge hygiene, canonical daemon

> Status: historical record — 1.4 track T1 dispatch; merged

Seat: claude (Opus 5, max reasoning effort; fall back to xhigh if max is
unavailable). You are authorized to use ultracode or the Workflow tool at
your discretion for this work.

## Scope

Implement, in this worktree, the decided architecture and hygiene batch:

1. **Generic capability resolver** per
   `handoff/archive/codex-capability-binding-architecture.md` (the decided model —
   read it first, in full): fenced tools (`rt-ack --fenced`,
   `rt-say --fenced`, `--expect-reply` seat-state writes) resolve identity
   via native `CODEX_THREAD_ID` → exact binding → active lease validation
   when ambient `RT_*` fence variables are absent. Stale lease, changed
   binding revision, or cwd mismatch refuses. Existing lifecycle skeleton
   (`_rtlauncher.claim_launch_seat`, `arm_codex_launch_intent`,
   `rt-codex-session-start` intent restore, `rt-codex-wake` validate+bind)
   is extended, not redone.
2. **Seat-capability record**: private record keyed to the lease carrying
   `threadId + bindingRevision + roundtableSessionId + leaseRevision` plus
   minimal surface data (kind, pane id, workspace/tab/session id or private
   socket endpoint — never full env/HOME/PATH/tokens). Joint validation
   against current binding + lease at every use. `surface.json` stays
   advisory. Never fabricate `HERDR_ENV=1` in the daemon; explicit
   endpoints only; if the herdr CLI requires an ambient socket, use a small
   broker executing in a genuine herdr environment.
3. **Lifecycle fail-closed rules**: resume keeps binding; clear migrates
   only after new SessionStart/CAS under the same active lease; /btw side
   threads, forks, new threads inherit nothing; supersede/mismatch/pane-gone
   fails closed immediately.
4. **Bridge hygiene (D16-2/3/4/8)**: SessionStart hook and drain path skip
   ephemeral/fork child threads (no bind requests consumed for them);
   `auto_bind_rejected` events carry project + thread_id; manual
   bind/unbind and every binding removal write log events; `rt-projects rm`
   reclaims the codex binding like `worktree remove` does, AND the launcher
   validates a stored binding's project UUID/registration generation and
   discards on mismatch.
5. **Launch feedback (D16-7)**: staged status lines during the silent
   codex seat launch window (checking service → binding thread → priming
   seat).
6. **Canonical daemon productization** per
   `handoff/archive/codex-canonical-daemon-proposal.md` (Adoption mechanics
   section): `pneu setup apply` codex section applies
   `launchctl setenv CODEX_APP_SERVER_USE_LOCAL_DAEMON 1` automatically,
   owns a re-apply-at-login mechanism, records it in the ownership
   manifest, and on disable/uninstall unsets BEFORE removing the daemon.
   Doctor gains: dual-host inventory (report-only), join drift probe, and
   fd/resource headroom check. Promotion unknown #1 (phone reachability) is
   already resolved empirically on this host — reachability works after the
   switch; unknown #2 (Desktop behavior when the daemon is unavailable)
   needs a test and an honest doctor/docs statement.

## Acceptance

Stages 1–3 of the architecture doc, verbatim — including the
client-equivalence test (Desktop joined via the switch operates the bound
thread as the same seat with the same fences; a /btw child resolves to
nothing). Full suite + compileall + public-safety green.

## Constraints

- Never patch or modify the Desktop app. Never restart the codex
  app-server daemon or bare-stop the wake bridge (bridge-only reload is the
  sanctioned path). Writer lock untouched. MARKER_BLOCKS in
  roundtable-init stay byte-identical. English-only artifacts; public-safe
  (no personal absolute paths, no session URLs).
- Commit to THIS worktree branch only. Do NOT merge to main — the operator
  reviews and merges manually.
- Report completion with handoff pointers via
  `rt-say claude@roundtable-product update ...`; mail the same address if
  blocked.
