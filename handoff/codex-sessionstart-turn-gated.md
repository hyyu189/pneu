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

## The correct fix

Renew rather than report. While the seat owner process is alive and the
intent still names the current fenced lease, a late first turn should be
claimable: either the launcher/bridge refreshes `armedAt`, or the resolver
accepts a late `startup`/`resume` whose intent resolves to the
still-current active lease. The fence that prevents an unrelated native
Codex in the same cwd from stealing the seat is the lease revision plus
owner identity. The clock is not load-bearing for safety, and it should not
be load-bearing for correctness either.

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

## Instrumentation gap to close

The trace records `intent_unresolved` without the resolver's reason, so TTL
expiry, a fence rejection, and a source mismatch are indistinguishable in
the log. The TTL reading above is inferred from the timestamps rather than
read from the trace. Propagate the resolver's decision into the trace.
