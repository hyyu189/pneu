# Whole-project architecture review — 1.4

> Status: current — Tier 0 and the RC1/D2/D3/D4 defects landed in 1.4 (T2, T6); Tiers 1-3 are open backlog

Date: 2026-08-15. Branch: `wt/t5-adapters`. Requested by Ocean as a follow-on to
the T5 harness-expansion track: *if we are going to refactor the launcher, do it
thoroughly — review the whole project for modularization and decoupling.*

Method: nine parallel area reviewers (one per module cluster) reading the source
directly, then one adversarial pass whose job was to refute them against the
codebase's real invariants and `decision.md`. 62 findings, 29 rated high by their
authors; the skeptic refuted or merged 11 of them. Every claim reproduced in this
document was re-verified against source by hand — the reviewers' evidence strings
were not taken on trust, and one headline proposal died that way.

Nothing was changed. Base suite green at review time (1081 passed, 1 skipped).

## 1. Verdict

The code is well engineered and badly filed.

That is not a diplomatic phrasing — it is the actual finding, and it is
consistent across all nine areas. The safety mechanisms this product rests on
(fd-based owned-path reads, the two-lock layout turnstile, witness-confirmed
project identity, compare-and-swap registry writes, fenced seat leases with
pid+start-fingerprint liveness, setup's snapshot/backup/rollback) are careful,
deliberate, and internally consistent. Reviewers repeatedly noted that the inline
comments explain *why* each gate exists, which is rare. **No reviewer found a
mechanism that should be removed or relaxed.**

What is wrong is boundaries. The same fact is written down in four to six places;
the same primitive exists in two strengths under one name; the module that every
tool imports contains eight unrelated concerns; and the diagnostic surface has no
abstraction at all. None of that is dangerous today because the current
maintainers hold the whole tree in their heads. All of it becomes expensive at
exactly the moment the project has just committed to — adding harnesses.

So the answer to the question asked is: **yes, and the launcher is the third or
fourth most valuable thing to fix, not the first.**

## 2. Four root causes

Nearly every finding in the review reduces to one of these. They are listed in
the order they should be addressed, which is not the order of their severity.

### RC1 — The installer's flat file manifest taxes every module split

`pneu_packaging/__init__.py:10-18` declares `MANAGED_HELPERS` as a flat tuple of
seven `.py` files. The install marker records a sha256 per helper
(`cli.py:1059-1066`), verification requires
`set(helper_digests) == set(MANAGED_HELPERS)` exactly (`cli.py:1128-1131`),
`scripts/build_release.py` carries the list twice more, and
`tests/test_packaging.py:301` asserts exact-set equality. There is also a
hardcoded import-smoke list at `cli.py:1015-1017`.

So "split `_rtlib.py` into four modules" is not a four-file change; it is a
four-file change plus four manifest edits, and an unlisted helper ships but is
unrecorded and survives uninstall. Across all proposals in this review the naive
total was **33 new modules under `bin/`, each paying that tax**.

`bin/` is a flat `sys.path[0]` namespace with no package boundary, which is why
the tax exists. Fixing the manifest to describe a helper *package* (recursive
digest, one entry) converts decomposition from expensive to free. **This is the
one prerequisite that makes everything else in the plan cheaper, and no
single-area reviewer could see it.**

### RC2 — One fact, many copies, silent drift

The review verified, by hand:

- **Harness identity** exists as at least five independent tables:
  `_rtlauncher.py:67-73` (`CONFIG_HARNESSES`), `bin/pneu:60-67`
  (`HARNESS_CONFIG`), `rt-worktree:525`, `_rtrchost.py:253`, and
  `rt-doctor:835-844` (`harness_family`) — plus `setup.py:37`'s bare `HARNESSES`
  tuple, which is the same fact in a different shape. Counting the per-harness
  dicts inside `_rtlauncher.py` alone, one reviewer put the total at 13 tables
  across 6 files. **Nothing fails when you miss one** — see §4, D1.
- **The maildir wake predicate** — "what counts as wake-eligible mail in `new/`"
  — is implemented at 12 sites, with the `ack-`/`.` filter spelled three
  different ways and at genuinely different strictness (`rt-codex-wake` rejects
  a symlinked or non-`.md` entry; `rt-stop-gate` lists it). This is the single
  rule whose failure mode the project has already hit once: an adapter that
  forgets to skip `ack-*` wakes its seat on its own acknowledgements.
- **`agents.yaml` seat resolution** — six readers, silently different failure
  semantics.
- **Filesystem-safety primitives** — `_atomic_write`, `_json_bytes`,
  `_ensure_private_dir`, `_fsync_directory`, `_sha256`, `_path_info`, `_lexists`
  each exist in three to four copies. See §4, D3 for the one where the copies
  have diverged in strength.
- **launchd job management** — three implementations of bootout→wait→bootstrap;
  the race-wait was independently rediscovered.

### RC3 — Generic machinery is named after its first consumer

The Codex subsystem is 5.7k lines against ~900 for Grok. Roughly half of it is
not Codex knowledge. `_rtcodex.py:173-181` defines eight service states and
`:1114-1307` a 190-line classifier whose skeleton — states, classification order,
lock ordering, converge loop, per-state repair dispatch, deferred-reload
busy-seat policy — is exactly what any future supervised-service harness needs,
and it is unreachable from outside because it is spelled in Codex nouns. The same
pattern recurs smaller: `_rtruntime.py:1366-1654` is ~290 lines of Codex-only
launch-intent protocol inside the harness-neutral runtime module, and the most
complete launchd layer is buried in the Codex module where the other two copies
cannot see it.

This is the finding with the most direct bearing on T5. The OpenCode blueprint
needs precisely this shape — a supervised server, a readiness classifier, a
binding record, a liveness probe — and today it would have to re-derive the lock
ordering and the state dispatch, which is where a subtle mistake becomes a
stranded seat.

### RC4 — Two surfaces have no abstraction at all

- **`rt-doctor`**: 19 probes, 24 check names, 85 `report.item()` call sites,
  hand-wired as a call sequence in three places, with 8 probes having no function
  boundary at all (6 inline in `main()`, 2 inline in `project_health_checks()`).
  Probe identity is a repeated string literal; ordering is statement order;
  applicability is a hand-maintained name tuple. Every harness adds probes here.
- **`pneu_packaging/setup.py`**: a harness is a string compared against a literal
  at 37 places. There are three clean three-way dispatchers, and then the
  per-harness knowledge leaks into 14 more functions and four registries.

## 3. What is well-factored — do not touch

Stating this is part of the deliverable. Three areas came back clean and should
be explicitly out of scope:

- **`bin/_rtsurface.py`** — a clean, injectable, dataclass-shaped adapter with a
  single dispatch point. No work proposed.
- **`bin/roundtable-init`** — small, template-driven, correct.
- **The generic ownership core of `setup.py`** (`:106-530`, `:677-783`) — owned
  path inspection, chain validation, atomic write, snapshot/restore/rollback,
  backup+manifest, mutation lock. Harness-agnostic and genuinely good. It should
  be *extracted verbatim* if anything, never rewritten.
- **`tests/test_mailbox_resolver.py` and `tests/test_mail_migration.py`** — the
  crash/retry matrices are stronger than most projects ship. A reviewer
  explicitly recommended against splitting them.
- **The lease mechanism in `_rtruntime.py`** — fencing, owner liveness via
  pid+start fingerprint, and storing `wake` as a sub-object of the lease record
  under one state lock are correct and mutually reinforcing.

Also out of scope by standing decision: **`_rtmigrate.py` is frozen**
(`decision.md` 2026-07-29). One consequence is permanent rather than one-off —
it imports 12 private symbols from `_rtlib`, so any `_rtlib` facade must
re-export all 12 forever.

## 4. Live defects found along the way

These are not refactors. They are bugs or latent traps, each independently
shippable, and each verified against source during this review.

**D1 — `rt-doctor` has zero OpenClaw coverage.** `grep -ci openclaw bin/rt-doctor`
returns 0. `harness_family()` (`rt-doctor:835-844`) maps claude, claude-code,
codex, hermes, hermes-agent, grok, grok-build — and omits openclaw. Every
consumer keyed on it therefore skips OpenClaw seats, and the stale-seat fix
string at `:828-833` degrades silently. This is RC2 demonstrating itself: five
copies of one table, one of them already out of sync, no test failing.

**D2 — the setup manifest is not forward-compatible, and T5 is what trips it.**
`setup.py:767-769` raises on `any(name not in HARNESSES for name in harnesses)`.
`HARNESSES` is `("claude", "hermes", "codex")`. The moment a build ships harness
#4 and writes its name into the manifest, an older build can no longer read that
manifest — including for `remove`. So a downgrade after adding a harness leaves
onboarding uninstallable by the older binary. `_selected()` already filters
through `HARNESSES`, so carrying unknown entries through is safe; this is a
one-line fix that must land *before* the first new harness, not after.

**D3 — two `_atomic_write` contracts under one name, and the weaker one writes
the more sensitive targets.** `setup.py:358-387` validates ownership of both the
target and its parent via `_inspect_owned`, opens with `O_NOFOLLOW`, and refuses
a pre-existing temp path. `cli.py:181-201` does none of that and additionally
does `mkdir(parents=True, exist_ok=True)`. The weaker one is used at
`cli.py:398` to rewrite a **harness configuration file** and at `cli.py:424` to
rewrite a **plist**, during 1.0 prefix migration. In fairness the call site
compensates for part of it — `cli.py:369-380` checks `is_symlink()`, `is_file()`
and `st_uid == getuid()` on the target — but not the parent-directory ownership
that `setup.py`'s version checks, and a reader cannot tell from the call which
contract they are getting. The finding is "same name, two contracts", not "hole".

**D4 — the install import smoke omits `_rtrchost`.** `cli.py:1015-1017` imports
`_rtcodex, _rtlauncher, _rtlib, _rtmigrate, _rtruntime, _rtsurface, yaml` — six
of the seven managed helpers. A syntactically broken `_rtrchost.py` passes
install verification.

## 5. Refuted proposals

The skeptic killed or merged eleven. The five worth recording:

**The headline `_rtlib` split, as proposed, is wrong.** The reviewer proposed
splitting `_rtlib.py` into eight modules behind a re-export facade and claimed
"zero production call-site changes". Verified false:
`pneu_packaging/smoke.py:57-75` loads `_rtlib.py` via `spec_from_file_location`
and **never inserts `bin_dir` into `sys.path`**. `rt-*` tools get `bin/` free as
`sys.path[0]`; `roundtable-smoke` is a console-script entry point and does not.
A facade doing `from _rtsafeio import ...` breaks smoke on a real install while
every test passes from source. Salvage: extract only `_rtenvelope.py` and
`_rtledger.py` (verified zero inbound edges) plus an `__all__`, and leave the
registry/identity/lock mass alone this cycle.

**Merging `rt-ack`'s subprocess call to `rt-say` into an in-process call is
unsafe.** The process boundary *is* the lock discipline. `rt-ack` takes the
layout lock in three disjoint windows (`:287`, `:327`, `:386`) and the `rt-say`
spawn at `:429-455` sits deliberately outside all of them; `rt-say` then takes
its own lock at `:1436`. An in-process call makes nesting a same-process flock
reachable by a later refactor. Dropped.

**Reworking the Grok and OpenClaw adapters is investment against parked code.**
`decision.md` 2026-08-12 records both as deviations pending "rework TUI-first or
park honestly". Unifying their two WebSocket clients now spends effort on ~950
lines each that may shrink or leave.

**Two reviewers proposed two homes for the harness table** (`_rtlib` vs a new
`_rtharness.py`). `_rtlib` is the wrong home: it pulls `yaml` and `fcntl` and the
whole registry, which is exactly why `integrations/grok:703` and
`integrations/openclaw:787` import it *lazily inside methods*. The table belongs
in a stdlib-only `bin/_rtharness.py`.

**Four reviewers independently proposed a safety-primitives module under four
different names**, and two proposed the same filename `_rtwake.py` with different
contents. Merging these conflict resolutions is what cuts the 33 new modules of
RC1 down to about 12.

## 6. Sequenced plan

Tiers are hard dependencies, not preference. Within a tier, items are
parallelisable.

**Tier 0 — test scaffolding, no product code.** Nothing else can safely start
first, because two tests currently pin the shape of the code the refactors would
move.
- Replace the AST locators that assert on source text. `tests/test_grok_adapter.py:446`
  does `launcher_source.split("def launch(", 1)[1]` — that is *everything after*
  `def launch(`, i.e. to end of file, not the function body. Any table placed
  below that point in `_rtlauncher.py` fails it. Verified.
- Derive the architectural-fitness consumer list instead of hand-maintaining it;
  it currently exempts both `integrations/` adapters, the two files most likely
  to drift.
- Build `tests/_kit`: 13 separate `write_project` definitions exist against a
  45-line `conftest.py` that provides only env isolation, with ~4,200 lines of
  per-module preamble across 43 files. The oracle for this refactor is the
  existing suite.

**Tier 1 — one home per fact.** Nothing in Tier 2+ starts until these three names
exist and are the only copies.
- `bin/_rtsafeio.py` — the merged safety primitives (resolves RC2's most
  dangerous case and D3).
- `bin/_rtharness.py` — the single harness registry, stdlib-only (resolves D1
  and unblocks the launcher work).
- `bin/_rtmail.py` — seat maildir vocabulary and the wake predicate with an
  explicit strictness enum, preserving the three genuinely different behaviours
  rather than flattening them.

**Also in Tier 1, independent:** the RC1 manifest fix. It has no code
dependencies and it makes every later module cheaper, so land it first in
practice even though nothing blocks on it.

**Tier 2 — independent, cheap, parallelisable.**
- Move `rt-say`'s retired cmux keyboard path into `_rtlegacy.py`. Do this early:
  every later mail refactor edits that file, and the legacy branch is what forces
  its three-phase argv inspection and its single ~390-line function.
- `__all__` on `_rtlib` plus the verified dead-code deletions (`agent_names`,
  `current_workspace_ref`, `has_lifecycle`, `find_msg`, and `_rtlib`'s copies of
  `run_json`/`acquire_lock`, which `rt-say` and `rt-refresh` each redefine).
  Do **not** unify `acquire_lock` — the three copies have different timeout
  defaults.
- D2, D4 — one-line fixes.
- Decide `rt-stop-gate`'s status: it is a superseded hook generation still
  shipped as a tool, it is the only production caller of `update_activity`, and
  `activityRevision` has zero production readers while the lease validates it on
  every read. That is a decision, not a refactor.

**Tier 3 — dependent.**
`_rtagents.py` (needs `_rtharness`) · the `_rtintent` extraction from
`_rtruntime` (needs a state layer) · `_rtlaunchd` → the Codex bridge-ownership
extraction → the readiness-classifier extraction, in strict order because each
mutates the wake-bridge build fingerprint · the watcher `emit` generalization
(needs `_rtmail`) · `rt-doctor`'s probe abstraction (needs `_rtharness` +
`_rtmail`) · the `HarnessDescriptor` launcher refactor (needs Tier 0 +
`_rtharness`) · setup's `HarnessSpec`, then the typed operation record, then the
triplicated upgrade branch — in that order.

