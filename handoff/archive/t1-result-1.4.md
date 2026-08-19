# 1.4 Track T1 — result

> Status: historical record — 1.4 track T1 result; merged

Branch: `wt/t1-codex-core` (worktree `t1-codex-core`). Not merged; the operator
reviews and merges.

Verification: `pytest -q` 1132 passed / 1 skipped, `python -m compileall -q bin
pneu_packaging scripts tests`, and `python scripts/check_public_safety.py` all
green.

## What shipped

### 1. Generic capability resolver

`bin/_rtcapability.py` is the new shared resolver. When a fenced tool finds no
ambient `RT_*` values it resolves native `CODEX_THREAD_ID` -> exact thread
binding -> live lease -> seat-capability record, and revalidates that whole
chain on every call. `_rtlib.authenticate_fenced_sender` consults it before
refusing, so `rt-ack --fenced`, `rt-say --fenced`, and the `--expect-reply`
seat-state write all work from a daemon-executed tool process. The resolved
fence is published into that one process only; nothing is written into the
daemon's environment or a thread config, so a lease rotation is visible
immediately instead of surviving in a snapshot.

Fail-closed triggers: superseded or stale lease, changed `bindingRevision`,
project/cwd mismatch, a binding without lease identity, and any thread that is
not the exact bound thread. `resolve_ambient_capability` gives Claude and
Hermes seats the same shape through their own transport.

The existing lifecycle skeleton was extended, not redone:
`_rtlauncher.claim_launch_seat`, `arm_codex_launch_intent`,
`rt-codex-session-start`, and `rt-codex-wake`'s validate-then-bind path are
unchanged in structure.

### 2. Seat-capability record

`capability.json` sits beside the lease in the seat's private runtime
directory (`_rtruntime.record_seat_capability` / `read_seat_capability` /
`clear_seat_capability`). It carries
`threadId` + `bindingRevision` + `roundtableSessionId` + `leaseRevision` plus
at most a minimal surface: `kind` and an explicit `pane`/`target`, optionally
`workspace`, `tab`, `session`, `endpoint`. Every other key is rejected, so no
environment, `HOME`, `PATH`, or token can reach the record. Stage 1 writes it
at launch (the launcher is the only process that can observe an ambient Herdr
or tmux surface truthfully); stage 2 adds the thread association at bind time
under the same lease fence. A record from an older lease generation is never
inherited, and `release()` drops the record with the lease. `surface.json` is
untouched and stays advisory.

`bin/rt-surface` (`show` / `probe` / `run -- ARGS`) is the consumer: it
resolves the capability, then addresses the recorded pane explicitly. The
`{surface}` placeholder is mandatory in `run`, and `--current`/`--self` are
refused, so an ambient address cannot slip in. When the calling process is not
genuinely inside a Herdr pane, the command is delegated to an operator-provided
`RT_HERDR_BROKER` executable that is; there is no path in the tree that writes
`HERDR_ENV`, and a test enforces that. A vanished pane fails closed with the
CLI's own diagnostic attached.

### 3. Lifecycle rules

Resume keeps the binding and `clear` still migrates only through a new
SessionStart/CAS under the same active lease (unchanged). `/btw` side threads,
forks, and sub-agent threads now inherit nothing at three layers: the
SessionStart hook declines on explicit fork/ephemeral evidence, the wake bridge
refuses them authoritatively via `thread/read`, and the resolver refuses any
thread that is not the exact bound one. Supersession, binding-revision change,
project mismatch, and a missing pane all fail closed immediately.

### 4. Bridge hygiene (D16-2/3/4/8)

- The SessionStart hook skips a payload carrying `parent_thread_id`,
  `forked_from_id`, `source_thread_id` (either spelling) or `ephemeral: true`,
  and traces the skip.
- The drain path classifies an ephemeral/fork thread as `EphemeralThread`,
  logs `auto_bind_skipped_ephemeral` with project and thread id, removes the
  dead queue entry, and **hands the launch intent back** so the real root
  thread can still bind. That last part is the fix for the observed failure
  where a `/btw` child's SessionStart stranded a launch.
- `auto_bind_rejected` now carries `project` and `thread_id`.
- Manual bind logs `manual_bind`; every binding removal goes through
  `remove_binding`, which logs `binding_removed` (project, thread, revision,
  reason) and clears the seat-capability record.
- `rt-projects rm` releases the Codex binding the way `worktree remove`
  already did; a failure is advisory, never a failed tombstone.
- Bindings record `projectUuid` + `projectRegisteredAt`, and the launcher's
  "(bound thread)" path discards a binding whose registration generation no
  longer matches. A binding written before those fields existed stays usable.

### 5. Launch feedback (D16-7)

