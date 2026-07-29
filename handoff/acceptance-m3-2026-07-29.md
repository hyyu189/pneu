> Provenance: Claude review artifact imported from its isolated scratchpad
> on 2026-07-29 at the final handoff request. Machine-local absolute paths
> are replaced with `<project-root>`, `<competition-worktree>`,
> `<user-home>`, and `<scratch-root>`; findings and attribution are otherwise
> preserved. This record accepted M3 at 570a940/1815b34 with four pre-v1
> P1 follow-ups. Those follow-ups were fixed before Claude's final ACCEPT
> of e13bd69.
# Acceptance Record — v1/central-mail, milestone `570a940`

**Verdict: ACCEPT.** No P0. The destructive core is correct in what it claims, and the one claim the previous review called hardest to see — that rollback after cutover copies *current central* state back rather than restoring the pre-cutover backup — is implemented as specified and verified first-hand. Four P1s survive; all four are about recoverability, availability and install hygiene, none about data loss or layout ambiguity.

**Tree note.** HEAD moved to `1815b34` while I was reviewing. `git diff 570a940..HEAD --stat` is `handoff/design-v1-central-mail.md` + `handoff/upstream-codex-zero-turn-resume.md`, 2 files, docs only, and the change is entirely §8 (Codex preallocation deferred). §7 is untouched. Every product-code finding below was measured against code byte-identical to `570a940`.

---

## 1. Discarded — evidence was inference

| Discarded claim | Source | Why |
|---|---|---|
| **"A Claude Stop inside the exclusive window loses both the drain check and the tripwire check"** | R3, filed P1 | The observation is real and I reproduced it. The *impact* rests on an unverified premise: that `rt-stop-gate` is the Claude Stop hook. It is not. `roundtable_packaging/setup.py:753-760` (`_claude_groups`) installs `rt-wait-inbox --claude-stop-hook`; `rt-stop-gate` appears only in `_legacy_claude_groups`, whose own docstring reads *"Known setup-owned hook generations that may be upgraded in place."* The operator's live `~/.claude/settings.json` Stop block contains `rt-wait-inbox --claude-stop-hook` and zero occurrences of `rt-stop-gate`. Demoted to P2, reframed. |
| **"The iCloud backup root plausibly explains the author's 3–5x higher timings"** | R3, R4 | Both reports flagged the causal step as inferred. It is worse than untested: `~/Documents/Workspace/backups/roundtable-central-mail` **does not exist** on this host (`No such file or directory`), so no migration has ever used the default root here — including the author's. The hypothesis has no on-disk support. Domain membership survives as an observed fact and feeds P1-2. |
| **"A trivially small project can hold its layout lock past the consumers' 10s bound"** (registry-lock amplification) | R3, filed P1 | The nesting is observed and I confirmed it. The impact requires a multi-second registry-lock holder, which R3 produced only with a hand-crafted `flock` and explicitly conceded: *"I could not produce a natural multi-second registry-lock holder from product code."* The longest natural holder I measured is `registry_flip_ms: 1041.6` during a 14,000-file migration. Demoted to P2. |
| **"The live prefix is stale *because of* the same-version no-op"** | R4 | R4 flagged this itself. The mechanism is proven and the staleness is observed; the causal link between them is not. Mechanism retained (P1-4), causal claim dropped. |
| **Any power-loss / fsync-barrier durability claim** | all four | All four state that SIGKILL and `os._exit(9)` skip Python unwinding but not kernel writeback. Nobody claimed it; recorded in §5 as not established. |
| **"2 of 3 exclusive acquisitions timed out at the 10s bound at baseline"** | R3 | I ran the same A/B and got multi-second waits (5837 ms, 7193 ms) but **no outright timeout**. The conclusion R3 drew survives on my weaker numbers; the specific timeout figure does not reproduce and I do not carry it. |
| **Anything derived from `rt-codex-wake --once`** | R3 | R3 itself: *"grep finds no caller in bin/, scripts/ or roundtable_packaging/ — I could not reach that path from product code."* Correctly self-discarded. |

`real=false` fields were treated as hypotheses throughout; none of the four reports filed one.

---

## 2. Surviving findings, ranked

### P0 — blocks this milestone
**None.** R2 hunted post-cutover data loss specifically and reported the absence as observed, not assumed: *"every construction I tried preserved the mail, so the P0 I was hunting is absent rather than merely unobserved."* I reproduced the preservation independently (§3 below). No report produced a state where two layouts were simultaneously authoritative, where a consumer read both, or where acknowledged mail was lost.

