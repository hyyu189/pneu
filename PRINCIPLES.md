# PRINCIPLES

> Status: current. Ratified by Ocean on 2026-08-17 and written down here so
> that later agents inherit the reasoning instead of re-deriving it from
> conversation. This file is the ranked constitution: when two documents
> disagree, the higher-ranked principle decides. It records principles, not
> decisions — individual rulings stay in `decision.md`, which remains the
> append-only owner ledger.

The list is ranked. Principle 1 outranks principle 2, and so on.

## 1. UX first

The product is one experience: **open your harness the way you always do, and
mail finds you there.** Everything else in this repository — the maildir, the
leases, the fences, the wake bridges — exists to deliver that sentence.

A **seat** is the human's own interactive session, on whatever surface they
actually use: a terminal TUI, Codex Desktop, a Claude phone session. The
surface is not the seat; the session is. Several clients driving one bound
Codex thread are one seat, because the human sees one conversation.

The acceptance test for any harness adaptation:

> Does mail reach the session the human is actually in?

A mechanism that instead spawns a headless replacement session, drains the
mailbox there, and reports success has failed this test. It did not adapt the
harness; it replaced the human's seat with a robot and answered a question
nobody asked.

This restates the 2026-08-12 ruling in `decision.md` with surface-neutral
wording. "Seats are interactive TUIs" was period shorthand: at the time every
seat happened to be a TUI. The canonical reading is the bound-thread model —
TUI, Desktop, and phone driving one thread are the same seat — and that
reading is what governs new work. Headless and oneshot agent processes remain
perfectly legitimate *inside* a harness (subagents, teammates, internal
automation); they are simply not user-facing seats.

## 2. The delivery core and the wake adapters obey different rules

These are two layers with two different contracts, and neither layer's rule
generalizes to the other.

**Delivery** needs no daemon, no multiplexer, no account, and no network. The
atomic write of a message file into the recipient's `new/` directory *is* the
delivery. This is why an offline seat loses nothing and why the core works in
an ordinary terminal.

**Wake** is an adapter layered on top, and it uses whatever the harness
natively provides — including a daemon. Codex's wake path runs through the
shared app-server daemon precisely because that is the native mechanism Codex
offers; that is correct, not a compromise.

So "pneu opposes daemons" is a false belief. The correct statement is that
*delivery* does not depend on one. A wake adapter that needs a daemon, a
socket, a launchd job, or a plugin is fine as long as the delivery layer
underneath it still works when that machinery is absent.

## 3. State discipline

Display surfaces detect and render fresh on every run, and never mutate state.
The launcher card reads the registry, the mailboxes, the leases, and the
rc-host record every time it draws; it writes nothing merely by being looked
at.

Project state changes only by explicit acts. `.roundtable/agents.yaml` is the
project's collaboration authorization list — the roster of who this project's
agents may address and plan around — so it changes when a person decides it
changes, never as a side effect of a scan.

**To-be (accepted design, not yet implemented).** Two roster behaviors are
agreed but unbuilt; do not read them as descriptions of the current code:

- *Census at project birth.* The roster should be censused once, at
  `roundtable-init` time, from the harnesses actually installed on the host.
  Today `roundtable-init` writes a fixed `claude` / `codex` / `hermes`
  template regardless of what is installed, which is why Grok Build is never
  in a new project's roster and has to be added afterwards with the
  launcher's `a` key.
- *Worktree inheritance at tree birth.* A new worktree should inherit its
  parent checkout's roster explicitly. Today there is no pneu mechanism for
  this: a linked worktree receives whatever `agents.yaml` the branch has
  committed, by ordinary Git checkout, and `pneu worktree add` then runs
  `roundtable-init`, which only fills in files that are missing.
- *`pneu seat add` / `pneu seat rm`.* The explicit roster commands do not
  exist. The launcher card's `a` key is the only roster write that ships
  today.

## 4. Support-claim discipline (应然 vs 实然)

A support claim requires a live, end-to-end smoke on a real environment.
Fixtures, unit tests, source inspection, and version-number comparisons are
evidence for *design*; they are never evidence for *support*. `docs/compatibility.md`
is the one home for what has actually been exercised and what has not.

Two corollaries that are easy to get backwards:

- **"We do not use it" does not lower the bar.** It removes the validation
  path, which *raises* the bar (`decision.md`, 2026-08-17). An adapter the
  team never exercises cannot accumulate live evidence, so it cannot be
  promoted, and it should not be shipped as if it were.
- **A shipped surface has no zero-cost parking state.** For anything already
  in a release, the honest options are retain, keep shipping, or stop
  shipping. Doing nothing is not the null option, because doing nothing ships
  it again.

The same discipline applies to documentation: a statement earns the label
"current" only after someone checked it against code or a release artifact.

## 5. Brief protocol

Every dispatch brief opens with two things:

1. the constitution line — the UX-first sentence from principle 1, so that the
   receiving agent starts from the product's purpose rather than from the
   task's mechanics;
2. the track's north-star sentence — one sentence describing the state of the
   world after the track succeeds.

A brief for work on a user-facing surface additionally carries its **target
screens**: what the user sees, in each state, when the work is done. Surface
work described only as a list of behaviors reliably produces a correct
mechanism behind an incoherent screen.

See [`docs/ux/launcher.md`](docs/ux/launcher.md) for the worked example of the
screen-level format.
