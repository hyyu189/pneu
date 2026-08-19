# Documentation consolidation — track report

> Status: current — the delivery record for the 1.5 layer-0 documentation
> track. Dispatch: `docs-consolidation-brief.md`.

Branch `wt/docs-consolidation`, eight commits, no merge. Docs only: nothing
under `bin/`, `pneu_packaging/`, `integrations/`, or `scripts/` changed. The
one test touched is a docstring path reference in `tests/test_bin_syntax.py`.

Verification: `python scripts/check_public_safety.py` (exit 0, read directly)
and the full suite, **1214 passed / 1 skipped** under
`mamba run -n general pytest -q -n auto`.

Commits:

| Commit | Work item |
| --- | --- |
| `9120065` | `PRINCIPLES.md` |
| `e0f3a12` | `handoff/` triage |
| `2ce32a7` | README + `docs/` + `AGENTS.md` + `ROUTING.md` accuracy pass |
| `48b5af6` | `docs/ux/launcher.md` |
| `779d844` | this report |
| `1b25408` | launcher spec §5.4–§5.6 (Ocean's 2026-08-18 occupancy ruling) |
| `91013e6` | audit rework: stray files untracked, §1.5 correction, polish |
| this one | `BRIEF.md` rewritten thin; new `BACKLOG.md` |

---

## 1. `PRINCIPLES.md`

New at the repository root. Five ranked principles: UX first · delivery core
vs. wake adapters · state discipline · support-claim discipline · brief
protocol. Written from Ocean's 2026-08-17 ratification.

Two things worth knowing about how it was written:

- **Principle 1 names the bound-thread model as the canonical reading** of the
  2026-08-12 seat ruling, and says explicitly that "TUI" there was period
  shorthand. The draft `decision.md` entry in §5 is the version for Ocean to
  ratify and append himself; `decision.md` was not touched.
- **Principle 3's roster behaviors are labelled to-be against verified code,
  not against the brief's summary.** `roundtable-init` writes a fixed
  `claude`/`codex`/`hermes` template (`templates/agents.yaml.tmpl`) regardless
  of what is installed; there is no census. There is no pneu worktree
  inheritance mechanism either — a linked tree receives whatever `agents.yaml`
  the branch has committed, by ordinary Git checkout. `pneu seat add/rm` does
  not exist; the launcher card's `a` key is the only roster write that ships.

Discoverability: `PRINCIPLES.md` is now reachable from `AGENTS.md` (mission),
`ROUTING.md` (a new conditional `principles:` entry keyed on design,
harness-adaptation, support-claim, and brief-writing triggers), and
`README.md`. `CLAUDE.md` was left alone deliberately — an `@PRINCIPLES.md`
eager import would load it into every session, and the ROUTING conditional is
the cheaper equivalent.

---

## 2. `handoff/` triage

86 files classified. **14 stay in `handoff/`, 72 moved to
`handoff/archive/`** with names unchanged. Every one of the 86 now carries a
one-line `> Status:` header naming its classification and, for superseded
files, what replaced it. `handoff/archive/README.md` states the rule for the
directory.

### Stayed — current

| File | Why it is still true |
| --- | --- |
| `architecture-review-1.4.md` | Tier 0 and RC1/D2/D3/D4 landed (T2, T6); Tiers 1–3 are open backlog. Verified: `bin/_rtsafeio.py`, `_rtharness.py`, `_rtmail.py` do not exist. |
| `architecture-review-1.4-codex.md` | the independent review behind that plan |
| `architecture-review-1.4-crosscheck-codex.md` | authoritative refinement of the defect audit |
| `harness-expansion-survey.md` | the 13-candidate verdict table; no candidate implemented |
| `harness-expansion-blueprints.md` | design proposals awaiting scheduling; T5 shipped paper, not adapters |
| `d14-openclaw-source-audit-2026-08-17.md` | newest OpenClaw state (2026.7.1-2); gates any future rework |
| `d15a-xdist-verdict.md` | cited by `README.md` and `tests/test_collection_determinism.py` |
| `d15b-journey-tier.md` | what the shipped journey tests pin |
| `grok-leader-socket-wake-2026-08-14.md` | standing negative result for A-class Grok wake |
| `btw-thread-semantics.md` | upstream reference behind the fork/ephemeral handling |
| `upstream-codex-zero-turn-resume.md` | draft issue, still unfiled (BRIEF #6) |
| `docs-consolidation-brief.md` | this track's brief |
| `antigravity-harness-research-2026-08-03.md` | historical — see below |
| `antigravity-stage1-2026-08-04.md` | historical — see below |

**The two Antigravity files are historical but did not move.** `decision.md`
2026-08-05 cites those exact paths, and that ledger is append-only, so moving
them would have required editing it. Their headers say so. This is the one
place where the brief's "grep and update every path reference" and its "never
edit `decision.md`" instructions pull against each other; the ledger wins.

### Moved — by category

- **v1 central mail** (6): the converged design, four acceptance records, the
  M4 addressing handoff.
- **2026-07 field records** (8): three `findings-*`, the six-lens audit, the
  live-validation run, the UX review, the SessionStart turn-gated retraction,
  the resume-binding defect.
- **Codex upstream surveys and proposals** (6): seat lifecycle, worktree/remote
  survey, env-channel proposal, canonical-daemon proposal, capability-binding
  architecture, mobile pairing design.
- **D-series dispatch/report pairs** (25): M5, D7–D14, D16-1, D2, the
  worktree-open fix, the payload-docs neutralization.
- **OpenClaw** (4) and **Grok** (3) workstreams.
- **1.4 cycle** (16): T1–T6 briefs and results, the adversarial charter,
  findings and joint verdict, the track-collision map, the review dispatch,
  the harness-expansion dispatch.
- **Launch** (1): `launch-materials-brief.md`, whose "Product facts (verified,
  current)" block describes pneu 1.1.0. A resumed launch workstream needs a
  fresh brief, not this one.

### Reference updates

`handoff/<name>.md` references were rewritten tree-wide to
`handoff/archive/<name>.md` for the 72 moved files — 29 files touched,
including the docstring in `tests/test_bin_syntax.py`. Two classes were
deliberately left alone:

- `<rev>:handoff/…` git-show paths inside archived documents (for example
  `git show wt/t6-packaging:handoff/d14-openclaw-isolation-root.md`). That
  path is correct for the revision it names; rewriting it would break it.
- Bare relative links between two files that both moved — they still resolve,
  because both landed in the same directory.

After the move, the only remaining bare `handoff/*.md` references anywhere in
the tree point at files that stayed. Verified by grep.

---

## 3. README and `docs/` accuracy pass

### Corrected as false

1. **README described the numbered selector as the non-TTY fallback.** It is
   not. `bin/pneu` `main()` prints usage and exits 2 when `stdin.isatty()` is
   false; the numbered selector needs a TTY stdin with a non-TTY stderr,
   because `_rich_card_available` requires both. Confirmed against
   `tests/test_roundtable_cli.py:161`.
2. **`docs/install.md` listed the launcher's seats as Claude, Codex, and
   Hermes.** `HARNESS_ORDER` has included Grok Build since 1.3.3.
3. **`docs/architecture.md` still listed the Grok credentialed native-TUI wake
   as outstanding promotion work**, and `docs/release.md` gate 6 said the same
   — while `docs/compatibility.md` records that run passing on the development
   host on 2026-08-14 with timings and message counts. Resolved in favour of
   `compatibility.md`: it is the file with the dated evidence, and the other
   two were carrying pre-2026-08-14 text. Both now name what actually remains
   (resume re-arm, clean-account/terminal matrix).
4. **README stated the suite is parallel-safe with no load qualifier.**
   `d15a-xdist-verdict.md` calls the not-already-saturated qualifier
   load-bearing, and records that an adversarial review rejected exactly that
   unqualified wording once already.

### Corrected as stale

"The release candidate now implements…" and "At this release-candidate
stage…" for a shipped 1.3.5 · "this 1.0.0 release" in README's History ·
"Before the Build Week release" as a live gate deadline · the Devpost and
"judge journey" framing in `release.md`, now the five-minute new-user journey
(the five-minute claim itself is a real standing gate and was kept).

### Redundancy cut

`docs/architecture.md` and `docs/install.md` each carried their own copy of
the promotion gates. Both now point to `docs/compatibility.md`, which is the
one home for what has been exercised. `docs/release.md` keeps its own
pre-tagging checklist — a different fact, the procedure rather than the status
— plus its RC-series evidence records, relabelled as pre-1.0 evidence rather
than current state.

### Status headers on period-named docs

`docs/wp21-quiet-ack-nudge-retirement.md` describes current behavior under a
pre-1.0 work-package name and now says so. `docs/legacy-v1-keyboard.md` is
labelled a historical record of a retired path, with the note that
`--legacy-nudge-only` still exists in `rt-say`, which is why the file is kept.

### `AGENTS.md` and `ROUTING.md` — outside the brief's literal scope

Flagging this explicitly because the brief said "README + `docs/`".
`AGENTS.md` carried a review-window freeze that expired on 2026-08-12 by its
own terms, and a mission line saying "the current cycle is `0.2`". Both are
exactly the class of false current-state belief the track exists to remove,
and `AGENTS.md` is an instruction file, so the cost of leaving it is higher
than for prose. The expired section is replaced by the permanent constraints
it contained (never move the `v0.1.8` tag; the archive worktree is not a
development surface). **No new permission was granted** — whether pushing to
public `main` is now allowed is Ocean's call and is not stated either way.
The onboarding-owned marker block is byte-identical; verified.

`BRIEF.md` was not touched in that pass; it was rewritten later, on Ocean's
ruling, together with the new `BACKLOG.md` — see §6.1.

---

## 4. `docs/ux/launcher.md`

The UX-SPEC pilot, in the agreed three-piece format: an ASCII screen per
state, a fact-source table (rendered element → the read-only fact behind it),
a mutation table (key → exactly what it writes), plus agent-facing surface
notes and a labelled to-be section. Every line was derived from `bin/pneu` and
`bin/_rtlauncher.py`.

States specified: entry routing with exit codes · project selection at three
registry states · first-run welcome · the seat card · both kinds of
unavailable row · the empty project · the notice line and all six of its
messages · the bound-thread suffix and its five preconditions · the numbered
selector · the sub-screens (guide, worktree list, add-seat prompt, one-time
setup plan).

Two behaviors the reading turned up that were not written down anywhere:

- **The numbered selector is not the non-TTY path** (see §3.1). This is the
  same finding as the README correction; it surfaced here first.
- **`_run_card_command` resolves output as `(stderr or stdout)`.** Because
  `rt-worktree list` prints the listing on stdout and registry warnings on
  stderr, a host with any registry warning sees the warnings under the `w`
  key *instead of* the sibling list. Recorded as-built; see §6 for the flag.

The to-be section carries six items. Three come from the brief (init census,
worktree roster inheritance, `pneu seat add/rm`). Three more were added by
Ocean's 2026-08-18 ruling, delivered mid-track as directive
`20260819T011104Z`: seat rows must show occupancy (vacant / active / stale
with owner locus), Enter on an active seat must offer jump / guarded takeover
/ cancel instead of dead-ending, and non-card refusals must name the holder
and the next action rather than printing pid-and-heartbeat forensics. The
motivating incident is cited in the spec: a Claude phone/web session held the
main project's seat and the launcher refused a relaunch with raw lease
forensics and no options.

Verified against code while writing those three: the card renders no
occupancy at all today — a live seat is indistinguishable from a vacant one,
and the only lease-derived signal is `(bound thread)`, which appears
precisely when the seat is *not* live. The refusal happens later, inside
`rt-<harness>`'s `claim_launch_seat`, after the card has already exited, and
`_owner_process_location` can only find a controlling tty or a tmux pane —
neither of which a phone/web session has, which is why that incident degraded
to a bare pid.

One refinement to the brief's own wording: the brief says "`a` is this
panel's only project-state write". Precisely, `a` is the only write to
durable, committed project state (`agents.yaml`). `p` also writes — the
registry-adjacent `rc-hosts/<uuid>.json`, a per-project LaunchAgent, and the
project's untracked `.claude/settings.local.json` — and `Enter` writes
`launcher.json`, which is git-ignored run state. The spec says all three.

---

## 4a. Audit rework

Three items from Ocean's rulings on the branch audit.

**Stray tracked files.** `GROK.md` and `downloaded_files/dashboard.lock` were
swept into the triage commit by a blanket `git add -A`. Neither belongs on the
branch: `GROK.md` was untracked before this session (this repo tracks
`AGENTS.md`, `CLAUDE.md`, and `HERMES.md` but not `GROK.md`), and
`downloaded_files/dashboard.lock` is a zero-byte artifact from another tool
that appeared in the worktree mid-session. Both are untracked again and the
final tree contains neither.

Two notes on how, because both were judgement calls:

- **History was not rewritten.** The directive preferred a clean branch
  history; `git rebase --exec` was refused by this session's permission
  layer, twice. The removal is therefore a forward commit, and `e0f3a12`
  still contains the two files. If you want them out of the history as well,
  a squash-merge does it, or you can rewrite locally — the pre-removal state
  is on the `backup/pre-strip` branch at `1b25408`.
- **The working-tree files were left in place.** "Remove from the branch" is
  what was executed; the untracked copies still exist on disk. `GROK.md` in
  particular is generated project state that this project's own Grok seat
  reads. Say the word and deleting them is one command.

The root cause was mine: `git add -A` in a repository whose worktree carries
untracked generated state. Staging by explicit path is the fix.

**`docs/ux/launcher.md` §1.5 was wrong.** It claimed a
neither-installed-nor-configured harness "reaches the numbered selector but
not the card". False: the card iterates every entry in `unavailable`, and the
override at `bin/pneu:1144-1147` rewrites the detail *only* for harnesses in
the addable set. A harness that is neither installed nor configured is not
addable, so its row renders with the unmodified detail. All three cases reach
the card; what differs is whether the row can promise that `a` will fix it.
Corrected, with the rendered row shown.

**Polish.** §1.9's decline-abort message now carries the
`project_ready_recovery` suffix that `onboard` appends to every
`OnboardingError` raised after project selection. §3's compare-and-swap is
named by its actual field tuple, `(st_dev, st_ino, st_size, st_mtime_ns)`.

---

## 5. Draft `decision.md` entry — for Ocean to ratify and append

Not applied. `decision.md` is untouched. Suggested text, in the ledger's
existing style, for the top of the list:

```markdown
- 2026-08-17: **A seat is the human's interactive session, on any surface**
  (Ocean). Restates the 2026-08-12 ruling with surface-neutral wording. A pneu
  seat is the session the human is actually working in: a terminal TUI, Codex
  Desktop, and a Claude phone session are all seats, and several clients
  driving one bound Codex thread are one seat, because the human sees one
  conversation. "Interactive TUI" in the 2026-08-12 entry was period shorthand
  for the only surface then in use, not a restriction to terminals; the
  bound-thread model is that entry's canonical reading. The acceptance test
  for any harness adaptation is unchanged and is what the ruling is for: mail
  must reach the session the human is actually in, and a headless replacement
  session does not count. Headless and oneshot agent processes remain
  legitimate inside a harness (subagents, teammates, internal automation) but
  are not user-facing seats. Recorded as principle 1 of PRINCIPLES.md, which
  is now the ranked constitution; decision.md remains the ledger of individual
  rulings.
```

Adjust the date to the day you append it if you prefer the ledger to record
ratification rather than the 2026-08-17 conversation.

---

## 6. Flagged — could not be resolved from code alone

Each with the competing sources named.

### 6.1 `BRIEF.md` — flagged here, then rewritten on your ruling

**Resolved after this report was first written.** Ocean ruled the rewrite in
as part of this track (directive `20260819T063651Z`), so `BRIEF.md` is now
one screen: north star as it stands at 1.3.5 with a pointer to
`PRINCIPLES.md` rather than a restatement, one paragraph on the consolidation
phase before 1.5, and the roadmap replaced by a pointer to `BACKLOG.md`. The
Completed and Launch-phase sections are gone; that history is in
`decision.md` and `handoff/archive/`.

`BACKLOG.md` is new, and it had to be: pointing at a file that does not exist
would have failed this track's own north star. It indexes open work by area —
decisions waiting on you, product surfaces, the open architecture tiers,
harness expansion, validation, the unfiled upstream issue, and two
documentation wording questions — with the file holding each item's real
detail. It restates no design and schedules nothing.

The original flag, kept because it is the record of what was wrong:

Not touched at the time, because it was your roadmap document rather than a
description of the code. It stated:

- **North star**: "ships as 1.0.0" — shipped; the release is 1.3.5.
- **Completed (0.2 line)** lists "OpenClaw Gateway adapter (credentialed
  real-model E2E)" as an achievement. Competing source: `decision.md`
  2026-08-17 removed OpenClaw from the shipped seat surface.
