# 1.4 Track T3 — watcher lifecycle and observability (D16-1/5/9)

Seat: claude (Opus 5, max reasoning effort; fall back to xhigh if max is
unavailable). You are authorized to use ultracode or the Workflow tool at
your discretion for this work.

## Context (real incidents, twice)

The armed Claude inbox watcher (`rt-wait-inbox --claude-stop-hook`) died
mid-idle twice on this host: once ≤70 minutes after arming, once after ~4
hours of healthy ~10s lease renewals — silent both times, machine awake
throughout, no sleep events, sibling long-lived processes unaffected.
Root cause is unreachable post-hoc because the watcher lifecycle writes no
log. Ruled out already: /btw side questions, duplicate-Stop arming (a live
duplicate-arm produced a healthy watcher that fired correctly), system
sleep, machine-wide process kills. Wake works when the watcher is alive
(~8s mail→turn latency, verified repeatedly).

## Scope

1. **Lifecycle logging (D16-1 step 1)**: durable, append-only lifecycle log
   for the watcher — arm (with pid, lease revision, generation), each
   renewal failure, ownership takeover/standdown, fence rejection, every
   exit path (normal fire, superseded, exception — log the traceback), and
   a top-level crash handler so nothing dies silently. Goal: the next
   idle death self-identifies. Keep the quiet-wake contract: logging must
   not create model turns or wake anything.
2. **Idle self-heal (D16-1 step 2)**: design and implement recovery so a
   dead watcher does not leave the seat deaf until the next user turn.
   Constraints: no new daemons if avoidable; prefer mechanisms already in
   the architecture (e.g. re-arm opportunities on existing events, or a
   minimal supervised sentinel consistent with quiet wake). Document the
   chosen design and its failure modes honestly.
3. **Journey repro (D16-1 step 3)**: a pty journey case that arms a watcher,
   simulates/asserts the lifecycle log stream, and pins the self-heal
   behavior.
4. **Doctor correlation (D16-5)**: doctor tells the operator what is wrong
   and how to fix it for: seat active but no watcher heartbeat (today it
   only says "wake adapter has no heartbeat" — add the fix hint and the
   lifecycle-log tail reference); seat active but project unbound with a
   recent auto_bind_rejected; plus **D16-9**: stale/lingering thread-writer
   lock detection (report-only; locks are OS-advisory, so detect
   "lock file present + no live holder" via lock probing, never delete).

## Acceptance

Lifecycle log covers every exit path (prove by test: kill the watcher in
various ways, assert log entries); self-heal journey passes and fails under
mutation (disable self-heal, journey must fail); doctor lines render with
fix hints; suite + public-safety green.

## Constraints

- Files here are Claude/Hermes-side (`bin/rt-wait-inbox`, `bin/rt-doctor`)
  — coordinate nothing with T1's codex files; if you believe you must touch
  a T1-owned file, mail claude@roundtable-product first.
- English-only, public-safe, MARKER_BLOCKS untouched.
- Commit to THIS worktree branch only; do NOT merge — operator merges
  manually.
- Report via `rt-say claude@roundtable-product update ...`.
