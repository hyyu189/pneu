> Provenance: Claude review artifact imported from its isolated scratchpad
> on 2026-07-29 at the final handoff request. Machine-local absolute paths
> are replaced with `<project-root>`, `<competition-worktree>`,
> `<user-home>`, and `<scratch-root>`; findings and attribution are otherwise
> preserved. This record captures the initial 1caff3b rejection. Its P0/P1
> findings were fixed before Claude's final ACCEPT of e13bd69.
# Acceptance record — v1/central-mail, 1caff3b (M1 + M2)

> Status: historical record — M1/M2 acceptance round

**Verdict: REJECT.** Two P0s. Both are small, local, and in the two predicates the milestone is *about*; neither is a design problem. This is a re-submit, not a redesign — everything else in the diff that I could reach verified sound, including the UUID machinery itself, the move-CAS, the worktree-minting claim, the single-resolver routing claim, and all three timing hazards the author named.

The milestone fails on both acceptance tests: it is **not correct in what it claims** (a documented command silently mints a fresh identity, which is the exact failure M1 exists to replace), and it is **not safe to build on** (one bad row in the registry fails all messaging closed for every project on the host, with every recovery command also refused — a regression against 46bc9e9, one `mv` away on this machine).

Environment: `666 passed in 86.50s`, matching the claim. Every scenario below ran against scratch registries under `RT_PROJECTS_FILE` in my scratchpad. Live registry mtime is still `Jul 28 17:24:02 2026`, `~/.roundtable/layout-locks` was never created, working tree clean at `1caff3b`. No product code was edited.

---

## What I re-ran rather than accepted

I reproduced, from scratch fixtures, the load-bearing claims of all four lenses: the registry-wide abort (both a git and a **new non-git** trigger), the silent mint via the printed remediation, the upgrade-tombstone mint, the squatter wedge, the v1-registry day-one behaviour, the doctor coverage loss (old-vs-new binary comparison), the unbounded hold inside the layout lock, the absence of writer preference, the pre-authorization lock file, and the git fan-out numbers. Findings I did *not* re-run are marked as resting on a single lens's output.

---

## P0 — must fix before M1+M2 is accepted

### P0-1. One bad registry row fails all mailbox access closed for every project, and every recovery verb is refused

Reported independently by the Identity lens and the Regression lens. **Reproduced here, plus a second trigger neither lens found.**

Three registered scratch projects: `repo` (git), `wt` (linked worktree of it), `plain` (unrelated, non-git). Baseline works. Then rename the parent — the operation §1 of the design celebrates as now-safe:

```
$ mv .../lab/repo .../lab/repo2
$ cd .../lab/plain && rt-say codex fyi "after rename"
rt-say: mailbox access failed: cannot derive Git sibling group for .../lab/wt; skipped     exit=1
$ cd .../lab/plain && rt-inbox
rt-inbox: mailbox access failed: cannot derive Git sibling group for .../lab/wt; skipped   exit=1
$ cd .../lab/plain && rt-ack <id>
rt-ack: mailbox access failed: cannot derive Git sibling group for .../lab/wt; skipped     exit=1
```

Every documented recovery is refused, including on the healthy project:

```
rt-projects rm  .../lab/wt     : project removal failed: cannot verify Git worktree for .../wt/.roundtable/project.json
rt-projects rm  .../lab/plain  : project removal failed: cannot derive Git sibling group for .../lab/wt; skipped
rt-projects add .../lab/newp   : project registration failed: cannot derive Git sibling group for .../lab/wt; skipped
```

`rt-projects list` does not even print the offending row, so the operator cannot read the UUID they would need to hand-edit. Restoring the parent path heals it instantly (`sent maildir-only 20260729T111727Z-...`), which is the only in-product escape and is not an in-product command.

**Second trigger, no git involved** — a project relocated behind a symlink at its registered path. Four healthy non-git projects, move one and symlink it back:

```
$ cd .../Code/one && rt-say codex fyi hi
rt-say: mailbox access failed: .../projects.yaml: projects[1] path is not canonical:
        .../Code/two (resolves to .../store/two); skipped
rt-projects rm .../Code/two   : refusing symbolic-link project root
rt-projects rm .../store/two  : project removal failed: ...path is not canonical...; skipped
rt-projects rm .../Code/one   : project removal failed: ...path is not canonical...; skipped
```

So this is not a git-worktree edge case. It is a general property: **any non-orphan structural warning on any row aborts the whole file for every consumer and every mutator.**

Control against the pre-change baseline (`git archive 46bc9e9 bin` extracted to scratch), same poisoned registry, same project:

```
$ oldbin/bin/rt-say codex fyi "old-code-probe"
sent maildir-only 20260729T112426Z-claude-to-codex-71560     exit=0
$ oldbin/bin/rt-inbox
20260729T112426Z-claude-to-codex-71560  claude  fyi  new  [maildir]  old-code-probe    exit=0
```

Old code was per-entry independent. This is a new coupling.

`rt-doctor` contradicts the runtime and misdirects the operator, both confirmed in the same run:

```
WARN registry: cannot derive Git sibling group for .../lab/wt; skipped
OK   registry: uuid=58b4e0c9-... path=.../lab/plain layout=local registered_at=...
WARN mailbox-resolver: cannot derive Git sibling group for .../lab/wt; skipped
  fix: repair project registration for .../lab/plain          <-- names the HEALTHY project
```

Control flow, all `bin/_rtlib.py`:

- `:968-973` `_parse_v2_entries` calls `_derive_project_group(root, uuid)` for **every** active+available row, not only the row being resolved.
- `:1765-1768` `_derive_project_group` raises when `git rev-parse --git-common-dir` fails and `_looks_like_git_worktree(root)` holds.
- `:995-996` caught per row, appended as `f"{error}; skipped"` — the word "skipped" is false; the operation aborts.
- `:473-478` `_structural_registry_warnings` exempts only the `orphan:` prefix.
- `:1021-1023` `_strict_entries_from_document` joins and re-raises.
- Single gate under `resolve_project_mailbox_checked` (`:2332`), `register_project` (`:2008`), `unregister_project`, and `_reindex_project_identity` (`:2245`).

`_canonical_registered_path` (`:836-839`) is the second producer of a structural, non-orphan warning; deliberate orphans are correctly carved out and *do* isolate.

Test-coverage gap: `tests/test_mailbox_resolver.py:790-808` exercises exactly this predicate but only on a single-project registry. Nothing pairs a healthy project with a sibling row broken by an ordinary filesystem operation. That is why 666 are green.

**Live blast radius, confirmed read-only.** `~/Code/roundtable-product/.git` contains `gitdir: <competition-worktree>/.git/worktrees/roundtable-product`, and `~/.roundtable/projects.yaml` registers both `<competition-worktree>` (line 21) and `<project-root>` (line 23). Renaming or archiving the parent kills messaging for all 15 registered projects, including the three running seats, with no in-product recovery. This is the 2026-07-24 `inspect_host_harness_seats` failure shape the design's own §5 says must not reach the mail path: *"Central mail stays out of any fail-closed host-service enumeration."*

### P0-2. `rt-projects add` — the remediation the product itself prints — silently mints a fresh identity

Reported by the Consumers lens. **Reproduced here.** Two ordinary operator actions: rename a project folder while no seat is open, then lose the marker (`.roundtable/project.json` is in `templates/roundtable-gitignore.tmpl`, so `git clean -xdf` removes it).

```
STEP 1 register                 marker: {"schema":"roundtable.project.v1","uuid":"06b7c054-..."}
STEP 2 mv p1 -> p2              (registry row still records .../p1)
STEP 3 rm .roundtable/project.json
STEP 4 rt-inbox
   rt-inbox: mailbox access failed: project is not registered (missing .../p2/.roundtable/project.json);
             run rt-projects add .../p2                                          exit=1
STEP 5 operator runs exactly that
   added .../p2                                                                  exit=0
STEP 6 registry:
   - uuid: 06b7c054-e4e2-4503-b77d-4e2451cef5f2   path: .../p1   status: active
   - uuid: f6847630-d23b-4867-a8cf-4b142f352264   path: .../p2   status: active
STEP 7 marker now: {"schema":"roundtable.project.v1","uuid":"f6847630-..."}
```

