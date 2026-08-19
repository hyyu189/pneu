# 1.4 Track T3 result — watcher lifecycle and observability (D16-1/5/9)

> Status: historical record — 1.4 track T3 result; merged

Branch: `wt/t3-observe`. Not merged; operator merges manually.

## What shipped

### D16-1 step 1 — lifecycle logging

`_rtruntime` gained a per-seat append-only JSONL lifecycle log,
`watcher-lifecycle.jsonl`, written beside that seat's lease
(`log_watcher_event`, `read_watcher_events`, `watcher_lifecycle_summary`,
`watcher_log_path`). `rt-wait-inbox` records every transition:

| Event | When |
| --- | --- |
| `armed` | watcher claimed the wake slot: watcher pid, ppid, supervisor pid, session id, lease revision, pending generation, hook mode, planned lifetime, restart attempt |
| `takeover` | replaced a previous watcher pid, with that watcher's last heartbeat |
| `stand_down` | duplicate live watcher, lost claim, or Stop-hook breaker |
| `fence_rejected` | claim, renewal, or reply-alarm reconcile lost the fence |
| `wake` / `reply_overdue` | the contentful edges, with the generation and wait time |
| `lifetime_rearm` | planned retirement before the Claude hook timeout |
| `supervisor_exited` | the hook process died first; the watcher stands down |
| `crash` | unexpected exception, with the full traceback |
| `exit` | every terminal path: code, reason, and the signal name if any |
| `supervisor_armed` / `watcher_killed` / `supervisor_restart` / `supervisor_child_exit` / `supervisor_exhausted` / `supervisor_signal` | supervisor-side transitions |

Quiet-wake contract preserved: one `O_APPEND` write per record, 0600, rotation
at 256 KiB, every failure swallowed, no stdout/stderr, no model turn. The
reader tolerates a torn tail. A top-level handler catches `BaseException` in
both the watch loop and the forked child, so nothing dies unrecorded.

The decisive diagnostic is negative evidence: `armed` with no following `exit`
or `crash`, and no live watcher, proves an uncatchable kill.
`watcher_lifecycle_summary` returns that as `verdict="unlogged-death"`.

### D16-1 step 2 — idle self-heal (three layers)

1. **Crash class — in-process restart.** An attempt that dies from an
   exception logs the traceback, revalidates the lease, and re-arms with
   backoff; five restarts per five-minute window; a lost lease is never
   re-armed. Off with `RT_WATCHER_SELF_HEAL=0`.
2. **Kill class — supervised fork.** For the Claude async hooks only, the hook
   process forks the watcher and waits. A watcher that dies without producing
   its own exit status is re-forked up to three times. This does **not**
   detach: the hook's own process still exits 2, so the 2026-07-23
   anti-daemonization ruling holds. The child stands down if its supervisor
   dies first, so a dead wake channel is never reported as a healthy seat.
   Off with `RT_WATCHER_NO_SUPERVISOR=1`.
3. **Timeout class — planned retirement.** See the finding below. The watcher
   retires itself `WATCHER_LIFETIME_MARGIN_SECONDS` (300s) before the packaged
   Claude hook timeout and exits 2 with an explicitly no-action notice; the
   ordinary Stop hook then arms a fresh watcher. Off with
   `RT_WATCHER_MAX_LIFETIME_SECONDS=0`.

Honest limits, also written into `docs/architecture.md`:

- A process-group kill — including Claude Code's own hook cancellation — ends
  supervisor and watcher together. Layer 2 does not survive it; layer 3 is
  what keeps the seat from reaching it.
- Nothing can wake an idle Claude session whose hook process is gone. Only a
  process Claude Code spawned can deliver a wake, so when every layer is
  exhausted the seat is deaf until its next turn and mail stays durable in
  `new/`, exactly as for an offline seat.
- Exhausting the supervisor budget escalates once: exit 2 with the lifecycle
  log path, converting silent deafness into one visible turn.

### D16-1 step 3 — journey repro

`tests/test_watcher_lifecycle.py` (13 cases). The pty journey
`test_killed_watcher_is_re_armed_and_still_delivers_mail` arms a real
`rt-wait-inbox --claude-hook` process on a pty, asserts the `armed` record and
a healthy seat, `SIGKILL`s the watcher, asserts `watcher_killed` →
`supervisor_restart` → a second `armed` with a new pid, then delivers mail and
asserts exit 2 plus the drain instructions on the pty.

The mutation counterpart is explicit:
`test_unsupervised_kill_leaves_the_seat_deaf_and_unlogged` runs the same
journey with supervision disabled and asserts the failing outcome — one
`armed`, no re-arm, no `wake`, no `exit`, and
`watcher_lifecycle_summary(...)["verdict"] == "unlogged-death"`. Disabling the
self-heal therefore turns the journey into that test's behavior.
`test_crash_without_self_heal_is_terminal` is the same mutation for layer 1.

### D16-5 — doctor correlation

