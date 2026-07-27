# Codex 0.145.0 does not dispatch SessionStart for `--remote` threads

Settled 2026-07-27 on the development host by a controlled launch. This
closes the open branch left by `findings-2026-07-27.md` and by §5.1 of
`audit-2026-07-27.md`.

## Method

The hook was instrumented first, so that "never dispatched" and "ran and
declined" would stop being indistinguishable: `bin/rt-codex-session-start`
now appends one bounded host-local JSONL line on every exit path, and
resolves its trace directory without trusting `RT_RUNTIME_DIR` — a hook
subprocess that never inherited the runtime environment is itself one of
the outcomes worth recording, and the trace names the missing variable when
that happens.

The trace was proven to fire on both decline paths before the experiment,
using a project with no armed intent so no live seat was touched:
`intent_unresolved`, and `incomplete_payload missing:["RT_RUNTIME_DIR"]`
with the variable deliberately unset. Three outcomes were therefore
distinguishable: hook ran and the resolver declined, hook ran without the
runtime environment, and hook never entered (no line at all).

One throwaway Codex seat was then launched through the real launcher
(`bin/rt-codex` → `_rtlauncher.main("codex")`) under a pty, in the scratch
project `rt-naive-test-2`, with every `RT_*` variable stripped so it
behaved like a fresh terminal rather than a nested seat. No keystroke was
sent; the experiment depends only on what happens at launch.

## Result

| observation | value |
| --- | --- |
| intent armed | `2026-07-27T04:03:11.667Z` |
| thread created (UUIDv7 timestamp) | `2026-07-27T04:03:12.208Z`, cwd `rt-naive-test-2` |
| gap vs. the 300 s intent TTL | **0.54 s** |
| hook trace | **0 bytes** |
| app-server stderr for the window | one unrelated MCP OAuth error; no hook error, no traceback |
| bind-request queue | empty |
| intent after the launch | `activeNativeSessionId: null`, `lastSessionStartAt: null` |

A real thread was created, well inside the TTL, and the hook was never
entered. The TTL branch is arithmetically excluded; the crash-at-import
branch is excluded by the clean stderr. Meanwhile `hooks/list` for that cwd
reports the owned entry `session_start:2:0` as `enabled: true`,
`trustStatus: "trusted"`, matcher `startup|resume|clear`.

**Codex `0.145.0` does not dispatch SessionStart hooks for threads created
through the `--remote` app-server path.** Registration, trust, and
enablement are all fine and all irrelevant.

Claude and Hermes are unaffected: their hooks fire, arm watchers, and
maintain lease heartbeats normally. The fault is specific to the Codex
remote path, which is Roundtable's own launch design rather than a Codex
requirement.

## Why the existing auto-discovery could not cover for it

`discover_thread` (`bin/rt-codex-wake:1119`) keeps only candidates whose
thread `source == "cli"`. Its comment states the reason: the app-server
labels every external client `vscode`, including both rt-codex remote TUIs
and genuine IDE sessions, and that union is not a safe auto-binding
identity. Our threads are exactly the refused class, which is why the live
log carries `expected exactly one auto-discoverable local CLI root thread
for …, found 0`.

That refusal was correct given only cwd and source. It is worth restating
what is at stake, because it constrains the fix: this code decides which
native thread receives the user's mail, and a wrong binding delivers a
private message into an unrelated session and can run a turn there. The
anchor the original design lacked is the launcher intent — published before
`exec`, fenced to the current seat lease, and therefore able to prove that
a particular thread is the one this launcher just spawned.

## Status

The instrumentation landed in `e70dfb5`. The binding fix is in progress;
it must not be able to bind a foreign thread that merely shares the project
cwd, and it should repair the seats that are already running unbound, not
only future launches.