- **Roadmap #1** "In flight — Grok ACP adapter … productize on the OpenClaw
  supervisor template". Competing source: the ACP supervisor is now internal
  lab machinery and `rt-grok` is TUI-first (`docs/compatibility.md`).
- **Roadmap #2** (worktree lifecycle) and **#3** (the rename) are shipped.
- **Roadmap #4** (clean-machine validation + demo recording) and **#5**
  (launch materials): status unknown to me. #4 is still listed as an open gate
  in `docs/release.md`; #5's only artifact,
  `handoff/archive/launch-materials-brief.md`, describes 1.1.0.
- **Roadmap #6** (file the zero-turn-resume upstream issue) still matches
  reality: `handoff/upstream-codex-zero-turn-resume.md` is an unfiled draft.
- **Constraints** still names the review-window freeze that expired
  2026-08-12.

A rewrite was a scope decision rather than a fact correction, which is why it
waited for your ruling instead of riding along with the accuracy pass.

### 6.2 Three shipped surfaces with no current validation path

Principle 4 says a shipped surface has no zero-cost parking state and that
"we do not use it" raises rather than lowers the bar. Three surfaces are in
that position and none has a recorded retain/stop-shipping ruling:

- **`rt-say --legacy-nudge-only` and the cmux keyboard path.**
  `docs/legacy-v1-keyboard.md` presents it as the emergency manual path, and
  it requires `rt-refresh` from a live cmux surface. Competing sources: the
  path still ships (`bin/rt-say`, `rt-refresh`, `rt-resolve` are all in
  `pneu_packaging` `TOOLS`), while `architecture-review-1.4.md` Tier 2 calls
  it "retired" and proposes moving it into `_rtlegacy.py`.
