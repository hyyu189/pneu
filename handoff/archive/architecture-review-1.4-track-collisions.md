# Refactor plan vs. the live 1.4 tracks — collision analysis

> Status: historical record — the collision map against the 1.4 tracks, which have all merged

Date: 2026-08-16. Companion to `handoff/architecture-review-1.4.md`.
Audience: `claude@roundtable-product`, for the concurrent / pause / fold
decision Ocean asked for.

Method: read `handoff/archive/t1-brief-1.4.md` … `t4-brief-1.4.md` in this tree and
mapped each track's declared file ownership against the refactor plan's tiers.
This is a file-level collision map, not a judgement about the tracks' merits.

## Summary

**Only one piece of the refactor plan is safe to run concurrently with the 1.4
batch today: the packaging/manifest work (RC1 + D2 + D4). Everything else
collides with at least one active track.** Two collisions are total rather than
partial — Tier 0 *is* T2's file set, and the watcher/doctor items *are* T3's two
files.

That is not an argument to pause the tracks. It is an argument that the refactor
is mostly a *post-1.4-merge* activity, with three exceptions that should move
now — one of which belongs inside an existing track rather than in a new one.

## Per-track map

### T1 — capability binding, bridge hygiene, canonical daemon

Declared ownership: `_rtlauncher.claim_launch_seat`, `arm_codex_launch_intent`,
`rt-codex-session-start`, `rt-codex-wake`, `setup.py`'s codex section and the
ownership manifest, plus three new `rt-doctor` probes (dual-host inventory, join
drift, fd headroom).

| Refactor item | Collision |
| --- | --- |
| `_rtintent` extraction (`_rtruntime.py:1366-1654`) | **Total** — that range *is* `arm_codex_launch_intent` |
| Codex `_rtlaunchd` → bridge-ownership → readiness-classifier extractions | **Total** — same subsystem, and each mutates the wake-bridge build fingerprint |
| setup `HarnessSpec` / typed operation record / upgrade-branch dedup | **High** — T1 adds a codex section and manifest ownership to the same functions |
| `rt-doctor` probe abstraction | **High** — T1 adds three probes to the surface being abstracted |
| **D2 (manifest forward-compat)** | **Helpful, not conflicting** — T1 writes new manifest entries; the `any(name not in HARNESSES)` refusal at `setup.py:767-769` is a trap T1 walks toward |

Verdict: every Codex-side refactor item queues behind T1. D2 should land *before*
T1 finishes, and T1 is the natural place to notice it.

### T2 — test process (xdist, journey tier, mutation discipline)

Declared ownership: `tests/` broadly, `requirements-dev.txt`,
`tests/test_mailbox_resolver.py:679-684`, `tests/test_open_journey.py`,
`bin/rt-worktree:793`.

| Refactor item | Collision |
| --- | --- |
| Tier 0 — replace the AST source-text locators | **Total** |
| Tier 0 — derive the fitness-function consumer list | **Total** |
| Tier 0 — build `tests/_kit` | **Total** — 13 `write_project` definitions across 13 files |

Verdict: **Tier 0 is not a separate track; it is T2's scope.** The two are the
same work in the same files under the same discipline — T2 is already
mutation-checking its own tests, and the AST-locator finding
(`tests/test_grok_adapter.py:446` does `split("def launch(", 1)[1]`, which is
everything to end of file rather than the function body) is a mutation survivor
of exactly the class T2 is hunting. Running Tier 0 in a second tree would mean
merging two independent rewrites of 43 files.

**Recommendation: fold Tier 0 into T2 by amendment, or drop Tier 0 until T2
merges.** Folding is better — it costs T2 little and it unblocks the launcher
work T5 already scoped.

### T3 — watcher lifecycle and observability

Declared ownership: `bin/rt-wait-inbox`, `bin/rt-doctor`. Its brief explicitly
tells it to coordinate nothing with T1's Codex files.