**Never in the same cycle:** the two competing `_rtlauncher.launch()` rewrites ·
the mail-module and watcher-emit refactors · setup's spec and operation-record
refactors · the three Codex extractions with each other · any `_rtlib` mass move
with anything else under `bin/`.

## 7. Hidden costs

- **`_rtmigrate.py` is frozen**, so the private-symbol alias set it imports can
  never shrink. Permanent tax, not a one-off.
- **`tests/test_grok_mutation.py:109-115` pins 16-space indentation** inside a
  multi-line needle with `count == 1`. Any reindent of the Grok adapter fails a
  *safety* test with a confusing message.
- **`decision.md` 2026-07-23 requires the hook process itself to exit 2** for
  Claude's `asyncRewake`. The watcher `emit` generalization preserves this; any
  JSON-output mode must never become the hook's mode.
- **`rt-say:1466-1491` opens the origin-mailbox lock in a deliberately
  non-nested section** to prevent A→B/B→A cycles, and says so in a comment. A
  context-manager refactor makes nesting the natural spelling. Encode
  non-nesting as a test before touching it.
- **`rt-ack:509-510` records the global lock order** — "runtime lease locks are
  deliberately acquired before any layout lock ... keeps the global lock order
  acyclic during central-mail migration". A shared CLI scaffold must carry that
  comment into the helper, not lose it.
