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

Forensic taxonomy (t3-observe): on any *caught* exit the watcher's
`clear_wake` pops both `watcherPid` and `heartbeatAt` from the lease wake
block, so a record with both fields absent and mtime at the exit instant is
a CLEAN exit; an uncatchable kill leaves `watcherPid` with a stale
`heartbeatAt`, mtime within ~5 s of death.

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

## Verdict

**CLEAN TIMEOUT, on schedule.** Claude Code cancelled the over-running Stop
hook at its configured timeout; the watcher caught the signal and cleared
its slot on the way out. This is the third observed idle death and the
first with controlled conditions and full forensics; it confirms the
root-cause diagnosis behind the 14-day timeout fix merged from
wt/t3-observe.

Explicitly not explained by this experiment: incident #1 (a death ~70
minutes after arming on 2026-08-12). That mode, if real, is distinct; the
lifecycle log shipped in 1.4 will identify it on next occurrence.

## Operational notes

- A heartbeat regime with cadence shorter than the watcher timeout is
  life-support, not monitoring. Detection requires an unpinged control or a
  cadence beyond the timeout.
- Post-death, any interaction with the seat re-arms a fresh watcher and
  overwrites the evidence; capture the frozen record first.
- A liveness probe sampled during a model turn always reads
  no-heartbeat (the Stop hook arms only after the turn ends); the reply to
  a handshake, not an `inspect_seat` sample, is the liveness signal.
