# D16-1 control experiment — watcher death forensics

Date: 2026-08-17. Subject: the installed 1.3.3 Claude Stop-hook watcher on
an idle seat, deliberately removed from the fleet heartbeat roster as an
unpinged control.

## Design

The 4-hourly fleet handshake was shown (by the t3-observe seat) to *prevent*
the watcher-death bug rather than detect it: a mail wake makes the incumbent
watcher exit and the post-turn Stop hook arms a fresh one with a fresh
timeout clock, so any cadence shorter than the timeout resets every clock it
touches. The control seat (t2-tests, its work fully committed and merged)
received no mail after its final Stop at 08:43 PDT and its pane was never
touched. Observation was passive: filesystem reads of its lease record only.

Forensic taxonomy (t3-observe, self-corrected after first publication):
`clear_wake` runs on EVERY caught exit path — mail wake, reply-alarm wake,
supersession, stand-down, caught signal, planned retirement, propagating
crash — so a record with both `watcherPid` and `heartbeatAt` absent proves
only that the watcher left through its own finally block, not which path.
Only the converse is discriminating: `watcherPid` PRESENT with a stale
`heartbeatAt` (mtime within ~5 s of death) = uncatchable kill. Attributing
a clean exit to the hook timeout therefore requires timing plus exclusion
of the other clean paths, which the controlled design provides.

## Result

- Baseline (final Stop, clock start): 08:43 PDT.
- Prediction under the timeout hypothesis (`CLAUDE_HOOK_TIMEOUT_SECONDS`
  value 15000 read as seconds = 4 h 10 m): death ≈ 12:53 PDT.
- Observed: lease mtime 12:52 PDT; frozen wake block read 12:53:17 PDT:

```json
"wake": {
    "lastWakeMessages": [
        "20260817T154146Z-claude-to-claude-83371.md"
    ],
    "wakeAttempts": 1
}
```

- `watcherPid` and `heartbeatAt`: both absent. Watcher process gone from
  the process table; five sibling watchers (other seats) unaffected.

## Timeline (settled by the seat's own transcript, read passively)

A timezone slip in the first cross-read briefly suggested a death at
12:52Z (~72 minutes after a morning arm). The seat's transcript refutes
it: the final conversation entry is 15:42:18Z ("Control seat accepted.
Going silent"), with zero turns after. So: final Stop and arm ≈ 15:43Z,
lease frozen 19:52Z — an undisturbed watcher lifetime of ~4 h 09 m. The
earlier watcher (armed after the 11:39Z handshake reply) exited legitimately
via mail wake when the control-designation mail arrived at 15:41Z.

Incidental positive finding, upgraded from empirical to by-construction:
two quiet ack receipts landed in the seat's `new/` at 15:43:12Z, seconds
after the final arm, and produced no turn. `rt-wait-inbox`'s `_wake_mail`
filters the listing with `not name.startswith(("ack-", "."))`, so receipts
are excluded from the wake generation by design on every build.

Method note (from the timezone slip that briefly produced a phantom
72-minute timeline): never let a format string assert a timezone the tool
did not apply — macOS `stat -f %Sm` renders local time regardless of a
literal `Z` in the format. Force `TZ=UTC` or read the ISO field the record
already carries (`heartbeatAt` is written by `utc_now()` and is
unambiguous).

## Verdict

**Timeout death, on schedule — attributed by timing plus exclusion.** The
lease signature proves an orderly exit; the 4 h 09 m elapsed time against
the 4 h 10 m configured ceiling, in a window with no mail, no interaction,
no supersession, and no retirement logic (1.3.3 has none), leaves the hook
timeout as the only live cause. Third observed idle death, first under
controlled conditions; confirms the root-cause diagnosis behind the 14-day
timeout fix merged from wt/t3-observe.

Explicitly not explained by this experiment: incident #1 (a death ~70
minutes after arming on 2026-08-12). Open hypothesis from t3-observe worth
keeping beside it: a turn that ends in user interruption or an API/auth
failure does not run a usable Stop hook, so the seat goes deaf with no
watcher ever armed — a mechanism no self-heal layer or timeout change
addresses. The 1.4 lifecycle log identifies either mode on next occurrence.

## Operational notes

- A heartbeat regime with cadence shorter than the watcher timeout is
  life-support, not monitoring. Detection requires an unpinged control or a
  cadence beyond the timeout.
- Post-death, any interaction with the seat re-arms a fresh watcher and
  overwrites the evidence; capture the frozen record first.
- A liveness probe sampled during a model turn always reads
  no-heartbeat (the Stop hook arms only after the turn ends); the reply to
  a handshake, not an `inspect_seat` sample, is the liveness signal.
