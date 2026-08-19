# Cross-check of the 1.4 architecture review (Codex)

> Status: current — authoritative refinement of the 1.4 defect audit

## Method and independence boundary

My independent review is commit `a6b7813`. I did not open
`architecture-review-1.4.md` or its track-collision companion until that commit
existed.

For this cross-check I read both documents, then re-read every source range
needed to test the four defect claims and five recorded refutations. I also
compared the proposed tiers with my independent ranked findings. “Verified”
below means the current source supports the claim; review-process history that
is not present in the repository is called out as non-reproducible rather than
accepted on authority.

## Agreements

The independent convergence is substantial:

1. **One stdlib-only harness registry.** Both reviews independently select
   `_rtharness.py`, not `_rtlib`, as the home for identity, aliases, labels,
   executable facts, anchor policy, and setup eligibility. The duplication at
   `_rtlauncher.py:36-77`, `pneu:42-67`, `pneu:636-681`, and setup's smaller
   registry is real and directly taxes new harnesses.
2. **Setup should become harness planners over an unchanged transaction core.**
   The ownership, snapshot, atomic-write, backup, manifest, rollback, and
   external-state machinery at `setup.py:106-530` and `:677-783` is
   load-bearing. The harness branches at `:1012-2032` and `:2288-2483` are the
   extraction target, not the safety engine.
3. **Move the explicit cmux keyboard path out of `rt-say`.** The 391-line
   `_main_locked` at `rt-say:978-1368` combines normal maildir delivery with a
   compatibility adapter that starts at `:1188`. Preserve the public switch
   and exit-code contract, but make the normal command independent of cmux
   implementation imports and state.
4. **Build stable test scaffolding before broad movement.** The 13 local
   `write_project` variants and repeated script/lease builders justify a small
   test kit. Scenario-specific builders should remain local. The peer review's
   warning about AST/text needles is valid and fits the same test-hardening
   step.
5. **Do not invest in Grok/OpenClaw lab unification while they are parked.** If
   D14 retains both, only their narrow fenced-generation shell is a plausible
   shared seam. Their transports and credential models are not generic.
6. **Leave migration and lease safety alone.** `_rtmigrate.py` remains frozen.
   Layout-lock order, non-nested cross-project sections, fenced lease revisions,
   fd/owned-path validation, atomic publication, and setup rollback are not
   accidental complexity.
7. **The clean areas are genuinely clean.** `_rtsurface.py`,
   `roundtable-init`, setup's ownership transaction core, and the migration/
   mailbox crash matrices do not need architecture work merely to reduce line
   counts.

## Disagreements

### 1. RC1 is useful infrastructure, not a prerequisite that makes splitting “free”

The flat helper list is a real tax: `pneu_packaging/__init__.py:10-18`,
`cli.py:1015-1066`, `cli.py:1128-1131`, two release-builder sets, the exact-set
test, and `pyproject.toml:43` all participate. A managed helper package with a
recursive digest can reduce subsequent per-module edits.

I disagree with calling this the root prerequisite for all decomposition or
saying decomposition becomes free. Introducing a package changes the wheel
layout, installed-marker ownership model, release allowlists, uninstall
validation, and smoke import behavior. The current explicit files provide
valuable exact ownership. For the approximately four modules both reviews
actually recommend soon, paying explicit manifest entries may be cheaper and
safer than first changing the artifact contract. RC1 should proceed only with
an isolated-release and uninstall proof; other small extractions need not wait
for it.

### 2. The Codex readiness classifier is not yet generic service machinery

`_rtcodex.py:1114-1307` has a reusable *shape*, but its classifier directly
depends on Codex CLI version parsing, owned SessionStart hooks, two exact plist
labels, a Unix-socket app-server handshake, daemon distribution/lineage,
protocol probes, bridge heartbeat, Codex busy-seat reload deferral, and a Codex
reload marker. `_rtruntime.py:1366-1654` is likewise a Codex thread-intent
protocol, not merely a badly named generic binding.

I would not schedule `_rtlaunchd` → ownership → generic readiness extraction
solely because OpenCode might need a supervised server. First implement or
prototype a second admitted F3 harness and identify the exact common state
machine. Until then, `_rtcodex.py` plus `rt-codex-wake` is a defensible
protocol/service-versus-policy boundary. Premature generalization here can make
Codex's build fingerprint and repair lock harder to audit without reducing
actual harness cost.

### 3. `_rtsafeio` cannot replace both `_atomic_write` contracts wholesale