Exit 0, no warning, two active rows, original UUID orphaned, new identity minted over a project that already had one.

**Root cause, and it is one line.** `bin/_rtlib.py:2011-2015` builds `active_at_root` by *exact path equality* (`entry["root"] == root`). The "refusing path-based identity inheritance" guard at `:2020-2027` — which is correct and which I confirmed fires — lives **inside** `if active_at_root:`. When the recorded path has drifted, that list is empty, `marker_uuid` is `None`, and control falls straight through to `:2059-2060` `project_uuid = marker_uuid or _mint_project_uuid(used)`. Nothing consults the index for an active row whose recorded path no longer exists.

This also resolves the apparent contradiction between lenses (see below): the author's *named* case — marker deleted, path unchanged — passes. The compound case fails. "Never silently mint" carries no path qualifier.

Consequence today is bounded because every row is `layout=local`, so mail stays in-tree: the damage is a wrong UUID, a duplicate active row, a changed derived group, and a broken audit trail. Under the v1 `layout=central` target, `mail/<uuid>/` is keyed by that UUID (`:2216`), so the project returns attached to an empty mailbox while its history is orphaned under a UUID §5 says is never deleted automatically — silent loss rather than loud.

---

## P1 — must fix before v1 ships

### P1-1. The identity marker's remediation names no UUID and misstates the condition

`bin/_rtlib.py:1411-1415` raises `project is not registered (missing {marker}); run rt-projects add {root}`. The project *is* registered; `rt-projects list` prints its UUID in the same state. The author's stated bar was "remediation naming the UUID from the index." It does not, and this is the sentence that walks the operator into P0-2. Fixing the wording and the `active_at_root` predicate together closes both.

### P1-2. Day one on this host: the live registry is v1, so all mail fails closed with a two-hop dead-end remediation

Live: `head -1 ~/.roundtable/projects.yaml` → `schema: roundtable.projects.v1`, 15 rows. Reproduced on a v1 scratch fixture:

```
rt-inbox : mailbox access failed: project is not registered (missing .../project.json); run rt-projects add ...   exit=1
rt-say   : (identical)                                                                                            exit=1
rt-projects add ... : project registration failed: ... uses legacy schema; run rt-projects --registry ... upgrade  exit=1
registry sha unchanged
```

Fail-closed and non-mutating, so this is a diagnostic defect, not a safety one — but the first hop names the wrong verb, and only `rt-doctor`'s separate `registry-legacy` line carries the correct instruction.

### P1-3. `rt-projects upgrade` tombstones merely-unavailable rows, and the follow-up `add` mints over them

**Reproduced here**, and it contradicts the design's own §2: *"absence alone never silently changes durable registry state."*

```
$ mv offline/.roundtable/agents.yaml aside     # unmounted volume, momentary rename, transient FS error
$ rt-projects upgrade                          exit=0
  - uuid: c12ed405-...  path: .../offline  status: tombstoned  tombstoned_at: '2026-07-29T11:18:25Z'
$ (restore agents.yaml — the project was never removed)
$ rt-inbox   -> "project is not registered ...; run rt-projects add .../offline"
$ rt-projects add .../offline                  exit=0
  - uuid: c12ed405-...  path: .../offline  status: tombstoned
  - uuid: e0993ced-...  path: .../offline  status: active        <-- two rows, one path
  marker: {"schema":"roundtable.project.v1","uuid":"e0993ced-..."}
```

`bin/_rtlib.py:1937-1943` writes `status_value="tombstoned"` for anything not resolvable at that instant; `:2011-2015` never consults a tombstoned row at the same path. Shares its fix with P0-2. Honest caveat: all 15 live rows are currently available, so this would not fire on this host today — but the upgrade of the live registry has not been run and is the M1 adoption path.

### P1-4. A squatter directory containing only `agents.yaml` wedges a moved project's identity, and the CLI escape destroys it

**Reproduced here, with a correction to the Identity lens.** After the supported rename, drop a directory holding only `agents.yaml` at the old name:

```
rt-say                    : mailbox access failed: project identity a3926a43-... is already active at .../app;
                            refusing copied identity at .../app-v2
rt-projects rm  app-v2    : project identity a3926a43-... is active at .../app, not .../app-v2
rt-projects add app-v2    : project UUID a3926a43-... is already active at .../app; refusing copied identity
rt-projects rm  app       : tombstoned .../app          <-- tombstones the REAL project's UUID
rt-projects add app-v2    : added                        marker rewritten a3926a43-... -> 65868a5b-...
```

The message is untrue: the squatter holds no identity. `_reindex_project_identity:2256-2266` and `unregister_project` gate on `entry["available"]`, i.e. `is_project_root(root)` — "does `agents.yaml` exist there", never "does that path still hold this UUID".

**Correction to the lens:** the state is *not* unrecoverable. Removing the squatter directory heals it with the identity intact — verified:

```
$ rm -rf .../app && rt-say codex fyi hi
sent maildir-only 20260729T112205Z-claude-to-codex-63708     exit=0
marker preserved: {"schema":"roundtable.project.v1","uuid":"809c255b-..."}
row: path: .../app-v2                                          # CAS reindexed cleanly
```

So the defect is a misleading diagnostic that steers the operator into a destructive CLI recovery, not permanent identity loss. Downgraded from the lens's P1-as-unrecoverable to P1-as-misdirection.

### P1-5. `timeout` bounds only the flock wait, not the lock hold — a shared holder can block indefinitely and permanently exclude the migrator

**Reproduced here.** `_ProjectMailboxLock.__enter__` bounds the poll loop at `:341-363`, then at `:386` calls `resolve_project_mailbox_checked` **while holding the lock**. On path drift that reaches `_reindex_project_identity` → `_update_project_registry` → `:1220` `fcntl.flock(lock.fileno(), fcntl.LOCK_EX)` — no `LOCK_NB`, no deadline.

```
# registry lock pinned LOCK_EX by another process; project has been moved
$ rt-inbox                       # advertised layout bound: 10.0s
  STILL RUNNING after 25s -- layout bound of 10.0s exceeded
# control: same registry lock held, project NOT moved
  ctl rt-inbox exit=0 elapsed=0s
```

And the stalled consumer is holding the shared layout lock the whole time, so exclusion fails at the one job it exists for:

```
$ lsof .../layout-locks/74547c74-....lock
python3.1 60965 <user> 6u REG 1,14 0 275431159 .../74547c74-....lock
$ (migrator, exclusive=True, timeout=5.0)
  ProjectLayoutLockTimeout after 5.015s: timed out waiting for exclusive layout lock ...
```

Every caller's advertised bound is therefore false: `rt-doctor` 0.1s, `rt-stop-gate` 1.0s, `rt-codex-wake` 1.0s, `rt-wait-inbox` 1.0s, and the 10.0s default. The trigger is the first mailbox access after a project move — the headline new capability.

The Locking lens reports a second reachable un-timed inner lock on the ordinary `rt-say --fenced` path (`rt-say:555` → `require_fenced_seat` → `_rtlib.py:512` → `_rtruntime.py:1175` → `:340` `flock(..., LOCK_SH)`, no deadline), observed as a fenced `rt-say` still holding the layout lock at 14s with the exclusive waiter blocked. I did not independently reproduce that one; it rests on that lens's `lsof` output and the traced call chain, and the chain reads correctly.

### P1-6. No writer preference: overlapping shared holders can starve the exclusive lock indefinitely

**Reproduced here.** The poll loop uses `LOCK_NB` (`:338-349`), so a pending exclusive request does not fence off new shared acquisitions:

```
SH held -> EX waiter (bound 9s) starts -> a NEW shared acquirer arrives:
  NEW SH ACQUIRED after 1.787s (while an EX request is pending)
  EX waiter: ProjectLayoutLockTimeout after 5.017s
```