`rt-codex` narrates the silent window on a TTY: checking the app-server
service -> claiming the project seat -> binding thread to seat -> priming the
seat. Non-TTY output is byte-identical to before.

### 6. Canonical daemon productization

- `pneu setup apply` (codex section) sets
  `CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` in the launchd user domain and owns
  `com.roundtable.codex-daemon-join`, a `RunAtLoad` agent that re-applies it at
  login because `launchctl setenv` does not survive logout. Both appear in the
  preview and the ownership manifest.
- Removal unsets the variable **before** any managed job is booted out or any
  plist removed, in every removal path, and a real invocation is reported as an
  external change rather than a rollback-able write.
- The managed app-server plist now requests `NumberOfFiles: 4096`. Measured on
  this host, the daemon held 136 open files against the 256 session default —
  the same exposure that produced `Too many open files` during the demo shoot.
- `rt-doctor` gains `codex-hosts` (host census plus the plain machine-wide
  trust-domain statement), `codex-daemon-join` (drift probe: switch set while a
  private stdio host still runs = Desktop did not join), and `codex-headroom`
  (open descriptors against the effective limit, warning at 70%).
- Desktop is never patched; no wrapper, alias, or PATH shim goes near `codex`.

## Judgment calls worth the operator's attention

1. **Hook-side child detection is evidence-based, not inferred.** I did not
   use "absent transcript path" as an ephemerality signal: I could not verify
   that field's presence semantics for non-ephemeral threads, and a false
   positive there would break every auto-bind. The hook skips only on explicit
   fork/ephemeral fields; the bridge, which reads authoritative `ephemeral` and
   `parentThreadId` from `thread/read`, remains the authority and now also
   releases the intent so nothing is stranded either way.

2. **"No bind requests consumed for them."** For an ephemeral thread the
   bridge removes the dead queue entry rather than leaving it. Leaving it would
   retry a permanent refusal at poll cadence (the 29 MB single-line log failure
   mode) and keep the lease's single request slot occupied against the real
   thread. What the child does not consume is the *seat's binding opportunity*:
   the launch intent is handed back. If you meant the literal queue entry, say
   so and I will invert it.

3. **`rt-surface` is a new command.** Stage 2 requires a seat to actually drive
   its recorded surface, and no existing tool was the right home (`rt-resolve`
   is cmux-legacy and requires a cmux runtime). It is packaged like every other
   tool and documented in the skill's tool table.

4. **Setup applies the switch only when it announces it.** A re-apply does not
   silently re-set a variable the user unset by hand; that is drift, which
   doctor reports and the login agent repairs. Doctor's fix line names both
   remedies.

5. **`_rtcapability.py` had to be added to four packaging lists.** Worth
   noting because a missed entry would have shipped a release whose every
   fenced Codex tool fails at import.

## Acceptance status

- **Stage 1 (resolver fenced ops)** — covered end to end by
  `tests/test_capability_resolver.py`: real `rt-say`/`rt-ack` subprocesses whose
  environment contains only `CODEX_THREAD_ID` (plus runtime/registry paths)
  succeed against the exact active lease and write a reply alarm; superseded
  lease, changed binding revision, project mismatch, legacy binding, and a
  `/btw` child all refuse; two seats in different projects never cross. The
  remaining leg is the "stock release artifact + live daemon" run, which needs
  a real Codex daemon and is an operator live gate like every other support
  claim in this repo.
- **Stage 2 (surface capability)** — covered by
  `tests/test_surface_capability.py` and the `rt-surface` end-to-end tests:
  explicit addressing, broker delegation, refusal without a broker, a
  fail-closed vanished pane, and a tree-wide assertion that nothing fabricates
  `HERDR_ENV`. A live Herdr pane run is an operator check.
- **Stage 3 (client equivalence)** — covered by
  `test_client_origin_does_not_change_the_resolved_seat`: the resolver has no
  notion of client origin, so a Desktop-driven bound thread resolves to the
  same seat with the same fences, while a `/btw` child of that thread resolves
  to nothing. The live Desktop-joined soak stays with the operator.
- **Promotion gate** — unknown 1 (phone reachability after the switch) was
  already resolved empirically on this host. Unknown 2 (Desktop behavior while
  the daemon is unavailable) is **unverified**; `docs/compatibility.md` records
  the exact test procedure and `rt-doctor` states plainly that the behavior is
  not established. Running it means booting out the daemon and reopening
  Desktop, which disrupts live sessions, so it is left to the operator.

## Constraints honored

No Desktop patching, no daemon restart, no bare bridge stop, writer lock
untouched, `MARKER_BLOCKS` in `roundtable-init` byte-identical, English-only
artifacts, no personal absolute paths or session URLs, commits on this branch
only.
