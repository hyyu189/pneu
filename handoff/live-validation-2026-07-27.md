# Live validation of the delayed-SessionStart fix

Run on the development host against the installed `fix/intent-ttl` build
(`3ba2c8a`), driven through a pty that reads the rendered TUI and injects
keystrokes. Three protected live seats (`roundtable-product`,
`MoneyMarket-MacroFinance`, `quant`) were never touched; every scenario used a
scratch project.

## Result

| # | Scenario | Verdict |
| --- | --- | --- |
| S1 | Delayed first turn — **the defect itself** | pass |
| S2 | Cold resume follows the screen — **the workflow that was reported** | pass |
| S3 | `/clear` moves a claimed intent to an out-of-window thread | pass |
| S4 | A hook that errors must not break the user's turn | pass, with a finding |
| — | Resolver decision table, 15 rows, offline against the installed build | pass |

### S1 — a first turn 366 s after launch still binds

```
intent armed      08:14:46.265Z
thread created    08:14:46.790Z      +0.525s, inside the launch window
idle              364.4s             past the old 300s arrival TTL
hook fired        08:20:52.565Z      request_published
binding           08:20:54.873Z      full lease fence
```

This is the exact condition that left the 2026-07-25 seats unbound for days.
The old code rejects here.

An unplanned by-product of a first attempt: killing the client immediately
after the hook published made the bridge log
`auto_bind_rejected: owner pid is not running`. A valid request with a dead
seat owner fails closed, which is correct and was not otherwise covered.

### S2 — mail follows the resumed conversation

`/resume` selected a thread created **952.9 s before** this launch armed its
intent. The hook published `source=resume` for that historical thread, the
intent was claimed with it, and the binding landed on it. Critically,
`bound_is_fresh_thread` is **false**: the thread this launch created was
provably not the one bound. That is the reported symptom, gone.

### S3 — `/clear` is the one source that can move a claimed intent

After a 314 s wait, `/clear` created a thread **318 s** past `armedAt` —
outside the launch window by construction — and it was still accepted, because
the window is never re-evaluated once `activeNativeSessionId` is set. The
intent and the binding both moved off the first thread. The popup was
screen-verified to be `/clear` and not `/new`, so a mis-selected command could
not be mistaken for a pass.

### S4 — resilience passes; the one-shot source is a finding

The project's own intent file was chmod'd to `0644` (surgically — damaging the
runtime root would raise for every project and break all three protected
seats), then a turn was taken.

**Pass.** The hook raised, the trace recorded

```
hook_error  runtime path exposes group/other permissions: …/f0cf26ae…/codex-launch-intent.json
```

the TUI surfaced `SessionStart hook (failed) / error: hook exited with code 12`
as an inline notice, **the turn completed normally and the client stayed
alive**. The intent was not claimed and no bind request was queued.

**Finding.** After repairing the permissions, the next turn fired **no hook at
all** — `turn2_saw_line: false`, and the intent stayed unclaimed. The queued
SessionStart source is consumed on its first dispatch and never re-queued, so
**a transient fault at the moment of the first turn costs that session its
binding for the rest of its life.**

This is not introduced by the fix — the hook was always turn-gated and
one-shot. But the fix enlarges the exposed population: before, a session whose
first turn came late had already lost its intent to the TTL, so the single
dispatch mattered less. Now every session depends on that one dispatch
succeeding.

## Two review items for the fix

1. **The trace's remedy names a command that cannot work.**
   `bin/rt-codex-session-start:411` emits
   `remedy="run rt-codex-wake bind from the target Codex turn"`. In a
   `--remote` thread that command always fails: it requires `RT_SESSION_ID`
   and `RT_LEASE_REVISION`, and the shell is spawned by the launchd
   app-server whose environment carries no per-seat fence at all
   (`bin/rt-codex-wake:481`, and the app-server plist). The honest remedy for
   a remote seat is to relaunch it.
2. **A permanently unclaimed intent has no recovery but relaunch**, per the
   finding above, and nothing tells the operator that. Worth a distinct
   diagnostic once (1) is corrected.

Neither blocks the fix. Both are cheap.

## Deliberately not run

Restarting the app-server and stopping the wake bridge were both rejected
before execution. The app-server is one host-wide LaunchAgent on one socket, so
restarting it disconnects all three protected seats, and its plist is
unconditional `KeepAlive`, so a controlled outage is not even achievable. The
wake plist is `KeepAlive {SuccessfulExit: false}`, so stopping the bridge takes
Codex wake down host-wide until a human restores it. Both would have tested
properties this fix did not change.

## GUI automation was not needed, and could not have helped

For a `--remote` thread the SessionStart hook subprocess inherits the
**app-server's launchd environment**, not the terminal's. Its four inputs —
`source`, `session_id`, `cwd`, `RT_RUNTIME_DIR` — are all supplied by the
app-server or by Codex itself. No terminal host, Electron or otherwise, can
alter them. Every fact this fix touches is a file fact, and thread creation
time decodes from the thread id. A pty renders the TUI fully, which covers the
only two screen-dependent assertions (the resume picker and the transcript).

## Method note

Four separate wrong readings occurred during this work, none of them caused by
the product:

- a zero-byte hook trace read as "the hook never runs", from an experiment that
  never sent a turn, when dispatch is turn-gated;
- "the TUI has no `/resume`", from a regex summary of a badly ANSI-stripped
  screen;
- `picker: false` while the same capture plainly contained
  `Resume a previous session`, because a fixed timeout expired before MCP
  startup finished drawing it;
- trace lines from another project attributed to this one, because a shared
  append-only file was sliced by byte offset instead of by project.

The common shape is treating an indirect quantity as if it were the target
quantity. The corrections were the same shape too: assert on attributable
facts — the project hash inside the line, the UI state actually reached, the
creation instant encoded in the thread id — rather than on elapsed time or file
position. That is the same correction the fix under test makes: it moves the
window from when the hook happened to arrive onto when the thread was provably
created.