### P1 — blocks v1

**P1-1. Post-cutover repair and rollback are both hostage to the backup bundle.**
`bin/_rtmigrate.py:1511` — `_verify_central_generation` calls `load_manifest(marker["manifest"], ...)` on the absolute path recorded in the central marker. Both the "already central" repair branch (`:2146`) and rollback (`:2513`) go through it. Reproduced independently (R1, R2, and me):

```
### 3. delete the backup bundle
### 4. rerun migrate (documented post-crash repair)
rt-projects: migrate failed: cannot resolve migration manifest .../manifest.json: [Errno 2] ...
### 5. rollback with the (now missing) manifest
rt-projects: rollback failed: cannot resolve migration manifest .../manifest.json: [Errno 2] ...
### 7. registry layout
  layout: central
```
Mail stayed fully readable throughout — all four messages including the post-cutover one. **No data is lost; recoverability is.** The coupling is gratuitous: rollback reconstructs from `scan_mail_tree(central)` and does not need the bundle's bytes, only its identity chain. Worst form, reproduced by R1 and R2: crash after the flip (half-migrated, local duplicate still on disk, no bookmark) *plus* a lost bundle leaves the project permanently unrepairable and unrollbackable.

**P1-2. The backup root is a hardcoded real path outside every isolation lever, and after `_cleanup_local_after_forward` it is the only independent copy of pre-cutover mail.**
`bin/_rtmigrate.py:48` — `DEFAULT_BACKUP_ROOT = Path.home()/"Documents"/"Workspace"/"backups"/"roundtable-central-mail"`, evaluated at import. Only `--backup-dir` overrides it; R4's grep over `bin/` and `roundtable_packaging/` found 24 `RT_*`/`ROUNDTABLE_*` variables and no backup variable — it is the only host-state root in the product that is not environment-redirectable. Observed by me read-only: `xattr -p com.apple.file-provider-domain-id ~/Documents` → `com.apple.CloudDocs.iCloudDriveFileProvider/<icloud-domain-id>`, and `~/Library/Mobile Documents/com~apple~CloudDocs/Documents` is a symlink to `<user-home>/Documents`. `_create_backup` runs inside the exclusive block. Uninstall preserves it forever in both modes (R4 verified; 103 MB accumulated across 7 bench migrations with no pruning). Together with P1-1 this is one recoverability story: the single copy that everything depends on lives in a user-managed, cloud-reachable, never-pruned directory that nothing declares load-bearing.

**P1-3. The exclusive hold is unbounded and linear in file count; past the consumers' fixed 10s bound a live send is rejected, not queued.**
`bin/_rtlib.py:35` — `LAYOUT_LOCK_TIMEOUT_SECONDS = 10.0`, used as the module default at `:355`, `:536`, `:553`; `rt-say`/`rt-inbox`/`rt-ack` expose no override. Reproduced by me at 14,000 files:

```
rt-say rc=1 elapsed=10077.6ms
rt-say: mailbox access failed: timed out waiting for shared layout lock .../layout-locks/<uuid>.lock
migrate: {"exclusive_hold_ms": 13636.973, "files": 14000, "copy_ms": 5032.5, "registry_flip_ms": 1041.6}
message landed anywhere? 0 location(s)
```
At 500 files (`exclusive_hold_ms: 719.1`) the same send returned rc=0 in 115.6 ms. Failure is fail-closed and loud: a rejected send, never a send the caller believed succeeded. But §5 of the spec states *"Purging is explicit. Nothing deletes message content automatically,"* and `rt-ack` moves `new/`→`cur/` (`bin/rt-ack:68`), so file count grows monotonically for the life of a seat. This degrades toward the cliff by design. See §4 for the settlement.

**P1-4. Same-version source reinstall is a silent no-op.**
`roundtable_packaging/cli.py:474-482` — the version directory is keyed on the VERSION string alone, and `_validate_version_dir` is passed `expected_project_wheel_sha256=None if source_mode`. Four commits of this milestone's history all carry `0.1.9`: `83078cb`, `1caff3b`, `0279174`, `78f1ccd`. Only `570a940` is `0.2.0`. Reproduced by me in an isolated prefix:

```
0279174 _rtlib has writer.lock: 0     78f1ccd _rtlib has writer.lock: 1
=== install 78f1ccd (also 0.1.9, WITH the turnstile) over the 0279174 prefix ===
installed Roundtable 0.1.9 at .../inst/prefix
installed _rtlib AFTER: writer.lock=0  sha=78201ff34b8362b2
(source 78f1ccd sha: 0709f10cff95bc5f, writer.lock=1)
```
The installer reported success and installed nothing. **Consequence for this milestone is provenance, not breakage:** anyone who hand-verified `78f1ccd` — the commit that introduces the turnstile §7 depends on — by re-running the source installer over an existing `0.1.9` prefix exercised pre-turnstile code. I confirmed the mitigation: installing HEAD (`0.2.0`) over the same prefix produces `versions/0.2.0` with `writer.lock=1` and `current -> versions/0.2.0`. R4 also verified the wheel path fails closed on the same input.

### P2 — recorded

1. **Migration debris accumulates silently.** `.<uuid>.prepare.<opid>/`, `.<uuid>.stale.<opid>/`, `.<uuid>.rolledback.<opid>/` in the mail root; `.central-mail-retired.<opid>/` and `.mail-rollback.<opid>/` in the project. I observed `.9e7ac5f1-….rolledback.e0e2e1e2-…/` and a surviving `.mail-rollback.json` after a clean rollback. Each is a full plaintext copy of the mailbox. `bin/rt-doctor` never enumerates the mail root; the milestone's own `.gitignore` addition (`mail`) plus the `.central-mail-*`/`.mail-rollback.*` block hides the project-side debris from `git status`. §5 promised doctor would report orphaned mailboxes; this milestone adds an orphan class doctor cannot see. (R1)
2. **Pointer=central with the central tree deleted has no product recovery,** and `rt-doctor`'s suggested fix ("repair project registration") is implemented by no command. R1 could not reach this by crashing the migrator and produced it by deletion — correctly labelled. (R1)
3. **A crashed post-flip rollback plus a deleted `.mail-rollback.json` wedges both commands,** and `git clean -xfd` now separates surviving central mail from the only witness of its UUID. Before this milestone the same clean destroyed mail and witness together, which was self-consistent. (R1)
4. **`_flip_layout` takes the global registry lock inside the per-project exclusive section** with its own separate 10s budget (`bin/_rtmigrate.py:2283` inside the block opened at `:2121`; `bin/_rtlib.py:1462`, `REGISTRY_LOCK_TIMEOUT_SECONDS = 10.0`). Composition is real; natural trigger unproven. Reverse direction is measurable: `registry_flip_ms` was 18 ms empty, 88 ms at 1000 files, **1042 ms at 14000** — one large migration holds the registry lock shared by all 15 rows for ~1 s.
5. **`rt-stop-gate` degrades badly under the new exclusive lock, but is not the shipped hook.** My side-by-side, 12 s exclusive hold, mail undrained:
   ```
   legacy  rt-stop-gate            rc=2 @1084ms  "timed out waiting for shared layout lock …"  (no mention of mail)
   legacy  rt-stop-gate  RETRY     rc=0 @63ms    (stop_hook_active=true → both checks skipped)
   shipped rt-wait-inbox stop-hook rc=2 @11746ms "…undrained mail: message-still-pending.md"
   ```
   The shipped hook waits the hold out and does its job. `bin/rt-stop-gate`, `bin/rt-wait-inbox` and `roundtable_packaging/setup.py` are all **untouched** by `0279174..570a940`. Latent risk only for an operator carrying a stale legacy Stop block who has not re-run `setup apply`.
