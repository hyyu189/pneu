# T3 amendment — implementation brief for the codex seat

Seat: `codex@t3-observe` (this worktree, branch `wt/t3-observe`).
Director: `claude@t3-observe`. Report back with `rt-say claude update ...`.

You are implementing three independent items on top of commit `488cc33`.
Do not merge. Do not touch `main`. Commit nothing yourself — the director
reviews your working tree and commits approved work with attribution.

Read `handoff/t3-observe-1.4.md` first: it is the record of what 488cc33
shipped and why. Items 1 and 3 modify that work.

---

## Item 1 — timer ruling: 14-day hook timeout, retirement as the only proactive layer

Operator ruling, decided directly with Ocean. Two parts, both required.

### 1a. Raise the packaged Claude hook timeout to 14 days

`pneu_packaging/setup.py` sets `CLAUDE_HOOK_TIMEOUT_SECONDS = 15_000` on both
owned Claude hook groups. That is 4h10m, and it is the verified cause of a
silent idle death: Claude Code arms `setTimeout(kill, timeout*1000)` when it
spawns the hook, then SIGTERMs the process group and discards its output.

Change it to `1_209_600` (14 days) in `pneu_packaging/setup.py`, and change
the mirrored `CLAUDE_HOOK_TIMEOUT_SECONDS` in `bin/rt-wait-inbox` to match.
`DEFAULT_WATCHER_MAX_LIFETIME_SECONDS` stays `timeout - 300`.

Verified facts you may rely on (read from the shipped Claude Code 2.1.233
binary; do not re-derive, and do not weaken these guards):

- the hook-config schema is `timeout: ut().positive().optional()` — no `.max()`;
- the timeout is used verbatim: `M = e.timeout ? e.timeout*1000 : 600000` — no clamp;
- the runtime is Bun, and `getTimerDuration` **silently truncates** any delay
  above `2**31 - 1` ms (~24.855 days) to that ceiling.

Add a regression test asserting the cliff, so a future edit to e.g. 30 days
fails loudly instead of being silently clamped back:

```python
# Bun truncates any setTimeout delay above 2**31-1 ms, so a hook timeout
# beyond ~24.8 days is silently clamped instead of honored.
assert CLAUDE_HOOK_TIMEOUT_SECONDS * 1000 < 2**31 - 1
```

The existing `test_hook_timeout_constant_matches_the_packaged_claude_hook`
already pins the two constants to each other. Keep it.

### 1b. Remove the forked supervisor layer

488cc33 shipped three self-heal layers. The ruling keeps two and removes one.

**Remove entirely** — the kill-class forked supervisor in `bin/rt-wait-inbox`:
`_supervisor_enabled`, `_supervisor_lifecycle`, `_reap_child`, `_supervise`,
every `SUPERVISOR_*` constant, the `supervisor_pid` parameter threaded through
`run()` / `_watch()`, the orphaned-child stand-down check in the poll loop, and
the `supervisor_*` / `watcher_killed` lifecycle events. `main()` returns to
calling its `execute` path directly.

Two tests currently opt out of supervision with `RT_WATCHER_NO_SUPERVISOR=1`
because they assert hook dispatch in-process — `tests/test_rt_tripwire_runtime.py`
(`test_claude_stop_hook_complete_fence_still_reaches_managed_run`) and
`tests/test_rc_host.py` (`test_unmanaged_stop_hook_restores_matching_active_lease`).
With supervision gone those opt-outs are dead weight: revert both to their
pre-488cc33 form, including the `options.pop("supervisor_pid", None)` lines.

**Keep** — lifecycle logging (all of it), the in-place crash-class restart in
`run()` (`RT_WATCHER_SELF_HEAL`), and the planned retirement
(`RT_WATCHER_MAX_LIFETIME_SECONDS`). `_PROCESS_STARTED` stays: it anchors the
retirement deadline to process start.

**Tests.** `tests/test_watcher_lifecycle.py` must keep a pty journey that arms
a real watcher, asserts the lifecycle event stream, and pins self-heal — that
is a standing acceptance requirement, not an artifact of the supervisor. Rework
it onto the surviving layers: the crash-restart pair
(`test_crash_self_heals_in_place_and_records_the_traceback` /
`test_crash_without_self_heal_is_terminal`) is the mutation pair, and the
retirement case is the proactive one. Delete the supervisor-only cases. The
kill case must survive in reduced form: SIGKILL the watcher, then assert the
seat is deaf, no `exit` record was written, and
`watcher_lifecycle_summary(..., watcher_live=False)["verdict"] == "unlogged-death"`.
That case is now the *documented outcome*, not a mutation.

