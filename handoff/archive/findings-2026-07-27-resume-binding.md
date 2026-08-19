# The bound thread is not the thread the human is looking at

> Status: historical record — resume-binding defect record; its root-cause correction is in codex-sessionstart-turn-gated.md

Found on the development host on 2026-07-27, immediately after the
intent-anchored bind replacement started working.

**Root cause corrected.** This document originally attributed defect 1 to a
missing Codex API and a lost hook. Both premises were wrong; see
`codex-sessionstart-turn-gated.md`. SessionStart is dispatched normally,
including `source=resume`, and would have rebound the resumed thread on its
own. Defect 1 was *introduced* by the intent-anchored discovery layer,
which claims the intent seconds after launch and thereby makes the hook's
legitimate resume claim fail the `active != native_session_id` guard.
Defects 2 and 3 below stand as written.

## 1. Launch-thread anchoring cannot follow a TUI resume

Reproduced by ordinary use. Ocean launched `roundtable` → codex, then chose
`resume` in the TUI to continue a previous conversation. The launcher had
armed its intent, the app-server had created a fresh thread
`019fa201-9302` 0.7 s later, and discovery bound that thread — correctly by
its own rule. The TUI then moved to the resumed conversation and left the
freshly created thread abandoned.

Mail therefore landed in a thread nobody was watching. From the human's
side it looked like the message created a brand-new conversation, and it
took a second trip through the resume selector to find it.

`CODEX_THREAD_ID` in the turn was still the launch thread, so the
environment identity does not follow a resume, and Codex `0.145.0` exposes
no way for an external app-server client to learn which thread another
client displays — no `currentThread`
query, no client-to-thread mapping, no `thread/attached` or
`thread/switched` notification, and `remoteControl/client/list` carries
client metadata without a thread id. A TUI resume is a client-to-server
`thread/resume` request that no notification republishes. Thread status is
runtime activity, not UI focus, so `thread/loaded/list` plus status cannot
identify the displayed thread either.

No launcher-intent or recency heuristic can solve this from the outside,
because the information required is not published. That mattered only while
the hook was believed dead. The hook is alive and carries the resumed
thread's own identity, so the supported answer is to stop pre-empting it,
not to reconstruct it externally.

## 2. The documented manual fallback cannot work in a remote thread

`rt-codex-wake bind` is what `README.md` and the Roundtable skill both name
as the troubleshooting fallback, and it is inoperative on this
architecture. Observed verbatim in the `MoneyMarket-MacroFinance` seat:

```
• You ran rt-codex-wake bind
  └ rt-codex-wake: RT_SESSION_ID and RT_LEASE_REVISION are required; launch this thread
    with rt-codex
```

The gate is `bin/rt-codex-wake:481`. It is reached with a perfectly healthy
seat: `inspect_seat` returns an active status and a validated token, and
the command still fails because `RT_SESSION_ID` and `RT_LEASE_REVISION` are
absent from the shell environment.

They are absent by construction. Under `--remote`, shell commands run in
the app-server process, which launchd starts from
`com.roundtable.codex-app-server.plist`. That environment carries
`RT_RUNTIME_DIR`, `RT_CODEX_RUNTIME_DIR`, `ROUNDTABLE_INSTALL_PREFIX`,
`RT_CODEX_BIN`, `CODEX_HOME`, `HOME`, and `PATH` — no per-seat fence at
all. The launcher publishes the fence as
`-c shell_environment_policy.set.RT_SESSION_ID=…` on the client command
line, and that client-side override does not reach the server-side shell
spawn. Corroborated independently: in the first successful wake turn, Codex
had to write `RT_FROM=codex rt-say …` because `RT_FROM` was not in its
shell either.

So the fallback fails in exactly the situation that needs it, and it fails
the same way in a freshly launched thread as in a resumed one.

## 3. `doctor` reports such a seat as healthy

All three Codex seats reported `active_healthy` with fresh wake heartbeats
while this was happening, and that reading was accurate for what the check
measures: the seat lease has a live owner and the wake adapter is
heartbeating. It says nothing about whether mail reaches the conversation
the human is actually in, and nothing about whether the seat's only manual
repair still works.

Two expected behaviors:

1. Seat health must not be reported in a vocabulary that implies
   deliverability it never checked. A seat whose bound thread has received
   only Roundtable wake turns, while another thread shares its project cwd,
   is a distinct and reportable state.
2. `rt-codex-wake bind` should detect that it can never satisfy its own
   precondition in a remote thread and say so, instead of asking the
   operator to relaunch with the launcher that already launched it.

## Options — superseded

The three options below were written on the assumption that the hook was
gone. With the hook working, B and C are both solving a problem that does
not exist, and the real work is removing the discovery layer's pre-emption
and renewing the intent so a late first turn can still claim it. They are
kept only as the record of what was considered.


**A — detect and tell the operator to rebind.** Retracted. Its remedy is
defect 2, which does not work. Any variant of A needs a repair action that
exists first, and a channel that actually reaches a human sitting in a TUI;
today the only channels are `doctor` and the wake log, neither of which the
human is looking at.

**B — follow user activity.** Rebind to the thread in the project cwd that
most recently ran a user-initiated turn, driven by `thread/status/changed`.
Handles resume and switching naturally. It reintroduces precisely the risk
the original design refused: a genuine IDE or ChatGPT-app session in the
same cwd also runs user turns and could capture the seat, which routes
private mail into a session the sender never addressed. Fails closed on
ambiguity, misdelivers when the foreign session is the only active one.

**C — interpose on the client's own request stream.** Roundtable controls
the launch, so it can point the TUI at a Roundtable-owned socket that
forwards to the app-server while observing that client's `thread/resume`.
This is the only approach that obtains ground truth. It puts Roundtable in
the data path of every Codex session, which is a real reliability and
complexity cost.

**D — upstream.** Ask for a client-to-thread association or a
`thread/switched` notification. Independent of the above and worth filing
regardless, since B and C are both workarounds for a missing API.

## Note on the original design — confirmed

The hook's supported sources are `startup`, `resume`, and `clear`, and
`resolve_codex_launch_intent` accepts the same set, with `clear` explicitly
privileged to move the active native thread. The original design expected a
resume to re-fire SessionStart and rebind, and that expectation is correct:
the live trace shows a `resume` request publishing normally. The design was
right and the replacement was built on a misdiagnosis.