6. **11–18% of the hold is post-cutover work.** R3 measured `_cleanup_local_after_forward` + `_install_bookmark` at 127.5 ms of a 1120 ms hold. Spec-compliant (step 7 says bookmark last) but the pointer already says central, so both are safe with the lock released.
7. **No preflight estimate, no warning, no quiesce guidance.** The hold is reported only after it completes. Nothing compares a projected hold against the consumers' fixed 10s bound. `rt-projects migrate --help` offers only `root`, `--backup-dir`, `--layout-lock-timeout`, `--registry-lock-timeout`. (R3, R4)
8. **Three unbounded blocking flocks remain** at `bin/_rtruntime.py:340`, `:385`, `bin/rt-codex-wake:168`. Outside the layout-lock order, cannot deadlock with migration, pre-existing. `78f1ccd` did fix the one that mattered — the previously untimed `LOCK_EX` in `_update_project_registry` reachable from inside the migrator's exclusive section. (R3)
9. **Install manifest's `preserved` list names prefix-relative registry paths** that do not exist under a non-default prefix (`cli.py:771-779` vs `_rtlib.py:643-650`). Cosmetic and pre-existing, but this milestone extends the same phrasing to mail and layout locks. (R4)
10. **`RT_MIGRATION_FAILPOINT` is a shipped, env-readable abort inside a destructive command** (`bin/_rtmigrate.py:136`), not covered by `scripts/check_public_safety.py`. I used it; it aborts into states the SIGKILL path already produces and the documented rerun repaired every one. Recorded because a shipped env-triggered abort in the one destructive command deserves an explicit intent decision.
11. **The backup bundle is verified-at-every-use, not immutable.** R2's 8/8 tamper vectors were all refused with state untouched, but every tamper was an ordinary same-UID write that succeeded. This matches §7's stated integrity domain (detection, not prevention) — the honest phrasing in any doc should be "verified at every use," never "immutable."

---

## 3. What I verified first-hand

- **Suite**: `742 passed in 107.53s`, exit 0. Baseline was 680.
- **§7 order in code**: exclusive lock (`_rtmigrate.py:2121`) → `_create_backup` → `_publish_central_generation` → `_flip_layout` → `_cleanup_local_after_forward` → `_install_bookmark`. Backup is created and verified before anything moves; the flip is the cutover; the bookmark is last.
- **The central claim, empirically.** Migrated a project holding one message, then made three *real* `rt-say` sends that existed only centrally, then rolled back:
  ```
  post-cutover strings present in FORWARD backup payload: 0   (expect 0)
  {'status': 'rolled back', 'files': 6, 'committed': True}
  layout now:   layout: local
  post-cutover messages recovered locally: 4
  INBOX IDENTICAL
  MESSAGES IDENTICAL
  ```
  `diff -r` of the pre-rollback central tree against the restored local tree is byte-clean, and `rt-inbox` lists all four messages afterwards. Mechanism: `_rtmigrate.py:2528 snapshot = scan_mail_tree(central)` → `:2534 _create_backup(source_root=central, direction="central-to-local")` → `:2559 _prepare_local_candidate(source_snapshot=snapshot)` → `:2588 _flip_layout(...,"central","local")`.
- **Crash at the cutover boundary.** `RT_MIGRATION_FAILPOINT=after_registry_flip`: `layout: central`, local tree still present with 3 files, no bookmark, and `rt-projects resolve` returns the **central** inbox only — one authoritative layout, the pointer alone deciding. The error names the fix (`rerun migrate to finish post-commit repair`); the rerun returned `status: 'already central'`, retired the local tree to 0 files, and installed the bookmark.
- **Timings, four shapes, median of 3, isolated 0700 backup root on APFS scratch:**

  | shape | mine | author | ratio |
  |---|---|---|---|
  | empty | **69.7 ms** | 416 ms | 6.0x |
  | 1000 files / 0.37 MB | **1014 ms** | 5838 ms (@0.51 MB) | 5.8x |
  | 4 files / 32 MiB | **288 ms** | 651 ms | 2.3x |
  | 253 files / 3.3 MB | **331 ms** | 888 ms | 2.7x |

  This is the fourth independent reproduction and the fourth to land 2–6x below the author. **Shape confirmed, magnitudes not.** Cost is per-file, not per-byte: 32 MiB in 4 files (288 ms) is cheaper than 3.3 MB in 253 files (331 ms) and 3.5x cheaper than 0.37 MB in 1000 files. My model `hold_ms ≈ 70 + 0.97·files` fits every point I took — 253 (1.03 ms/file), 500 (0.91), 1000 (0.94), 14000 (0.97). The author's own four points do **not** fit one: their 253-file point implies 1.87 ms/file, their 1000-file point 5.42 ms/file — a 2.9x internal inconsistency, with the higher-byte case being the *faster* one. Their 1000-file datapoint is the outlier, and it is the one that sets the pessimistic crossover.
- **Turnstile A/B**, baseline `_rtlib.py` materialised read-only via `git show 0279174:bin/_rtlib.py`:
  ```
  BASELINE-0279174  8 readers x 0.10s   writer_waits [203.1, 720.6, 5837.0] ms
  HEAD-570a940      8 readers x 0.10s   writer_waits [ 83.7, 210.8,  197.7] ms
  BASELINE-0279174 12 readers x 0.10s   writer_waits [7192.5, 43.8, 35.2] ms
  HEAD-570a940     12 readers x 0.10s   writer_waits [145.8, 145.5, 149.2] ms
  ```
  Baseline admission is unbounded and wildly variable, reaching 72% of the 10s bound in my runs. HEAD is deterministic at ~150 ms and does not degrade with reader count. Reader throughput is unchanged (416→421, 658→617 acquisitions).
