# SessionStart is turn-gated, and the launch intent expires before it fires

Corrected 2026-07-27. This file previously claimed that Codex `0.145.0`
does not dispatch SessionStart for `--remote` threads. **That claim was
false.** The retraction and the actual mechanism are both recorded here,
because the wrong conclusion drove a code change, and the reasoning error
is the more useful artifact.

## What was claimed, and why it was wrong

The claim rested on a "controlled" launch: the hook was instrumented to
append one line on every exit path, a seat was launched through the real
launcher under a pty, the app-server created a thread 0.54 s after the
intent was armed, and the trace file was zero bytes afterwards. That was
read as proof the hook never ran.

The experiment never sent a turn. Codex `0.145.0` queues the SessionStart
source at session construction and dispatches it on the **next turn**:
`codex-rs/core/src/session/session.rs:1261-1274` maps
`InitialHistory::New/Forked` to `Startup`, `Resumed` to `Resume`, and
`Clear` to `Clear`, then calls `queue_pending_session_start_source(...)`;
the dispatcher is
`codex-rs/core/src/hook_runtime.rs::run_pending_session_start_hooks`
(103-155), invoked from `codex-rs/core/src/session/turn.rs::run_turn`
(188-190). `thread/start` alone does not execute the hook. A zero-byte
trace immediately after thread creation is therefore the *expected* result
of that implementation and proves nothing.

The experiment was designed on an explicit assumption — "the experiment
depends only on what happens at launch" — that was never tested and was
wrong. The instrumentation was the right move; the experiment built on it
was not.

Two further claims fell with it:

- **Not a regression.** Comparing the official `rust-v0.144.6` and
  `rust-v0.145.0` tags shows the queue site, the first-turn dispatcher, and
  hot-resume behavior present in both. The only relevant `0.145` diff adds
  an invocation after a compact-recovery path. The release notes list a
  SessionStart-after-compact fix, not a remote omission, and no upstream
  issue matches.
- **Resume does signal.** `source=resume` fires normally on a cold resume.

## What is actually true

The live hook trace, after the seats had run real turns:

```
05:16:15  intent_unresolved   startup  thread 019fa1b5 (created 03:54:17)
05:20:00  request_published   startup  thread 019fa201-9302
05:32:57  request_published   resume   thread 019fa201-9302
05:35:20  request_published   startup  thread 019fa201-0134
```

The hook runs. It publishes on both `startup` and `resume`. The first line
is the defect: the hook fired at 05:16:15 for a thread created at 03:54:17,
against an intent armed at 03:54:16 with a 300 s TTL
(`DEFAULT_CODEX_LAUNCH_INTENT_TTL`). By the time the human's first turn
triggered the hook, the intent had been dead for roughly 82 minutes, so
`resolve_codex_launch_intent` returned `None` and no bind request was
published.

**A seat binds only if the human sends their first message within 300
seconds of launch.** Launch, get coffee, come back and type, and the seat
is silently unbindable for the rest of its life. That is why the
2026-07-25 seats sat unbound for days.

This is exactly finding F5 of `audit-2026-07-27.md` — the launch intent is
a one-shot 300 s token with no renewal path. The audit reported it; it was
filed as mechanism detail behind a louder wrong hypothesis and
under-weighted.

## The implemented fix

The earlier proposal to renew `armedAt` or accept any late request from a
live lease was incomplete: the lease proves the current Roundtable seat and
owner process, but it does not correlate an app-server thread with that
client. An unrelated same-cwd remote client could therefore win an
unbounded first-claim race.

Codex-generated thread IDs are UUIDv7 in both `0.144.6` and `0.145.0`, so
the resolver now applies the 300-second window to the fresh thread's
embedded creation time rather than to the delayed hook arrival time. The
real `019fa1b5...` thread decodes to `03:54:17.762Z`, roughly one second
after its launch intent was armed, so its first turn remains claimable 82
minutes later. A same-cwd thread created outside that fixed launch window
is rejected even if its hook runs first.

A cold `resume` preserves a historical thread ID rather than creating one
at launch, so it cannot use the same creation-time association. The
resolver accepts a historical resume only under the exact current live
lease fence and documents this as part of P0's one-interactive-Codex-seat
cooperative boundary. Strict per-client attribution still requires an
upstream client identity or nonce in SessionStart and `thread/read`.

## The intervening change made it worse

Intent-anchored discovery (commit `9367cb4`) was built to replace a hook
that was never broken. It claims the intent within seconds of launch and
writes `activeNativeSessionId`. When the human then resumes a different
thread, the hook fires `source=resume` exactly as designed, but
`resolve_codex_launch_intent` takes the `active != native_session_id`
branch — "a nested or unrelated root thread cannot steal an established
launch intent" — and returns `None`.

So the discovery layer did not merely fail to help: it pre-empted the
mechanism that would have rebound the resumed thread correctly. The resume
mis-binding recorded in `findings-2026-07-27-resume-binding.md` was
introduced by it.

## Instrumentation gap closed

The hook now propagates the resolver's stable reason into every
`intent_unresolved` trace. Fence rejections also include their safe detail,
so a dead owner, replaced lease, missing intent, invalid thread timestamp,
and active-session mismatch no longer collapse into the same line. Each
unresolved trace carries the in-thread manual-bind remedy.