- An active seat whose watcher is not live now names the recovery instead of
  "restart the wake adapter": *send this Claude session any message — its Stop
  hook re-arms the inbox watcher after the turn — or relaunch the seat: cd
  &lt;project&gt; && RT_FROM=claude rt-claude*. Hermes gets its own wording; a
  harness with no host-side watcher keeps the old text.
- A new `seat-watcher-lifecycle` line renders the lifecycle verdict, uptime,
  last exit reason and signal, and the log path — and stays silent for a
  healthy live watcher and for `stale` seats, whose watcher is expected to be
  gone with the session.
- New `codex-unbound`: a registered project with an **active** Codex seat and
  no stored thread binding is reported as having no waker, correlated with
  recent `auto_bind_rejected` / `auto_bind_request_unsafe` records from the
  wake bridge log (read-only; the bridge's own files are untouched). Bind
  request filenames are lease-identity digests, so rejections are reported as
  recent correlated evidence, not as a proven per-project attribution.

### D16-9 — layout lock residue

`probe_layout_lock` takes the same non-blocking exclusive lock a normal
entrant takes and releases it immediately; it never writes, truncates, or
deletes. `report_layout_lock_residue` classifies every `<uuid>.lock` and
`<uuid>.writer.lock`:

- active registration → silent (a held lock is normal transient contention);
- tombstoned registration, no holder → one aggregated OK line (this residue is
  by design, and enumerating it drowned the report: 66 files on this host);
- UUID absent from the registry, no holder → one aggregated WARN with up to
  three examples, explicitly report-only;
- **held** by a live holder on a retired or unknown UUID → individual WARN,
  the only lock state that names a process worth finding;
- anything else in the directory → WARN.

## Finding for the operator: the 4-hour incident matches the hook timeout

`pneu_packaging/setup.py` sets `CLAUDE_HOOK_TIMEOUT_SECONDS = 15_000` on both
owned Claude hook groups. 15000s is 4h10m; incident #2 died after "~4 hours of
healthy renewals". Claude Code's documented behavior is that `timeout` applies
to command hooks including async ones, and that an over-running hook is
cancelled; the Agent SDK reference describes the cancellation as SIGTERM to
the hook's whole process group followed by SIGKILL. That is a complete
explanation for a silent idle death with no crash, no sleep event, and no
effect on sibling processes.

It does not explain incident #1 (≤70 minutes). Layer 3 removes the timeout
class outright; the lifecycle log will identify whatever killed #1, including
the SIGTERM if one is sent.

Two decisions this raises, both yours:

1. **Raise the hook timeout?** No documented cap. A larger value makes the
   planned retirement rarer (one short turn per window per fully idle seat).
   `rt-wait-inbox.CLAUDE_HOOK_TIMEOUT_SECONDS` is asserted equal to the
   packaged constant by `test_hook_timeout_constant_matches_the_packaged_claude_hook`,
   so raising one requires raising the other.
2. **Is a planned re-arm turn acceptable?** It costs one short turn per ~4h,
   only in a session idle for that whole window. The alternative is a
   guaranteed 4-hourly deaf seat. I shipped it on by default; say the word and
   the default flips to off.

## Risk note

The forked supervisor changes the process shape of every armed Claude
watcher (two small processes per seat instead of one). It degrades to inline
execution if `fork` fails, and `RT_WATCHER_NO_SUPERVISOR=1` disables it. It is
the riskiest part of this change and the first thing to switch off if a live
seat misbehaves after the merge.

## Also added

`RT_WATCHER_POLL_SECONDS` — bounded diagnostic/test override for the idle scan
cadence. It can only shorten the interval, so the "dead watcher is stale
within one heartbeat TTL" invariant is preserved.

## Verification

- `pytest -q`: 1106 passed, 1 skipped (was 1081/1).
- `python -m compileall -q bin pneu_packaging scripts tests`: clean.
- `python scripts/check_public_safety.py`: passed, 195 tracked files.
- Live read-only `rt-doctor` on this host: renders the new fix hint against a
  real active-but-deaf Claude seat, and two clean aggregated layout-lock lines.

## Files

- `bin/_rtruntime.py` — lifecycle log writer, reader, summary; `SeatPaths.watcher_log`.
- `bin/rt-wait-inbox` — instrumentation, signal recording, self-heal layers, supervisor.
- `bin/rt-doctor` — watcher lifecycle correlation, `codex-unbound`, layout-lock probe.
- `tests/test_watcher_lifecycle.py` — new; journey, mutation, and log semantics.
- `tests/test_rt_doctor_diagnostics.py` — new doctor cases.
- `tests/test_rt_tripwire_runtime.py`, `tests/test_rc_host.py` — two in-process
  hook-dispatch cases now opt out of supervision (they assert dispatch, not
  process topology).
- `docs/architecture.md`, `docs/compatibility.md` — design, limits, and knobs.

No T1-owned Codex file was modified; the wake bridge log is read only.