- **Live blast radius is zero right now.** `~/.roundtable/projects.yaml`: `schema: roundtable.projects.v1`, 15 rows, mtime `Jul 28 17:24:02`, unchanged at the end of my work. `~/.roundtable/versions/0.1.9/bin/_rtlib.py` is dated `27 Jul 18:42` and contains `writer.lock=0`, `central_mail_root=0`, `PROJECT_LAYOUTS=0` — **none of this milestone's code is installed**, and the live registry is still the legacy schema that `_rtlib.py:1794` refuses to serve until an explicit backed-up upgrade. Nothing in this milestone can run against the operator's real mail without two deliberate steps. Live mailbox sizes, read-only: `roundtable-product` 63 files, `MoneyMarket-MacroFinance` 138, `quant` 19.
- `scripts/check_public_safety.py` → `public-safety check passed (109 tracked files, full reachable history)`.
- Rails honoured: everything under an isolated `RT_PROJECTS_FILE`/`RT_RUNTIME_DIR` and a scratch `ROUNDTABLE_INSTALL_PREFIX` in my own scratchpad; no product file edited; no git write; no branch switch; no installer or uninstaller against the live prefix; nothing written to `~/.roundtable`, `~/.claude`, `~/.codex`, `~/.hermes`, `~/Documents`, or any shell profile.

---

## 4. Settling the deferred question with the numbers

**Writer starvation — was fatal, is fixed. Not cosmetic in either direction.**
At the accepted baseline `0279174`, exclusive admission had no turnstile and no bound on overtaking. I measured writer waits of 5.8 s and 7.2 s under ordinary reader load — 58% and 72% of the consumers' own 10s ceiling — with no mechanism preventing a longer one; R3 pushed the same load until 2 of 3 attempts actually timed out. The migrator is the only exclusive taker in the product, so before `78f1ccd` **the destructive command could not reliably acquire its own lock on a project with live agents polling.** `78f1ccd` is not a hardening nicety; it is what makes `570a940` runnable. Verified fixed: ~150 ms deterministic admission, flat in reader count, zero reader-throughput cost.

**Unbounded hold — not cosmetic, but it does not block this milestone. It must be closed before v1 as a warning, not necessarily as a re-architecture.**
It is real: I reproduced the failure end-to-end, not by extrapolation — 14,000 files, 13.6 s hold, concurrent `rt-say` rejected at 10.08 s, message delivered nowhere. But the numbers decide the *urgency*, and they say the cliff is far away and the fall is soft:

- **Crossover** (hold ≥ 10 s): ~10,200 files at my measured 0.97 ms/file; ~1,860 files at the author's pessimistic 5.42 ms/file slope. Take the author's — it is the conservative one and the only slope measured on a machine anyone else can reproduce badly.
- **Distance from reality**: the operator's largest live mailbox is 138 files. That is **13x below** the pessimistic crossover and 74x below mine. Hold at 138 files is 0.13 s (mine) to 0.75 s (author's worst slope).
- **Failure mode**: fail-closed, non-zero exit, explicit diagnostic naming the lock file. A *rejected send*, never a send the caller believed succeeded. Zero corruption, zero silent loss — R2 and R3 both drove hundreds of concurrent sends through a live cutover and a live rollback with 0 acknowledged sends lost.
- **Frequency**: once per project, operator-initiated only. `migrate_project` is called from `bin/rt-projects` and nowhere else; `docs/install.md:49` — *"No install or uninstall command migrates mail implicitly."*

**Therefore: fix before v1, at the cost of a preflight, not a redesign.** The three cheap changes that close it are (a) count the mailbox and print a projected hold *before* taking the lock, refusing or warning above a fraction of the 10s consumer bound; (b) document the quiesce step — nothing anywhere tells an operator to stop their seats first; (c) move `_cleanup_local_after_forward` and `_install_bookmark` outside the exclusive section, which returns 11–18% of the hold for free and is safe because the pointer already reads central. What is *not* justified by these numbers is blocking `570a940`, or re-architecting the lock. The hold is a scale property with a 13x margin and a loud failure, sitting behind an explicit one-time operator command.