**Docs — do not skip this.** `docs/architecture.md` (§ "Watcher lifecycle,
self-heal, and their limits") and `docs/compatibility.md` both describe three
layers today. Rewrite them honestly for two: a kill aimed at the watcher is no
longer recovered at all; it leaves the seat deaf until its next turn, with the
lifecycle log recording the unlogged death and `rt-doctor` naming it. Say that
plainly rather than softening it. Remove `RT_WATCHER_NO_SUPERVISOR` from the
documented knobs.

---

## Item 2 — D1, as narrowed by the codex cross-check (SCOPE CORRECTED)

**Read this version, not the earlier one.** The original review's headline —
"`rt-doctor` has zero OpenClaw coverage" — is **false**, and you must not
describe your change that way. `inspect_project_seats()` iterates every
configured harness and calls the generic `inspect_seat()`, so an OpenClaw seat
already gets seat-status, owner-anchor, and watcher-anchor checks today. Source:
`git show wt/t5-adapters:handoff/architecture-review-1.4-crosscheck-codex.md`
§ "D1 — inaccurate as stated; a narrower defect is real".

Two real defects, both in scope:

**2a. `harness_family()` omits OpenClaw**, so `launch_fix()` falls back to the
less useful generic "restart the wake adapter" text instead of naming
`rt-openclaw`. The canonical alias set is `bin/_rtlauncher.py:71` —
`frozenset({"openclaw", "openclaw-gateway"})` — so map each to family
`"openclaw"`. Add a test pinning every alias in that launcher set to a family,
so the next harness cannot be added to one table and forgotten in the other.

**2b. The `seat-identity` comparison can miss a real mismatch.** In
`inspect_project_seats()` the guard is
`harness_family(str(declared_harness)) != harness_family(harness)`. A configured
OpenClaw seat normalizes to `None` today, and so does a runtime record declaring
*any* unknown harness — so `None != None` is false and a genuine identity
mismatch is silently not reported. Adding OpenClaw to the table closes this for
OpenClaw specifically.

Recommended shape for closing it generally, if it survives your own review:
compare `harness_family(x) or x`, so unknown harnesses fall back to raw-string
comparison. Known aliases are unaffected; two different unknown harnesses stop
comparing equal. Verify this against the existing seat-identity tests before
adopting it, and say so if you reject it.

**Required deliverable: an identity regression test that proves the
`None`-vs-`None` miss is closed** — a configured OpenClaw seat whose runtime
record declares a different harness must produce the `seat-identity` FAIL. A
test that only checks the family mapping does not discharge this.

**Out of scope:** no OpenClaw-specific Gateway/adapter probe — that adapter is
parked. And **do not** attempt the `_rtharness.py` registry extraction; that is
sequenced work owned elsewhere.

---

## Item 3 — grok-monitor cross-project false positive

Operator-approved fix. Live proof, from the 2026-08-14 T4 escalation:

```
OK grok-monitor: project=~/Code/rt-grok-e2e ...
   evidence found in .../sessions/%2F...%2Ft4-grok/019fff89-.../chat_history.jsonl
```

`grok_monitor_evidence()` in `bin/rt-doctor` walks the **entire**
`~/.grok/sessions` tree and returns the first file whose text contains the
expected maildir path plus `monitor` plus `backgroundtasks|background_tasks`
plus `persistent:true`. It never scopes to the session belonging to the project
being reported on. It is report-only, but it fails in the **unsafe direction**:
an unarmed seat is reported armed, and the match can be satisfied by arbitrary
text a Grok session merely *read*.

Two changes, both required:

**3a. Scope the walk.** Grok stores sessions as
`~/.grok/sessions/<percent-encoded-cwd>/<session-uuid>/...`. Select only the
top-level entry whose **percent-decoded** name equals the project root
(`urllib.parse.unquote`), and walk only that subtree. Decode rather than
re-encode: encoding choices vary, decoding is exact. No matching directory is
`absent`, not `unreadable`. `grok_monitor_evidence()` will need the project
root in addition to the expected maildir; update
`report_grok_monitor_liveness()` and the fixtures accordingly.

**3b. Require a real record, not arbitrary text.** Parse candidate JSONL
records and require a structural hit, not a substring soup. Verify the exact
shapes against the live files before you code — they are readable right now:

```
~/.grok/sessions/%2FUsers%2F<user>%2FCode%2Froundtable-product-worktree%2Ft4-grok/019fff89-7b0c-7ad2-8a3e-12a668feea20/chat_history.jsonl
```

Confirmed shapes in that file:

- an assistant record with `tool_calls[].name == "monitor"` whose `arguments`
  is a JSON string containing `"persistent": true` and a `command` naming the
  maildir;
- a `tool_result` whose content begins `Monitor started (task <uuid>, persistent`;
- a `system-reminder` user record reporting `Monitor "<uuid>" ended: [monitor
  ended: killed by signal max_runtime]`.

Note the third: Grok's background tasks have a vendor `max_runtime` cap
(observed twice at 36000.3s = 10h), after which the model re-arms on its own.
Evidence of a monitor that has since ended is **not** evidence of an armed
monitor — prefer the most recent record and do not report `present` when the
latest monitor record for that maildir is an end/kill notice.

Keep every existing safety bound: file-count and byte caps, symlink refusal,
`GROK_MONITOR_SUFFIXES`, and the report-only framing (`evidence is not a
lease`). Keep the fixture-root seam the unit tests use.

---

## Constraints

- Branch `wt/t3-observe` only. **Do not commit and do not merge** — leave your
  work in the tree and report; the director reviews and commits.
- Do not touch T1-owned Codex bridge files. `bin/rt-codex-wake` and the wake
  bridge log are read-only to this work.
- **Guidance, not scope**: `architecture-review-1.4.md` § RC4 describes an
  end-state probe abstraction for `rt-doctor`. Read it so your changes do not
  fight that direction — but do **not** harden the current hand-wired probe
  shape further, and do not build the abstraction here. Items 2 and 3 should
  add as little new hand-wiring as they can.
- English-only, public-safe (no personal absolute paths in committed files, no
  session URLs). `MARKER_BLOCKS` in `roundtable-init` stay byte-identical.
- Green before you report: `pytest -q`,
  `python -m compileall -q bin pneu_packaging scripts tests`,
  `python scripts/check_public_safety.py`.
- Report what you did **not** do, and anything you found that contradicts this
  brief. A brief that turns out to be wrong is a finding, not an obstacle.
