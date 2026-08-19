# D7 — quiet wake: remove unconditional heartbeat wakes (+ runtime residue reclamation)

> Status: historical record — D7 dispatch; shipped in 1.1.0

Owner decision (Ocean, 2026-08-07): the periodic empty-inbox heartbeat wake is
retired. An armed seat must cost zero model turns while its inbox is empty.
Wake-on-mail stays exactly as it is. The bridge heartbeat file and the lease
`wake.heartbeatAt` health signal stay — they are diagnostics, not wakes.

## D7a — rt-wait-inbox: silent renewal instead of empty-beat wake

North star: an armed watcher waits indefinitely; the model is woken only by
mail (including `malformed` listings), never by the passage of time.

- On the empty timeout that today prints "heartbeat timeout" and (in Claude
  hook mode) exits 2: instead renew silently and keep waiting. No exit, no
  Stop-hook feedback string, no model turn. The existing supersede checks and
  mail-wake retry semantics are unchanged.
- Fold in the seat-health fix: an armed watcher should stamp
  `wake.heartbeatAt` on a cadence coherent with `DEFAULT_HEARTBEAT_TTL` (30s)
  in `_rtruntime.py`, so an armed Claude seat reads `active_healthy` between
  mails. Today the stamp lands only on 45m beats, so live Claude seats read
  "wake heartbeat is stale" almost always and doctor's WARN is pure noise.
  Pick stamping cadence + TTL together and justify the pair; the invariant is
  armed ⇒ healthy, watcher dead ⇒ stale within one TTL.
- The empty-beat backoff machinery (DEFAULT_MINUTES / FLOOR_MINUTES /
  BACKOFF_AFTER, `empty_beats` counting) loses its purpose; simplify to
  whatever the silent-renewal loop actually needs. Keep `update_wake`'s
  contract coherent for other callers.
- Non-Claude watcher modes (Hermes plugin, plain terminal): same silent
  semantics. Verify the Hermes plugin does not rely on periodic watcher exit
  to stay armed; if it does, document and adapt its lifecycle.
- The watcher becomes a genuinely long-running background process (days).
  The loop must stay RSS/FD-flat; prove it with an accelerated-cadence
  mini-soak, not by inspection.
- Docs truth-pass for the changed semantics: README drain-protocol wording,
  the canonical skill copy (skills/shared/pneu/SKILL.md), and any
  hook-printed instruction text that mentions the heartbeat.

## D7b — reclaim runtime residue at closure

The workstream-closure path retires the registry entry and the worktree but
leaves `~/.roundtable/.runtime/projects/<hash>/` (dead lease + locks) behind
forever. Seven such orphans existed on this host today (backed up and listed
in `~/Documents/Workspace/backups/runtime-stale-lease-dirs-20260807.tar.gz`).

- `rt-worktree remove`: after the existing seat-safety checks pass, also
  delete the retired project's runtime dir — but only when every lease under
  it is dead/stale (owner pid not verifiably live). Any live or ambiguous
  lease ⇒ keep the dir and print an advisory naming it. Fail closed.
- `rt-doctor`: report-only advisory listing runtime project dirs whose
  `projectRoot` no longer exists on disk AND whose registry entry is
  tombstoned or absent. No auto-delete from doctor.

## Constraints

- No delivery-path changes; the maildir remains the sole fact source.
- `rt-codex-wake-heartbeat.json` (bridge heartbeat) is untouched.
- No new daemons, timers, or launchd jobs.
- Version bump to 1.0.1; full suite + compileall + public-safety green;
  update every test that pins the old empty-beat/backoff behavior with
  condition-level mutation checks, not message-string edits.

## Reporting

Work on branch `wt/rt-quiet-wake` in `~/Code/rt-quiet-wake`. When done:
commit, then `rt-say claude@roundtable-product` a one-line pointer to your
handoff (evidence: suite counts, soak numbers, seat-health matrix). I run
acceptance, merge, rebuild the artifact, and hot-swap this host.