The composition risk (P2-4) is the one thing that could invalidate that margin — `hold ≈ O(mail) + up to 10 s of global registry-lock wait` means an empty project could in principle cross the bound. Nobody produced a natural multi-second registry-lock holder, so it stays P2, but the preflight in (a) should account for it rather than assuming the hold is bounded by mail volume.

---

## 5. What was NOT established

Read this as the limit of the proof, not as suspicion.

**Durability.** Nobody tested power loss. Every "crash" in all four reports and in mine is `SIGKILL` or `os._exit(9)`, which skips Python unwinding but not kernel writeback. R1 traced the barrier ordering (staging fsync → rename → parent fsync; registry temp fsync → rename → parent fsync at `_rtlib.py:1427-1446`) and the idempotent re-fsync on the `changed==False` retry path (`:1536-1548`), but **no one observed an fsync barrier hold or fail**. If the host loses power mid-cutover, this record says nothing.

**The author's absolute timings.** Four independent reproductions, four results 2–6x below the reported figures, on the same machine, and no one can explain the gap. The leading hypothesis — the iCloud-backed default backup root — is now *contradicted*: `~/Documents/Workspace/backups/roundtable-central-mail` does not exist, so the author's own numbers were not measured there either. Nobody benchmarked against the default root, deliberately, because writing test mail into the operator's synced Documents while he is asleep is outside the rails. **The author's numbers should be treated as a conservative single-host measurement of unknown provenance, not a ceiling and not a bound** — which is exactly how §7 phrases it.

**Credentialed harness paths.** No live wake happened. `rt-codex-wake`, `rt-stop-gate` in its live seat, `rt-wait-inbox --wait-last-wake-drained` as Hermes' follower, and the Hermes watcher were verified by source (they take the lock; `rt-codex-wake:1915` returns `heartbeat=True` on a busy layout so a migration pause is not scored as a fault) and by the author's own passing tests, **not by a real app-server round trip**. R3's Codex conclusion rests on code plus two tests. I exercised only `rt-say`, `rt-inbox`, `rt-projects` and the two Stop-hook binaries live.

**Clean account.** Everything ran as the current UID with an overridden HOME. That is not a fresh account. Cross-UID lock substitution was not attempted by anyone — §7 explicitly disclaims it, and same-UID adversarial replacement of a layout-lock file is out of the stated integrity domain.

**Harness-onboarding interaction.** `roundtable-setup apply` writes into `~/.claude`, `~/.codex`, `~/.hermes` and `~/Library/LaunchAgents`, so nobody exercised the `harness-setup.json` gate, `setup remove --unload-codex`, or LaunchAgent teardown. Whether the new bookmark and central-mail state interact with that second ownership layer is **unknown**.

**Registry upgrade under load.** The live registry is still v1, so no one could observe an in-flight `rt-projects upgrade` racing a concurrent migration.

**Concurrency shapes not covered.** Two *different* projects migrating simultaneously (distinct lock files, shared registry lock) was not tested by anyone — which is precisely the shape P2-4 predicts would amplify. Failure injection between the unlabelled renames inside `_prepare_local_candidate`'s install loop (`_rtmigrate.py:2030-2031`) was not done; only the labelled `_maybe_fail` boundaries were.

**One thing I could not disprove and am recording as an open question rather than a finding.** `RT_MIGRATION_FAILPOINT` is a shipped environment variable that aborts the one destructive command at a chosen internal phase, and `scripts/check_public_safety.py` does not cover it. Every abort I triggered landed in a state the documented rerun repaired. Whether shipping it is intended is a decision the author has to state; nobody established that it was.

---

**Bottom line for the operator.** The migration does what §7 says: one authoritative layout at every interruption point, the registry pointer alone deciding, an atomic flip as the cutover, and — the part that was hardest to see and is the reason this is safe to build on — a rollback that copies *current* central state back, so mail that arrived after the flip comes home. Four independent attacks and this gate all failed to lose a byte. What is not yet safe to *rely on* is the recovery story around it: the single backup bundle that both repair paths hard-depend on lives in a cloud-reachable folder nothing protects, prunes, or declares load-bearing. Fix that pair before v1 and this milestone is done. None of it is installed or reachable on your machine today: the live prefix predates all four commits and the live registry is still the legacy schema that refuses service until you explicitly upgrade it.
