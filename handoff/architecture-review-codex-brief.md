# Dispatch — independent architecture review (codex seat, t5-adapters)

Seat: `codex` in this worktree. Requested by Ocean: a second, **independent**
architecture review of this project, to be cross-checked against one already
completed by the `claude` seat in this same tree.

## The point of this dispatch

You are the independent arm of a cross-check. A review that reproduces the other
one adds nothing; a review that disagrees with it, with reasons, is the whole
value. **Do not read `handoff/architecture-review-1.4.md` or
`handoff/architecture-review-1.4-track-collisions.md` until you have committed
your own findings.** Phase 2 below is when you read them, and by then your own
conclusions are already on record and cannot be quietly adjusted.

## Phase 1 — your own review

Question: **where should this project be modularized, decoupled, simplified, or
de-duplicated?** This is an architecture review, not a bug hunt — but report any
real defect you trip over, because several were found that way.

Scope: `bin/` (~30.5k lines), `pneu_packaging/` (~4.8k), `integrations/` (~2.5k),
`tests/` (~34k). Read what you need; you are not required to read everything, but
say what you did and did not read.

Deliverable: `handoff/architecture-review-1.4-codex.md`, committed to this branch.
For every finding give exact `file:line` anchors, the current shape, the proposed
shape, blast radius (which files and tests move), effort S/M/L, and risk. Rank
your own findings. Say plainly when an area is well-factored instead of inventing
work — that is a useful result, not a wasted section.

### Hard constraints

A proposal that violates one of these is worthless, so check before you write it:

1. **`bin/_rtmigrate.py` (3672 lines) is frozen** by an owner decision dated
   2026-07-29 in `decision.md`. Observe it if you like; propose no refactor of it.
2. **These are load-bearing safety, not accidental complexity**, and must never be
   removed or relaxed — though you may propose extracting or better isolating
   them: the layout locks and flock ordering, fenced seat leases and
   lease-revision validation, owned-path / fd-based filesystem validation, atomic
   maildir writes, fail-closed error handling, and the backup + manifest +
   rollback machinery in `pneu_packaging/setup.py`.
3. **Behavior-preserving only.** A 1081-test suite pins current behavior. The
   suite needs a Python with dev dependencies; `python` on PATH is pneu's managed
   interpreter and has no pytest.
4. **No new third-party dependencies.** Runtime today is stdlib plus PyYAML.
5. **`integrations/{grok,openclaw}` are recorded deviations** pending
   "rework TUI-first or park honestly" (`decision.md` 2026-08-12, tracked in D14).
   Weigh any proposal there against the chance that code shrinks or leaves.
6. Prefer proposals that **reduce the cost of adding a new harness integration** —
   that is the project's next major workstream. Say so when a finding has that
   leverage.

### Two things worth knowing before you start

- There are four other 1.4 tracks live in sibling worktrees (`handoff/t1-brief-1.4.md`
  through `t4-brief-1.4.md`). You may read those briefs; they tell you which files
  are currently owned by someone else. Do not let that narrow your findings —
  report what is true and let the scheduling be someone else's problem.
- Verify evidence yourself. In the first review, one headline proposal was killed
  because its stated blast radius was wrong in a way that only showed up by
  reading the actual loader code. Do not trust a plausible-sounding claim,
  including your own.

## Phase 2 — cross-check

Only after Phase 1 is committed:

1. Read `handoff/architecture-review-1.4.md` and
   `handoff/architecture-review-1.4-track-collisions.md`.
2. Write `handoff/architecture-review-1.4-crosscheck-codex.md` covering:
   - **Agreements** — findings you both reached independently. Note these
     briefly; convergence is evidence, and the list is more useful than the prose.
   - **Disagreements** — where you reach a different conclusion about the same
     code, with the reason and the evidence. Be specific and be willing to say the
     other review is wrong.
   - **Misses** — what you found that it did not.
   - **Errors** — any claim in it you can show is inaccurate. Check the four
     defects in its §4 and the refutations in its §5 against source yourself;
     they are the load-bearing parts.
   - **Sequencing** — whether you agree with its Tier 0/1/2/3 ordering and its
     "never in the same cycle" list, and what you would reorder.

## Constraints

- English-only artifacts, public-safe (no personal absolute paths, no session
  URLs). This repository's public-safety gate forbids `Claude-Session:` trailers
  in commit messages.
- Commit to **this worktree branch only** (`wt/t5-adapters`). Do **not** merge.
- Change no product code. Both phases are documents.
- Report completion to the claude seat in this project:
  `rt-say claude update "<one-line pointer>"`. Mail the same address if blocked.
  Your cross-check will be forwarded to `claude@roundtable-product`, which owns
  the final execution plan.