The Locking lens showed the handoff is guaranteed in practice, not merely possible: `rt-ack:212` holds the layout lock across `rt-ack:176`'s child `rt-say`, which takes its own (both PIDs on the same lock inode via `lsof`), and replaying that pattern starved an exclusive waiter through 248 handoffs. That half rests on that lens's output. Note the trap it flags: adding writer preference without first taking `rt-ack` out from around its child `rt-say` converts starvation into deadlock.

### P1-7. `rt-doctor` silently drops seat, tripwire and hook-trust coverage on a v1 registry while printing OK

**Reproduced here with an old-vs-new comparison on one fixture** (a project with claude+codex agents and a planted `.roundtable/inbox/codex/.armed-legacy`):

```
OLD (46bc9e9):
  OK   registry: .../proj registered_at=2026-01-01T00:00:00Z
  WARN seat: ... agent=claude ... status=vacant
  WARN seat: ... agent=codex  ... status=vacant
  WARN legacy-tripwire-marker: .../inbox/codex/.armed-legacy is not an authoritative session or watcher record
    fix: remove legacy marker ...
  OK   hook-trust: project=.../proj has 13 enabled hook(s), all managed or trusted

NEW (1caff3b), same registry, same cwd:
  WARN registry: ... uses legacy schema roundtable.projects.v1; run rt-projects upgrade before mailbox operations
  WARN registry: no active available projects in ...
  WARN registry-legacy: path=.../proj has no durable project UUID
    fix: run rt-projects --registry ... upgrade
  OK   hook-trust: no configured Codex projects to check
  — zero seat lines, zero tripwire lines, .armed-legacy still on disk
```

Cause: `bin/rt-doctor:406` and `:1128` now filter through `active_project_entries`, which drops rows carrying `legacy: True` (`_rtlib.py:1040-1047`) — 100% of rows on a v1 registry. On exactly the host state that ships today, two of three lost checks degrade to an affirmative `OK`, one of them security-adjacent.

### P1-8. Every mailbox operation forks one `git` per registered project

**Reproduced here.** Instrumented `subprocess.run` around one `resolve_project_mailbox_checked`:

```
rows=12   git spawns for ONE resolve=15   wall=141ms
rows=1    git spawns for ONE resolve=4    wall=43ms
```

Three probes are for the project being resolved; the remaining N-1 are `_derive_project_group` walking every other active+available row (`:968-973`). This is the hot path for `rt-say`, `rt-inbox`, `rt-ack`, `rt-stop-gate`, `rt-wait-inbox`, and §7 requires watchers to re-resolve under a fresh shared section every scan — the Regression lens measured the Hermes follower at 18 spawns per 0.25s tick, ~72 git processes/second sustained, with 166ms of work per 250ms of duty cycle at 12 rows. Each spawn carries `timeout=5.0` (`:1741`), so one stalled mount serialises up to 15×5s per load on this host. Beyond cost, this fan-out is the mechanism of P0-1.

---

## P2 — worth recording