| Refactor item | Collision |
| --- | --- |
| Watcher `emit` generalization + Claude-adoption extraction | **Total** — `rt-wait-inbox` is T3's file |
| `rt-doctor` probe abstraction | **Total** — `rt-doctor` is T3's file |
| `_rtmail` (the merged wake predicate) | **Partial** — 4 of the 12 predicate sites are `rt-wait-inbox:151,159,181,273` |
| **D1 (rt-doctor has zero OpenClaw coverage)** | **None — T3 is the natural owner.** One line in `harness_family()` at `rt-doctor:835-844` |

Verdict: the watcher and doctor refactors queue behind T3. But T3 is about to add
lifecycle logging, self-heal, and three correlation lines to precisely the two
files the refactor wants to restructure, so **T3 should be told the intended
end-state now** — not to change its scope, but so it does not harden the current
shape further (e.g. adding a fourth hand-wired probe site, or threading another
harness-specific parameter through `run()` the way `claude_hook` is threaded
today).

Recommend handing T3 two things: D1 as a one-liner, and one paragraph of
"this is where the probe surface is going".

### T4 — grok-4.6 supervised trial

Declared ownership: `docs/compatibility.md` grok section,
`scripts/herdr_open_lab.py`, grok leader-socket research.

| Refactor item | Collision |
| --- | --- |
| — | **None.** The review explicitly recommends *against* touching `integrations/{grok,openclaw}` because `decision.md` 2026-08-12 has them parked pending rework-or-park |

One caution to pass along rather than a collision:
`tests/test_grok_mutation.py:109-115` pins 16-space indentation inside a
multi-line needle with `count == 1`. If T4's work reindents the grok adapter, a
*safety* test fails with a confusing `count != 1` message.

Verdict: **T4 runs concurrently, unchanged.**

## What can move now

| Item | Files | Collides with |
| --- | --- | --- |
| **RC1 — helper manifest becomes a package** | `pneu_packaging/__init__.py`, `cli.py`, `scripts/build_release.py`, `tests/test_packaging.py` | nothing |
| **D2 — manifest forward-compat** (`setup.py:767-769`) | one line | nothing; T1 benefits |
| **D4 — install import smoke omits `_rtrchost`** (`cli.py:1015-1017`) | one line | nothing |
| **D1 — rt-doctor OpenClaw** (`rt-doctor:835-844`) | one line | hand to T3 |
| **D3 — two `_atomic_write` contracts** | `setup.py:358-387`, `cli.py:181-201` | nothing, but see note |

D3 note: the fix is to make `cli.py`'s copy adopt `setup.py`'s contract, not to
merge them yet — merging is the Tier 1 `_rtsafeio` work, which should wait.

RC1 is the highest-value concurrent item: it is what converts every later module
extraction from "four manifest edits" into "free", and no track touches the
helper manifest.

## Recommended decision

1. **T1, T2, T3, T4 all continue.** Nothing in this review justifies pausing a
   track.
2. **Amend T2 to include Tier 0.** It is the same files, the same cycle, and the
   same mutation discipline. Without it the launcher refactor T5 scoped cannot
   start.
3. **Give T3 D1 plus a one-paragraph heads-up** on the probe-abstraction
   end-state.
4. **Give T1 D2** — it is a trap on T1's own path.
5. **Schedule RC1 + D3 + D4 as a small standalone packaging item**, assignable to
   any free seat, including this one.
6. **Everything else in the refactor plan is post-merge**, sequenced as Tier 1 →
   Tier 2 → Tier 3 in `handoff/architecture-review-1.4.md` §6, and none of it
   should start while the track that owns its files is open.

## Honest limits of this analysis

- It maps *declared* ownership from the briefs, not what the tracks have actually
  touched. A track that has drifted outside its brief will produce collisions
  this map does not show.
- It does not know T1–T4's completion state. If a track is nearly done, its
  collisions expire sooner than this suggests.
- The refactor plan's own effort estimates are unvalidated (see
  `architecture-review-1.4.md` §9), so "post-merge" is an ordering claim, not a
  schedule.