- The four-module per-harness test taxonomy (adapter / interop-lab / mutation /
  soak) is real and load-bearing, per `docs/compatibility.md` — but it ships as
  copy-paste, so the next harness pays for it again. That is the test-side
  version of RC3.

## 8. Recommendation on scope

**Tier 0 plus Tier 1 plus the RC1 manifest fix is one cycle's work, and it is the
cycle I would run.** It ships no user-visible feature. What it buys is that every
subsequent refactor — and every new harness — costs a fraction of what it costs
today, and three of the four live defects close along the way.

The launcher `HarnessDescriptor` refactor proposed in the T5 blueprints
(`handoff/harness-expansion-blueprints.md` §1.1) survives review intact, but it
moves from "prerequisite item 0" to **Tier 3**: it depends on the Tier 0 test
locators and on `_rtharness.py`, and doing it first would mean writing the
descriptor against five scattered tables instead of one. The T5 plan's ordering
should be amended accordingly — its item 0 becomes this review's Tier 0 + Tier 1.

Two things argue against a bigger swing. First, `_rtmigrate.py` is frozen and
`integrations/{grok,openclaw}` are parked, which removes ~5.6k lines from the
addressable surface. Second, the calibration Ocean set in `decision.md`
2026-07-29 applies directly here: a surface whose blast radius is only ourselves
needs the load-bearing properties, not fleet-grade machinery. This plan extracts
and de-duplicates; it does not redesign anything that currently works.

## 9. What this review did not do

- Changed no code, ran no refactor, and proposed no behavior change except the
  four defects in §4, each of which is a fix rather than a redesign.
- Did not review `_rtmigrate.py` beyond noting its frozen status and its import
  coupling, per the standing decision.
- Did not review `scripts/*_lab.py` (Grok ACP and Herdr labs, ~1.4k lines) or
  `docs/`.
- Did not attempt to validate the effort estimates by prototyping any extraction.
  S/M/L here are reviewer judgment, and the one estimate the skeptic checked
  ("`_rtlib` split is L, mechanical") turned out to be wrong.
- Did not measure runtime performance. No finding in this review is
  performance-motivated; "optimization" throughout means structural cost, not
  speed.