- **`rt-stop-gate` does not classify `ProjectLayoutLockTimeout`.** Confirmed by inspection: mention counts are `rt-wait-inbox` 5, `rt-codex-wake` 3, `rt-doctor` 2, **`rt-stop-gate` 0**. It asks for `timeout=1.0` at `:133` — a bound chosen so the timeout can be classified — then catches the parent `ProjectRegistryError` and returns 2, blocking the Stop with a raw lock path. `rt-doctor` emits `WARN mailbox-layout-busy` with a retry hint for the identical condition; `rt-codex-wake` returns `heartbeat=True`. Three consumers, three contracts. Blast radius limited: this host's live settings run `rt-wait-inbox --claude-stop-hook`, and `roundtable_packaging/setup.py:762-777` lists the `rt-stop-gate` Stop hook under `_legacy_claude_groups` — but it is still shipped and still symlinked.
- **`rt-wait-inbox` retries its startup mailbox snapshot on layout contention with no cap.** `bin/rt-wait-inbox:334-347` is `while True` / `sleep(0.25)` with no deadline, no attempt cap, no output; same shape at `:258-271`. The comment at `:343` asserts *"Migration holds EX only for its bounded cutover"* — nothing in the tree enforces that, and §7 step 5 has the migrator copying `inbox/` and `messages/` under EX, a hold proportional to mailbox size. See the contradiction resolution below.
- **`rt-doctor`'s `mailbox-resolver` fix names the healthy project.** Observed in my P0 run. `bin/rt-doctor:991` formats the fix with the loop variable while `str(error)` names the row that failed. During a P0-1 outage the one surviving diagnostic points at a project that is not broken.
- **`rm` of a layout lock file silently voids exclusion**, and the bad-mode error (`invalid layout lock ownership/type/mode`) carries no remediation, unlike the directory case which suggests `chmod 700`. `~/.roundtable` is a git checkout whose `.gitignore` does not cover `layout-locks/`. Rests on the Locking lens's output; the mechanism follows from `_verify_layout_lock_entry` running only at acquisition.
- **The layout lock is keyed to the registry directory, not the mailbox.** `:2235` `layout_lock = registry.parent / "layout-locks" / f"{uuid}.lock"` while `layout == "local"` puts the mail root at `state_dir` (`:2212-2214`). Two `RT_PROJECTS_FILE` spellings give one local mailbox two lock namespaces; the Locking lens drove two simultaneous exclusive holders of the same mailbox that way. Same-uid only, and it disappears under `layout=central` — but it bites precisely at the state the migrator reads *from*.
- **The lock file is created before authorization.** **Reproduced here:** an unregistered alien marker fails closed (`project identity bc2dcd35-... has 0 registry entries`) and still leaves `bc2dcd35-....lock` in a namespace the design says is never deleted. `_open_layout_lock` runs at `:337`, `resolve_project_mailbox_checked` at `:386`. Cheap fix: resolve first, open second.
- **`~/.roundtable/layout-locks/` is a new permanent host artifact no uninstall path removes.** `roundtable_packaging/cli.py:957-972` `--purge-runtime` covers only `prefix/.runtime`; no occurrence of `layout-locks` in `roundtable_packaging/`, `scripts/`, or `docs/install.md`. Confirmed the directory does not yet exist on this host.
- **The reviewed spec was amended by the commits under test.** Confirmed: `git diff 46bc9e9..1caff3b --stat -- handoff/archive/design-v1-central-mail.md` → 54 insertions, 9 deletions, authored by `83078cb` and `1caff3b`, in a file headed *"Status: converged. Reviewed by Codex and Hermes."* Added by the implementation: the `project.json` witness paragraph, the `roundtable.projects.v2` schema paragraph, the entire §7 layout-lock and lock-ordering rule that M2 is supposed to satisfy, the maildir shape change, and three names in the single-resolver list. For those 54 lines, checking implementation against spec is checking the change against itself. Recommend Codex/Hermes re-review of the amended sections before they are treated as converged.
- **`rt-refresh` now requires project registration to write cmux `runtime.json`**, and fails after every cmux round trip. `runtime.json` is topology state the design explicitly keeps out of the mail plane. Rests on the Regression lens's reproduction of `_commit_runtime`'s call, not an end-to-end `rt-refresh` run.

---

## Discarded, downgraded, or re-scoped

- **Discarded — the `safe.directory` / foreign-uid trigger for P0-1** (Identity lens). Self-declared as inferred, unreachable same-uid because `_clean_git_environment` strips `GIT_CONFIG_*`. P0-1 does not need it: I have two observed triggers, one of them non-git.
- **Discarded — the live-inode alias structural error** (`:958-965`). Both a lens and I could not construct it; `register_project:2036-2054` reindexes onto the alias spelling rather than adding a second row. Not a finding.
- **Discarded — "resident old `rt-codex-wake` keeps heartbeating with zero projects after upgrade"** (Regression lens). Explicitly traced, not executed. Recorded below as untested, not as a defect.
- **Downgraded — "the squatter wedge is unrecoverable"** → P1-4 as stated. I reproduced a non-destructive recovery the lens missed.
- **Re-scoped — the central-layout consequences of P0-2 / P1-3.** The Identity lens derived them from reading `:2216`; the Consumers lens observed an empty `rt-inbox` and a `WARN registry-orphan` against a **hand-built** central tree. Neither is evidence about a migrator. I state the consequence as established for the resolver, not for migration.
- **Not a finding — "adding writer preference would deadlock `rt-ack`"** (Locking lens). Reasoning about a hypothetical fix. Retained above as a constraint on the fix, not as a defect.