D3 correctly finds two strengths under one name. But `cli.py`'s writer is also
used to create fresh install/version markers and wrappers whose parent trees do
not yet exist (`cli.py:1071`, `:1240`, `:1275`), while setup's writer requires
an existing, validated owned parent (`setup.py:358-387`). The contracts are
different because some callers are bootstrap publishers and others rewrite
already owned sensitive files.

The immediate fix is to name both contracts explicitly and use the strong
owned-rewrite operation at the prefix-migration config/plist call sites
(`cli.py:398`, `:424`). Do not globally replace `cli._atomic_write` with
`setup._atomic_write`. A later shared module can host both primitives, but the
strength must remain visible at each call.

### 4. Do not combine wake vocabulary with maildir commit machinery

The peer review's `_rtmail` targets the wake predicate. My highest-ranked
finding targets the separate tmp→new publication and hard-link→unlink archival
protocol duplicated across `rt-say:588-657`, `rt-inbox:350-412`, and
`rt-ack:79-279`. These should not become one broad “mail” module in one cycle.
Wake eligibility and durable mutation have different failure domains. Extract
the mutation kernel first as literal code motion under the existing layout
lock; extract predicate vocabulary separately, with its current strictness
variants preserved.

### 5. Test files should be split selectively

I agree not to split the migration and mailbox-resolver crash matrices. I do
recommend splitting `test_rt_tooling.py` by durable delivery, cross-project
addressing, malformed mail, and legacy cmux, and `test_rt_codex.py` by bridge,
transport, launchd ownership, and doctor integration. A shared test kit alone
does not remove those ownership collisions.

## What the peer review missed

1. **Durable mailbox mutation is duplicated separately from the wake
   predicate.** The send, quiet-ack drain, and acknowledgement archive paths
   carry parallel fsync/no-clobber/retry logic. This is the most safety-relevant
   extraction because a future change can otherwise strengthen one archive
   path and leave another behind.
2. **Project ancestor discovery has five copies.** `_rtlauncher.py:1072-1077`,
   `rt-wait-inbox:96-102`, `rt-stop-gate:26-32`,
   `rt-startup-advisory:46-57`, and `rt-doctor:710-714` repeat the walk; doctor
   has already drifted to a raw file check instead of `is_project_root`.
   `_rtlib.project_at_or_above` should be the pure primitive, while
   `find_project_root` retains override/fallback/cmux policy.
3. **OpenClaw permits its isolation root inside the project.** Grok explicitly
   rejects this at `integrations/grok/roundtable/__init__.py:265-277`.
   OpenClaw's `create_isolation` at
   `integrations/openclaw/roundtable/__init__.py:202-228` does not. An explicit
   `RT_OPENCLAW_RUNTIME_DIR` beneath the checkout can therefore create adapter
   state inside the project. Because the adapter is parked, fix this only if it
   is retained, but the defect should be recorded now.
4. **The current Codex module boundary is worth preserving until contradicted
   by a second implementation.** Large files are not enough evidence for the
   generic-service extraction proposed in Tier 3.

## Defect audit (§4)

### D1 — inaccurate as stated; a narrower defect is real

The literal observation is correct: `rt-doctor` contains no `openclaw` string,
and `harness_family()` at `rt-doctor:835-844` omits it. The headline “zero
OpenClaw coverage” is false. `inspect_project_seats()` at `:1078-1214` iterates
every configured harness and calls the generic `inspect_seat`; OpenClaw still
gets seat status, owner-anchor, and watcher-anchor checks.

The real defects are narrower:

- `launch_fix()` falls back to the less useful generic “restart agent” text;
- a configured OpenClaw seat and a runtime record with any other unknown
  harness both normalize to `None`, so the `seat-identity` comparison can miss
  a mismatch;
- no OpenClaw-specific Gateway/adapter probe exists.

Add the family mapping and identity regression test, but do not describe it as
adding doctor coverage from zero.

### D2 — verified mechanism; overbroad outcome wording

`setup.py:754-770` rejects every unknown harness entry before any command can
select known entries. `_selected()` at `:2313-2326` already filters operations
through the older binary's `HARNESSES` list, and apply/remove mutate selected
known records while carrying the manifest dictionary. Therefore opaque unknown
records can be preserved safely if they are never interpreted or mutated.

The concrete downgrade problem is verified: an older `roundtable-setup` cannot
plan, status, apply, or remove its known onboarding while a newer harness entry
exists. Saying the whole product is “uninstallable” is too broad because package
uninstall is a separate command/path; saying older onboarding removal is blocked
is exact. The forward-compatibility fix needs a test that proves unknown records
survive byte-for-value across known-harness apply/remove.

### D3 — verified, with a two-contract fix

