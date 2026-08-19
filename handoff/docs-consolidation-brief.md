# Track brief — Documentation consolidation (1.5 layer 0)

> Status: current — the active brief for the 1.5 layer-0 documentation track

Date: 2026-08-17. Branch: `wt/docs-consolidation`. Dispatcher:
claude@roundtable-product (Ocean's order: "clean out stale and redundant
information in one sweep, so next-phase agents start from an accurate, clean
information surface").

> **Constitution.** The first principle is UX: pneu is "open your harness as
> you always do, and mail finds you there." A seat is the human's own
> interactive session on any surface; a mechanism that cannot reach the
> session the human is actually in is not an adaptation.
>
> **North star for this track.** After this track, a fresh agent reading this
> repository's documentation learns only true, current facts — every stale or
> superseded statement is corrected, archived, or labeled as history.

## Why (context you must internalize before editing)

Two shipped adapter schemes (the Grok ACP supervisor, the OpenClaw Gateway)
drifted off Ocean's intent because principles lived in conversation instead
of the repo, and because stale handoff documents taught later agents false
beliefs ("pneu opposes daemons", "a seat must be a terminal TUI"). Both
beliefs are over-generalizations of context-specific rulings. This track is
the structural fix: write the principles down once, and make every remaining
document either current-true or clearly labeled as history.

## Work items

### 1. Inventory and triage (`handoff/`, ~60 files)

Classify every file in `handoff/`: **current** (still the best statement of
a live fact), **historical** (a record of work done — correct then, not a
guide now), or **superseded** (a later file or ruling replaced it). Add a
one-line status header to each kept file
(`> Status: current | historical record | superseded by <file>`); `git mv`
historical and superseded files into `handoff/archive/` (keep names). Before
moving anything, grep the whole tree (code, tests, docs, README) for path
references and update them. `decision.md` is an append-only owner ledger:
never edit or move existing entries.

### 2. `PRINCIPLES.md` (new, repo root) — the ranked constitution

Contents were ratified by Ocean in conversation on 2026-08-17. Write them in
your own clear English; the ranked substance is:

1. **UX first.** The product is the experience "open your harness as always;
   mail finds you there." A seat is the human's own interactive session on
   any surface — terminal TUI, Codex Desktop, Claude phone. The acceptance
   test for any harness adaptation: mail reaches the session the human is
   actually in; a headless replacement session does not count (this restates
   decision.md 2026-08-12 with the surface-neutral wording; "TUI" there was
   period shorthand, and the bound-thread model — TUI/Desktop/phone driving
   one thread are the same seat — is the canonical reading).
2. **Delivery core vs. wake adapters.** Delivery needs no daemon, multiplexer,
   account, or network: the atomic maildir write is the delivery. Wake
   adapters use whatever the harness natively provides — including daemons
   (Codex app-server). Neither layer's rule generalizes to the other.
3. **State discipline.** Display surfaces detect and render fresh every run
   but never mutate state. Project state (`agents.yaml`) changes only by
   explicit acts. The roster is censused once at project birth from installed
   harnesses; a worktree inherits its parent checkout's roster at tree birth;
   `agents.yaml` is the project's collaboration authorization list — who this
   project's agents may address and plan around. (The census/inheritance
   behaviors are **to-be** — accepted design, not yet implemented; label them
   so.)
4. **Support-claim discipline (实然/应然).** A support claim requires a live
   end-to-end smoke on a real environment; fixtures and version comparisons
   never substitute (see `docs/compatibility.md`). "We do not use it" does
   not lower priority — it removes the validation path and raises the bar
   (decision.md 2026-08-17).
5. **Brief protocol.** Every dispatch brief opens with the constitution line
   and the track's north-star sentence; surface work additionally carries
   target screens (see item 4 below).

### 3. README + `docs/` accuracy pass

Audit every claim in `README.md` and `docs/*.md` against the shipped 1.3.5
reality and the current code; fix stale statements, cut redundancy between
README and docs (one home per fact, pointers elsewhere). Do not touch
onboarding-owned marker blocks — they must remain byte-identical. Keep
`PROVENANCE.md`/`CREDITS.md` untouched.

### 4. UX-SPEC pilot — `docs/ux/launcher.md`

First artifact of front-to-back development. Spec the `pneu` entry surface
**as built**, in the agreed three-piece format:

- ASCII screen per state (TTY seat card; non-TTY numbered selector; empty
  project; unavailable rows of both kinds — configured-but-missing with
  install hint, installed-but-unconfigured with "press a"; notice line;
  bound-thread suffix);
- a fact-source table (each rendered element → the read-only fact it renders);
- a mutation table (each key → exactly what state it changes; note that `a`
  is this panel's only project-state write);
- agent-facing surface notes where relevant (exit codes, non-TTY contract).

Derive from `bin/pneu` (`_render_seat_card`, `_addable_harnesses`) and
`bin/_rtlauncher.py`; every line of the spec must be checked against code,
not memory. Then a clearly-labeled **to-be** section: init census of
installed harnesses, worktree roster inheritance, `pneu seat add/rm` — as
design deltas awaiting scheduling. **This track ships documentation only:
zero behavior changes under `bin/`.**

### 5. Drafts for Ocean (in the report, not applied)

- A draft decision.md entry restating the 2026-08-12 seat ruling with
  surface-neutral wording (for Ocean to ratify and append himself).
- A short list of contradictions you found that you could NOT resolve from
  code alone, each with the competing sources named.

## Constraints

- Docs only; tests may be touched only where a moved file breaks a path
  reference (verify with the full suite if you touch any test).
- English throughout; public repository — no private session URLs, no
  `Claude-Session:` trailers (Co-Authored-By is fine).
- Run `python scripts/check_public_safety.py` before every commit, never
  piped into anything (read its exit code directly).
- Verify-before-label: a claim keeps "current" status only after you checked
  it against code or the 1.3.5 artifacts, not because it sounds right.
- Multi-agent fan-out (workflow / ultracode) at your discretion for the
  inventory sweep; keep judgments and final wording in one voice.

## Deliverable and protocol

- Commits on `wt/docs-consolidation` (logical commits per work item, not one
  blob). No merge — Ocean reviews.
- `handoff/docs-consolidation-report.md`: what moved, what changed status,
  the two draft items, and anything you flagged.
- `rt-ack` the dispatch mail on receipt. Questions via
  `rt-say claude@roundtable-product question "..."`. On completion,
  `rt-say claude@roundtable-product report "<one-line pointer>"`.