---

## Contradictions between lenses, resolved against the code

1. **"The six-state fail-closed matrix never mints" (Identity) vs "`rt-projects add` mints" (Consumers, Regression).** Both are correct and the resolution is one line. The Identity lens held the recorded path *fixed*, so `active_at_root` (`:2011-2015`) was non-empty and the guard at `:2020-2027` fired — I confirmed that branch. The other two lenses moved the directory first, so `active_at_root` was empty and control fell through to `:2059-2060`. The guard is conditioned on exact path equality. That single predicate is the root cause of P0-2, P1-3 and P1-4, and it is where the fix belongs.

2. **`rt-wait-inbox`'s unbounded retry: `real=true` (Locking) vs `real=false` (Consumers).** Resolved by splitting the claim. The *silent uncapped retry loop* is a code fact — `bin/rt-wait-inbox:334-347`, `while True`, no cap, no output — and the Locking lens observed 20.2s of it with the seat reporting `active_unhealthy` and empty stdout/stderr. The *"spins forever"* consequence needs a hung-but-alive exclusive holder, which neither lens could construct because a crashed holder releases the flock at process death. Ranked P2 on the observed half. The Consumers lens was right to refuse the stronger claim; the Locking lens supplied the missing observation for the weaker one.

3. **"Missing marker is recoverable" (Consumers) vs "unrecoverable through every verb" (Identity).** Not a contradiction — different states. A *missing* marker recovers via `rt-projects rm` (exit 0, tombstone). A *zero-byte or truncated* marker is refused by every verb including `rt-projects rm`, which propagates the raw `json.JSONDecodeError` before any registry work (`unregister_project:2106-2111`). Both hold; the truncated case is the P2-grade recoverability gap.

---

## Verified sound — do not re-litigate these

- **Single-resolver routing (§2).** Confirmed by grep: the only literal `inbox`/`messages` joins in `bin/`, `integrations/`, `roundtable_packaging/` are `_rtlib.py:2214,2217,2222,2233` inside `mailbox_from_registry_entry`, plus `bin/roundtable:40` (a subcommand name). Every `INBOX_DIR`/`MSG_DIR`/`LOCK_DIR`/`LEDGER_LOCK` is assigned from a resolved `ProjectMailbox` (`rt-say:764-767`, `rt-inbox:563-564`, and the rest). The Hermes poller holds no path at all — it shells out to `rt-wait-inbox --wait-last-wake-drained`. The "second place that builds the path by hand" does not exist.
- **The UUID machinery itself.** Move CAS reindexes atomically and refuses a claimed target path. Worktree minting is true — in my own fixture `repo` and `wt` received distinct UUIDs (`7886de88`, `afb6aff3`) and the same derived group (`git:3e7e6a01...`). Copied live UUIDs and tombstoned UUIDs fail closed. Registration writes `.roundtable/.gitignore` and refuses a tracked or unignored marker. Deliberate orphans are correctly isolated and do not abort other projects — the `orphan:` carve-out works.
- **Tombstone durability.** The `load_project_registry` drop the design called out is fixed; tombstones survive.
- **Lock namespace and contention.** Disjoint from `projects.yaml.lock`, `<mail_root>/locks/`, and `~/.roundtable/.runtime/**`. Every substitution refused (0644 file, hardlink, symlink, directory, 0755 dir, symlinked dir). SH/EX contention works in both directions with a real `ProjectLayoutLockTimeout`; same-process upgrade fails bounded rather than deadlocking. No AB-BA cycle: all four `_update_project_registry` call sites were read and none acquires a layout lock, so the documented order holds.
- **All three timing hazards the author named.** The Hermes generation follower ignored a local drain and exited 0 only when the central copy drained across a live pointer flip. `ProjectLayoutLockTimeout` is provably not an `IdentityError`, so `rt-codex-wake:1915` takes the `heartbeat=True` branch and `heartbeat_bound_seats` really refreshes the lease. Eight trials of four queued Claude Stop hooks behind an exclusive holder produced exactly one winner and exactly one 1→2 attempt increment every time, with all four correctly reporting exhausted when pre-armed. These rest on the Consumers lens's output; I did not re-run them.
- **On-disk format and old/new interop.** `project.json` added to the gitignore template and to `roundtable-init`'s marked block; pre-change projects self-heal at registration. New `rt-say` and old `rt-inbox` read each other's mail identically under `layout=local`.