The strength difference and sensitive migration call sites are exactly as
reported. The target prechecks at `cli.py:369-380` compensate only partially;
they do not provide setup's owned-parent validation and no-follow temp open. Use
an ownership-safe rewrite at those call sites, while keeping a separately named
bootstrap atomic publisher for fresh install trees.

### D4 — verified, but partially masked by a later test

Production install verification at `cli.py:1015-1017` omits `_rtrchost` from
the import command. `tests/test_packaging.py:307-321` later imports it in a
separate installed-root probe, so the repository suite can catch a broken
module even though the product's own verification would not. Add `_rtrchost` to
the production smoke and assert that exact production command, rather than
relying on the test's independent probe.

## Refutation audit (§5)

1. **Mass `_rtlib` facade with zero caller changes — refutation verified.**
   `pneu_packaging/smoke.py:57-75` loads an arbitrary `bin_dir/_rtlib.py` by
   file location and does not add that `bin_dir` to `sys.path`. A facade that
   imports new siblings can fail in the source-bin smoke even when ordinary
   `rt-*` scripts succeed via `sys.path[0]`. Any extraction must fix and test
   this loader explicitly. The narrower envelope/ledger extraction remains
   viable.
2. **In-process `rt-ack` → `rt-say` — refutation verified.** `rt-ack` reads
   origin metadata inside a short layout-lock window (`:327-345`), spawns
   `rt-say` outside it (`:386-455`), then opens a fresh archival window
   (`:461-477`). `rt-say` acquires the target lock and later the origin lock in
   non-nested sections (`rt-say:1436-1491`). The subprocess boundary currently
   makes lock ownership and environment authority explicit. Do not remove it
   as incidental overhead.
3. **Unify parked Grok/OpenClaw adapters now — refutation verified.** The
   2026-08-12 decision makes this speculative investment. Only a retained-code
   safety fix or later narrow shared shell is justified.
4. **Put the harness table in `_rtlib` — refutation verified.** `_rtlib` imports
   `fcntl` and optional `yaml` and owns registry/layout concerns. Both lab
   adapters already import it lazily inside methods to avoid that coupling. A
   stdlib-only `_rtharness.py` is the correct direction.
5. **Four competing safety modules / two `_rtwake.py` meanings — not
   independently reproducible.** The source verifies the underlying duplicate
   primitives, but the repository contains no individual area-review artifacts
   from which to verify reviewer count, proposed filenames, or the “33 new
   modules” arithmetic. Treat the consolidation conclusion as editorial
   provenance, not source-backed evidence. It does not weaken the supported
   recommendation to use a small number of purpose-specific modules.

## Sequencing and collision assessment

The companion collision map is careful and its declared-file mapping is
credible. I agree that T1/T2/T3/T4 should continue, that test scaffolding belongs
with T2, that doctor work belongs with T3, and that Codex/setup extraction waits
for T1. Its honest limitation remains important: it maps briefs, not live diffs
or completion state.

I would reorder the architecture tiers as follows:

1. **Cheap correctness first:** D4; D2 with opaque-record preservation tests;
   corrected D1 under T3; record the OpenClaw isolation defect for the D14
   retain/park decision. Handle D3 with explicit bootstrap-versus-owned writer
   contracts, not a global replacement.
2. **Test ownership:** T2's locator fixes and stable `tests/_kit`, plus selective
   split of the two mega test files.
3. **Artifact decision:** prototype RC1 against release install, verification,
   downgrade, and uninstall. Adopt it only if the recursive package remains as
   auditable as the current exact helper set.
4. **One fact per module:** `_rtharness` and canonical project discovery. Then
   `_rtmail` wake vocabulary, independently from the durable mutation kernel.
5. **Transport isolation:** durable maildir mutation kernel with fault injection;
   then, in a separate cycle, legacy cmux extraction.
6. **Harness-facing orchestration:** launcher descriptors and setup planners over
   the unchanged transaction engine.
7. **Deferred:** doctor probe registry and watcher emit after their active
   tracks; generic Codex service/launch-intent extraction only after a second
   admitted harness proves the common boundary.

I agree with the peer review's “never in the same cycle” constraints and add
three more:

- never combine wake-predicate extraction with durable publish/archive code
  motion;
- never combine helper-package ownership changes with maildir or setup safety
  extraction;
- never combine a Codex generic-service experiment with bridge fingerprint,
  protocol, or live reload behavior changes.

## Bottom line

The peer review's central verdict survives: safety mechanisms are strong, fact
ownership and file boundaries are the problem, and harness expansion should
wait for a canonical harness vocabulary. Its tier plan is directionally right
but too eager to make packaging and Codex service machinery generic. Correct D1
before forwarding it, preserve two explicit filesystem-write contracts, and
add the missing durable-mutation and project-discovery seams to the final plan.