- **`rt-stop-gate`.** `architecture-review-1.4.md` §6 Tier 2 calls it "a
  superseded hook generation still shipped as a tool" and says explicitly that
  its status "is a decision, not a refactor". It is still in `TOOLS`.
- **The optional cmux adapter generally** — `rt-startup-advisory`, the
  topology reads in `_rtlib`, the shim-rejection logic. `AGENTS.md`,
  `README.md`, and `docs/architecture.md` all describe cmux as a supported
  optional adapter, and `docs/compatibility.md` lists it in the terminal
  matrix as "pending baseline and separate optional-adapter gate".

The docs are internally consistent about all three; what is missing is a
ruling on whether they stay. I left every description as-is.

### 6.3 The `w` key can hide the worktree list

`_run_card_command` (`bin/pneu:1181`) returns `(result.stderr or
result.stdout)`. `rt-worktree list` prints its listing to stdout and registry
warnings to stderr, so on a host with any registry warning the `w` key
displays only the warnings. Documented as-built in `docs/ux/launcher.md`
§1.9. This looks like a defect rather than a decision, but it is a code
change and this track is docs-only.

### 6.4 "P0" as current vocabulary

`docs/architecture.md`, `docs/compatibility.md`, and `docs/install.md` use
"P0" throughout to mean "the first supported scope" — P0 requires a project
anchor, P0 has one seat per harness per project, multi-auth is "outside the
P0 lifecycle contract". At 1.3.5 this reads as pre-1.0 vocabulary that a new
reader has to decode, and there is no definition of P0 anywhere in `docs/`.
Not a contradiction and not corrected: renaming it across three files is a
wording decision, and it may still be the term you want.

### 6.5 Harness onboarding matrix still names RC artifacts

`docs/compatibility.md`'s matrix cites "installed-RC8", "RC7", and "RC5"
evidence for the Claude, Hermes, and Codex rows. Those are accurate
descriptions of pre-1.0 release candidates and the evidence is real, so
nothing was changed. Worth knowing that a new reader will not recognise the
labels; if you want them dated instead of RC-numbered, that is a small
follow-up.