---

## What was NOT tested — read this as the boundary of the record

**Nothing about the actual migration.** `grep -rn "exclusive=True" bin/ integrations/ roundtable_packaging/ scripts/` returns nothing: no shipped code takes `LOCK_EX`, and nothing in the tree sets `layout=central`. Every exclusive-side result used a stand-in holder. Every central-layout result came from a hand-built tree or a direct `_update_project_registry` call. So: the migrator's copy/fsync/rename correctness, how long a real cutover holds EX, the rollback, resumption after interruption, and the post-cutover state are all **entirely unexercised**. The number that decides whether P1-5, P1-6 and the `rt-wait-inbox` P2 are cosmetic or fatal — real EX hold duration — does not exist yet.

**The live registry upgrade.** The 15-row v1 registry on this host was never upgraded. The v1→v2 upgrade was exercised only on 2-row scratch fixtures. The actual adoption path for this machine is untested.

**A genuinely stuck exclusive holder.** A crashed holder releases the flock at process death, so no lens could produce an indefinite hang. The unbounded loops are code facts plus bounded observations.

**Live credentialed harness paths.** No `WakeBridge.step` end-to-end (no app-server socket exists in an isolated `RT_RUNTIME_DIR`), no `rt-codex-wake run --once` exit path executed, no `rt-refresh` under real cmux topology, no clean-account repeat, and `roundtable-setup apply` was never run against a stale installation — so whether an upgrade actually replaces the legacy `rt-stop-gate` Stop hook on an installed host is unknown. Note also that `rt-doctor` probes the **real** `~/.codex` app-server socket even from a scratch runtime dir (fixed `DEFAULT_SOCKET`, `bin/_rtcodex.py:72`) — read-only and pre-existing, the diff does not touch that file, but it means a truly isolated Codex test is not available from a scratch directory at all.

**Filesystem assumptions.** All work ran on APFS under `<scratch-root>`. `flock` behaviour on network or FUSE mounts, where the unbounded waits in P1-5 would be considerably worse, is untested.

**Unreachable states.** The live-inode alias structural error and git's dubious-ownership refusal could not be constructed same-uid.

**Not in scope of this diff at all.** Cross-worktree `@name` addressing (§4), group revalidation at use, and §8 Codex thread preallocation are not implemented in `83078cb..1caff3b`. Mutation testing is running separately and is not part of this record.

---

## Live-state attestation

`~/.roundtable/projects.yaml` mtime `Jul 28 17:24:02 2026`, predating this session. `~/.roundtable/layout-locks` does not exist. No writes to `~/.roundtable`, `~/.claude`, `~/.codex`, `~/.hermes`, or any shell profile. No git write commands, no branch switch; `git status --porcelain` empty at `1caff3b`. `~/Code/roundtable-product`, `~/MoneyMarket-MacroFinance` and `~/quant` were read only — the first for its `.git` pointer, the registry for its schema and row paths. No product code was edited.

## Smallest path to ACCEPT

1. Make structural registry warnings **row-scoped**, not file-scoped, for the resolve path (`_strict_entries_from_document:1021-1023`) — or defer `_derive_project_group` so it runs only for the row being resolved, which fixes P0-1 and P1-8 together and is the more contained change.
2. Consult the index by marker-absence **and** by drifted path before `:2059-2060` mints, and make `_rtlib.py:1411-1415` name the index UUID. Fixes P0-2, P1-1, P1-3 and P1-4 at one predicate.
3. Add the two regression tests the 666 do not have: a healthy project plus a sibling row broken by an ordinary filesystem operation, and `add` after a rename with the marker gone.

Items 1 and 2 are the whole gap between this record and ACCEPT.
